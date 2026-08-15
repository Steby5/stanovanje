"""The watch loop: poll each group once, then fan the posts out per subscriber."""

from __future__ import annotations

import logging
import random
import threading
import time

import requests

from .control import DiscordControl
from .delivery import Dispatcher
from .facebook import BrowserUnavailable, FacebookScraper, LoginRequired, ScrapeError
from .matcher import KeywordSyntaxError
from .models import Group, Post, load_groups
from .notify import DiscordNotifier
from .store import SeenStore
from .subscribers import Subscriber, SubscriberError, load_subscribers

log = logging.getLogger(__name__)

# After this many consecutive failed cycles, tell the admin on Discord.
ERROR_ALERT_THRESHOLD = 3


class Watcher:
    def __init__(
        self,
        cfg,
        notifier: DiscordNotifier | None = None,
        dispatcher_factory=None,
        scraper_factory=None,
    ):
        self.cfg = cfg
        # Admin channel: where operational warnings go, separate from the
        # per-subscriber notification routing.
        self.notifier = notifier or DiscordNotifier(cfg)
        # Overridable so tests (and any future transport) can swap delivery out.
        self._dispatcher_factory = dispatcher_factory or Dispatcher
        # Same seam for the browser: run_forever used to build one inline, which
        # is why none of the failure handling in it could be tested.
        self._scraper_factory = scraper_factory or FacebookScraper
        self.store = SeenStore(cfg.state_path, cfg.state_retention_days)
        self.groups: list[Group] = []
        self.subscribers: list[Subscriber] = []
        self.dispatcher = None
        self.session = requests.Session()
        self._consecutive_failures = 0
        self._alerted = False
        self._adopted_legacy = False
        self._config_broken = False
        # Commands run on their own thread so they are answered while a scrape
        # is in progress.  This guards the state both threads touch; the store
        # guards itself.  Never held across a browser call or an HTTP send.
        self._lock = threading.RLock()
        self._control_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._wake = threading.Event()

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

        Called each cycle, and by the control thread whenever a command edits
        one of those files.  The three attributes are swapped under the lock so
        the watch loop can never read a new subscriber list against the old
        dispatcher.
        """
        groups = load_groups(self.cfg.groups_path)
        if not groups:
            raise ValueError(f"{self.cfg.groups_path} has no groups in it")
        subscribers = load_subscribers(self.cfg)
        dispatcher = self._dispatcher_factory(self.cfg, subscribers, session=self.session)

        with self._lock:
            self.groups = groups
            self.subscribers = subscribers
            self.dispatcher = dispatcher

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
            # Deciding who gets this post touches the shared state, so it runs
            # under the lock - but only for microseconds.  Delivery is HTTP and
            # is deliberately left outside it, or a batch of notifications
            # would block commands for seconds.
            with self._lock:
                matches: list[tuple] = []
                for sub in watchers:
                    if self.store.has(sub.name, group.slug, post.post_id):
                        continue
                    totals["new"] += 1
                    # A dry run must leave no trace, or testing your rules
                    # would silently consume the posts a real run would send.
                    if notify:
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
                dispatcher = self.dispatcher

            if not matches:
                continue

            if not notify:
                for sub, result in matches:
                    log.info("[dry run] %s would get: %s — %s", sub.name, result.reason, post.url)
                totals["sent"] += len(matches)
                continue

            delivered = dispatcher.deliver(post, matches)
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

        if notify:
            self.store.save()
        return totals

    # -- one cycle -------------------------------------------------------
    def _safe_reload(self) -> None:
        """Re-read the config files, surviving a broken one.

        A typo in keywords.txt or a hand-edited subscribers.json used to raise
        straight out of the watch loop and kill the daemon - which flatly
        contradicts "edit them while the watcher is running", and is reachable
        from Discord, since commands write to disk before the reload validates.

        reload_inputs builds everything before swapping it in, so a failure
        leaves the previous good configuration in place and we simply carry on
        with it.
        """
        try:
            self.reload_inputs()
        except (ValueError, KeywordSyntaxError, SubscriberError, FileNotFoundError) as exc:
            if not self._config_broken:
                self._config_broken = True
                log.error("Could not reload the configuration: %s", exc)
                log.error("Carrying on with the last good settings; fix the file to apply changes.")
                self._alert(f"fbwatch cannot read its configuration: {exc}. Still running on the "
                            "previous settings - changes will not apply until it is fixed.")
            return
        if self._config_broken:
            self._config_broken = False
            log.info("Configuration is readable again.")
            self._alert("fbwatch configuration is readable again.")

    def run_cycle(self, scraper, notify: bool = True) -> dict:
        self._safe_reload()
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
    def request_stop(self, reason: str = "") -> None:
        """Ask the watch loop to finish and shut down cleanly.

        systemd sends SIGTERM on `systemctl restart`, and Python's default
        handler kills the interpreter without running the loop's `finally` -
        so the state file was never saved and every routine restart re-notified
        the last cycle's posts.  The signal handler calls this instead.
        """
        if reason:
            log.info("Stopping: %s", reason)
        self._stopping.set()
        self._wake.set()

    def run_forever(self, max_cycles: int | None = None) -> int:
        """Poll on an interval until stopped.  Returns a process exit code."""
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
            self.start_control_thread()
        elif self.cfg.control_enabled and self.cfg.discord_bot_token:
            log.warning("Discord control is configured but not usable - see the log above.")

        cycle = 0
        scraper = None
        try:
            while not self._stopping.is_set():
                cycle += 1
                # Recycling the browser periodically keeps memory flat on long runs.
                if scraper and cycle > 1 and (cycle - 1) % self.cfg.restart_browser_every_cycles == 0:
                    log.debug("restarting the browser")
                    scraper.stop()
                    scraper = None
                if scraper is None:
                    scraper = self._scraper_factory(self.cfg)
                    try:
                        scraper.start()
                    except BrowserUnavailable as exc:
                        for line in str(exc).splitlines():
                            log.error("%s", line)
                        self._alert(f"fbwatch cannot start Chromium: {str(exc).splitlines()[1]}")
                        return 3
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

                # Checked here rather than at the top of the loop, so a bounded
                # run does not sit through a poll interval it will never use.
                if max_cycles is not None and cycle >= max_cycles:
                    return 0

                wait = self.cfg.poll_interval_seconds + random.uniform(0, self.cfg.jitter_seconds)
                log.info(
                    "cycle %d done (%d new, %d matched, %d sent); next in %ds",
                    cycle, totals["new"], totals["matched"], totals["sent"], int(wait),
                )
                self._wait(wait)
            return 0
        except KeyboardInterrupt:
            log.info("Stopped.")
            return 0
        finally:
            self.stop_control_thread()
            if scraper:
                scraper.stop()
            # Always the last thing: this is what stops a restart re-notifying
            # everything the final cycle already delivered.
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

    # -- the command thread -----------------------------------------------
    def start_control_thread(self) -> None:
        """Answer Discord commands independently of the scan.

        Post hunting is a long sequence of blocking browser calls; a single
        thread cannot both do that and stay responsive.  This thread only ever
        touches files, the store and Discord's HTTP API - never the browser,
        which Playwright does not allow off its own thread.
        """
        if self.control is None or not self.control.enabled:
            return
        if self._control_thread and self._control_thread.is_alive():
            return

        def loop() -> None:
            while not self._stopping.is_set():
                try:
                    with self._lock:
                        flags = self.control.poll()
                except Exception as exc:  # noqa: BLE001 - keep answering commands
                    log.exception("Discord control poll failed: %s", exc)
                    flags = {}
                if flags.get("force_check"):
                    log.info("immediate check requested from Discord")
                    self._wake.set()
                self._stopping.wait(self.cfg.control_poll_seconds)

        self._control_thread = threading.Thread(
            target=loop, name="fbwatch-control", daemon=True
        )
        self._control_thread.start()
        log.info("Commands are handled separately, so they answer during a scan.")

    def stop_control_thread(self) -> None:
        self._stopping.set()
        if self._control_thread and self._control_thread.is_alive():
            self._control_thread.join(timeout=self.cfg.control_poll_seconds + 5)
        self._control_thread = None

    def _wait(self, seconds: float) -> None:
        """Wait for the next cycle, cut short if someone asks for a check."""
        if self._wake.wait(timeout=max(0.0, seconds)):
            self._wake.clear()

    # -- error reporting -------------------------------------------------
    def _note_failure(self, count: int) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= ERROR_ALERT_THRESHOLD and not self._alerted:
            # Latch only on a *successful* send.  A network outage is exactly
            # when this alert matters and exactly when the send fails, so
            # latching regardless meant the one message you needed was dropped
            # and never retried.
            self._alerted = self._alert(
                f"fbwatch has failed to read any group for "
                f"{self._consecutive_failures} cycles in a row ({count} error(s) last cycle). "
                f"Check {self.cfg.log_path.name}."
            )

    def _note_success(self) -> None:
        if self._alerted:
            self._alert("fbwatch is reading groups again.")
        self._consecutive_failures = 0
        self._alerted = False

    def _alert(self, message: str) -> bool:
        """Tell the operator something is wrong.  Returns whether it got through.

        Falls back to the control channel when no webhook is configured - the
        README now says webhooks are optional if you use the bot, and in that
        setup every operational alert was being discarded in silence.
        """
        if not self.cfg.notify_errors:
            return False

        text = f":warning: {message}"
        # One attempt, not four: an outage should cost seconds of the watch
        # loop, not the ~75s that four backed-off retries take.
        if self.notifier.enabled and self.notifier.send_text(text, attempts=1):
            return True
        if self.control is not None and self.control.enabled:
            try:
                self.control.reply(text)
                return True
            except Exception as exc:  # noqa: BLE001 - alerting must never raise
                log.debug("Could not alert via the control channel: %s", exc)
        return False
