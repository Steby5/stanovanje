"""The watch loop: poll each group once, then fan the posts out per subscriber."""

from __future__ import annotations

import logging
import random
import time

import requests

from .control import DiscordControl
from .delivery import Dispatcher
from .facebook import FacebookScraper, LoginRequired, ScrapeError
from .models import Group, Post, load_groups
from .notify import DiscordNotifier
from .store import SeenStore
from .subscribers import Subscriber, load_subscribers

log = logging.getLogger(__name__)

# After this many consecutive failed cycles, tell the admin on Discord.
ERROR_ALERT_THRESHOLD = 3


class Watcher:
    def __init__(self, cfg, notifier: DiscordNotifier | None = None, dispatcher_factory=None):
        self.cfg = cfg
        # Admin channel: where operational warnings go, separate from the
        # per-subscriber notification routing.
        self.notifier = notifier or DiscordNotifier(cfg)
        # Overridable so tests (and any future transport) can swap delivery out.
        self._dispatcher_factory = dispatcher_factory or Dispatcher
        self.store = SeenStore(cfg.state_path, cfg.state_retention_days)
        self.groups: list[Group] = []
        self.subscribers: list[Subscriber] = []
        self.dispatcher = None
        self.session = requests.Session()
        self._consecutive_failures = 0
        self._alerted = False
        self._adopted_legacy = False

        # Live state, reported by the Discord `status` command.
        self.paused = False
        self.cycles = 0
        self.total_sent = 0
        self.started_at = time.time()
        self.last_cycle_at: float | None = None
        self.control = None

    # -- inputs ---------------------------------------------------------
    def reload_inputs(self) -> None:
        """Re-read groups, subscribers and every trigger-word file.

        Called each cycle, so edits (from a text editor or from Discord) apply
        without a restart.
        """
        self.groups = load_groups(self.cfg.groups_path)
        if not self.groups:
            raise ValueError(f"{self.cfg.groups_path} has no groups in it")

        self.subscribers = load_subscribers(self.cfg)
        self.dispatcher = self._dispatcher_factory(
            self.cfg, self.subscribers, session=self.session
        )

        # State written before multi-user support belongs to the primary user.
        if not self._adopted_legacy:
            primary = next((s for s in self.subscribers if s.admin), self.subscribers[0])
            moved = self.store.adopt_legacy(primary.name)
            if moved:
                log.info("Migrated %d remembered post(s) to subscriber '%s'", moved, primary.name)
            self._adopted_legacy = True

    @property
    def active_subscribers(self) -> list[Subscriber]:
        return [s for s in self.subscribers if s.deliverable]

    # -- one group -------------------------------------------------------
    def check_group(self, scraper, group: Group, notify: bool = True) -> dict:
        """Scrape one group once, then notify everyone the posts matched.

        Posts drive the loop rather than subscribers, so a post matching
        several people who share a channel becomes one message mentioning them
        all, instead of the same listing arriving once per person.  Posts are
        handled oldest-first so they arrive in the order they were posted.
        """
        posts: list[Post] = scraper.scrape_group(group)
        totals = {"seen": len(posts), "new": 0, "matched": 0, "sent": 0}

        watchers = [s for s in self.subscribers if s.watches(group) and s.deliverable]
        if not watchers:
            return totals

        # A subscriber's first sight of a group is its whole visible feed;
        # record it but stay quiet, per subscriber rather than per group.
        first_time = {s.name: not self.store.knows_group(s.name, group.slug) for s in watchers}
        seeded = {s.name: 0 for s in watchers}

        for post in reversed(posts):
            matches: list[tuple] = []

            for sub in watchers:
                if self.store.has(sub.name, group.slug, post.post_id):
                    continue
                totals["new"] += 1
                self.store.add(sub.name, group.slug, post.post_id)

                if first_time[sub.name] and not self.cfg.notify_on_first_run:
                    seeded[sub.name] += 1
                    continue

                result = sub.matcher.match(post.text)
                if not result.matched:
                    log.debug("%s: skip %s (%s)", sub.name, post.url, result.reason)
                    continue
                totals["matched"] += 1

                # Paused means "mute": keep recording so resuming does not
                # replay everything that piled up in the meantime.
                if self.paused:
                    continue

                matches.append((sub, result))

            if not matches:
                continue

            if not notify:
                for sub, result in matches:
                    log.info("[dry run] %s would get: %s — %s", sub.name, result.reason, post.url)
                totals["sent"] += len(matches)
                continue

            delivered = self.dispatcher.deliver(post, matches)
            totals["sent"] += len(delivered)
            for sub, result in matches:
                if sub.name in delivered:
                    log.info("-> %s: %s [%s]", sub.name, post.url, result.reason)
                else:
                    # Delivery failed - forget it so the next cycle retries
                    # rather than silently losing the post for this person.
                    self.store.forget(sub.name, group.slug, post.post_id)
                    log.warning("%s: delivery failed, retrying next cycle: %s", sub.name, post.url)

        for name, count in seeded.items():
            if count:
                log.info(
                    "%s/%s: first poll, recorded %d existing post(s) without notifying",
                    name, group.name, count,
                )

        self.store.save()
        return totals

    # -- one cycle -------------------------------------------------------
    def run_cycle(self, scraper, notify: bool = True) -> dict:
        self.reload_inputs()
        totals = {"seen": 0, "new": 0, "matched": 0, "sent": 0, "errors": 0}

        idle = [s for s in self.subscribers if s.enabled and not s.deliverable]
        for sub in idle:
            log.warning("subscriber '%s' is receiving nothing: %s", sub.name, sub.why_idle())

        for index, group in enumerate(self.groups):
            try:
                stats = self.check_group(scraper, group, notify=notify)
                for key, value in stats.items():
                    totals[key] += value
                log.info(
                    "%s: %d post(s) on page, %d new, %d matched, %d sent",
                    group.name, stats["seen"], stats["new"], stats["matched"], stats["sent"],
                )
            except LoginRequired:
                raise  # the loop handles this; no point trying other groups
            except ScrapeError as exc:
                totals["errors"] += 1
                log.warning("%s", exc)
            except Exception as exc:  # noqa: BLE001 - one bad group must not stop the rest
                totals["errors"] += 1
                log.exception("%s: unexpected error: %s", group.name, exc)

            if index < len(self.groups) - 1:
                pause = random.uniform(
                    self.cfg.min_delay_between_groups, self.cfg.max_delay_between_groups
                )
                log.debug("waiting %.1fs before the next group", pause)
                time.sleep(pause)

        return totals

    # -- forever ---------------------------------------------------------
    def run_forever(self) -> int:
        """Poll on an interval until interrupted.  Returns a process exit code."""
        self.reload_inputs()
        active = self.active_subscribers
        log.info(
            "Watching %d group(s) for %d subscriber(s); polling every ~%ds",
            len(self.groups), len(active), self.cfg.poll_interval_seconds,
        )
        for sub in active:
            log.info(
                "  %-16s %d rule(s) -> %s",
                sub.name, len(sub.matcher.includes), self.dispatcher.describe(sub),
            )
        if not active:
            log.error(
                "Nobody is set up to receive anything. Check %s.",
                self.cfg.subscribers_path.name,
            )
            return 1

        self.control = DiscordControl(self.cfg, self)
        if self.control.enabled:
            self.control.start()
            self._warn_if_admin_unclaimed()
        elif self.cfg.control_enabled and self.cfg.discord_bot_token:
            log.warning("Discord control is configured but not usable - see the log above.")

        cycle = 0
        scraper: FacebookScraper | None = None
        try:
            while True:
                cycle += 1
                # Recycling the browser periodically keeps memory flat on long runs.
                if scraper and cycle > 1 and (cycle - 1) % self.cfg.restart_browser_every_cycles == 0:
                    log.debug("restarting the browser")
                    scraper.stop()
                    scraper = None
                if scraper is None:
                    scraper = FacebookScraper(self.cfg)
                    scraper.start()
                    if not scraper.is_logged_in():
                        log.error("Not logged in. Run:  python main.py login")
                        self._alert("Facebook session is gone - run `python main.py login`.")
                        return 2

                log.info("--- cycle %d ---", cycle)
                try:
                    totals = self.run_cycle(scraper)
                except LoginRequired as exc:
                    log.error("Facebook wants a login: %s", exc)
                    self._alert(f"Facebook session expired ({exc}). Run `python main.py login`.")
                    return 2

                if totals["errors"] and not (totals["seen"] or totals["new"]):
                    self._note_failure(totals["errors"])
                else:
                    self._note_success()

                self.cycles = cycle
                self.total_sent += totals["sent"]
                self.last_cycle_at = time.time()
                self.store.prune()
                self.store.save()

                wait = self.cfg.poll_interval_seconds + random.uniform(0, self.cfg.jitter_seconds)
                log.info(
                    "cycle %d done (%d new, %d matched, %d sent); next in %ds",
                    cycle, totals["new"], totals["matched"], totals["sent"], int(wait),
                )
                self._wait(wait)
        except KeyboardInterrupt:
            log.info("Stopped.")
            return 0
        finally:
            if scraper:
                scraper.stop()
            self.store.save()

    def _warn_if_admin_unclaimed(self) -> None:
        """Point out that admin commands are open to the whole channel.

        Harmless on a private server with one person in it; worth saying out
        loud once other people are subscribed.
        """
        if any(s.discord_user_id for s in self.subscribers if s.admin):
            return
        if len(self.subscribers) < 2 and not self.cfg.control_allowed_user_ids:
            return  # single-user setup: nothing to protect yet
        if self.cfg.control_allowed_user_ids:
            return  # the hard allowlist already restricts the channel
        log.warning(
            "No admin has a discord_user_id, so anyone who can post in the control "
            "channel can run admin commands. Claim it with:  "
            "python main.py users <name> --discord-id <your Discord user id>"
        )

    def _wait(self, seconds: float) -> None:
        """Sleep until the next cycle, staying responsive to Discord commands.

        Returns early when someone asks for an immediate check.
        """
        deadline = time.monotonic() + seconds
        if self.control is None or not self.control.enabled:
            time.sleep(max(0.0, seconds))
            return

        step = self.cfg.control_poll_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(step, remaining))
            flags = self.control.poll()
            if flags.get("force_check"):
                log.info("immediate check requested from Discord")
                return

    # -- error reporting -------------------------------------------------
    def _note_failure(self, count: int) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= ERROR_ALERT_THRESHOLD and not self._alerted:
            self._alert(
                f"fbwatch has failed to read any group for "
                f"{self._consecutive_failures} cycles in a row ({count} error(s) last cycle). "
                f"Check {self.cfg.log_path.name}."
            )
            self._alerted = True

    def _note_success(self) -> None:
        if self._alerted:
            self._alert("fbwatch is reading groups again.")
        self._consecutive_failures = 0
        self._alerted = False

    def _alert(self, message: str) -> None:
        if self.cfg.notify_errors and self.notifier.enabled:
            self.notifier.send_text(f":warning: {message}")
