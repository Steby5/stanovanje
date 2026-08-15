"""Configure the watcher by typing commands in Discord.

A webhook can only send, never receive, so this half needs a bot token.  It
polls the control channel over the REST API between scrape cycles - no gateway,
no asyncio, so it slots into the existing synchronous loop.

Two levels of access:

* everyone manages **their own** trigger words (`add`, `remove`, `exclude`),
  matched to their subscription by Discord user id;
* **admins** manage people and shared settings (`user ...`, `group ...`,
  `pause`, `interval`), and can act on someone else's list with `for <name>`.

Commands edit `groups.txt`, the per-person keyword files and `subscribers.json`
in place.  Those are re-read every cycle, so a change applies immediately,
survives a restart, and stays editable in a text editor too.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import requests

from .matcher import KeywordMatcher, KeywordSyntaxError, parse_rule
from .models import load_groups, parse_group_line
from .subscribers import (
    Subscriber,
    SubscriberError,
    ensure_keywords_file,
    find_by_discord_id,
    find_subscriber,
    save_subscribers,
)
from .telegram import recent_chats
from .textutil import truncate

log = logging.getLogger(__name__)

API_ROOT = "https://discord.com/api/v10"
MAX_MESSAGE = 1900  # Discord's limit is 2000; leave room for formatting

HELP_EVERYONE = """**fbwatch — your trigger words**  (prefix `{p}`)

`{p} add <rule>` — notify me about posts matching this
`{p} remove <rule>` — stop notifying me about it
`{p} exclude <term>` — never notify me on posts containing it
`{p} mine` — show my rules and where my notifications go
`{p} test <text>` — would this post notify me?
`{p} channel <channel id>` — send my listings elsewhere (`{p} channel here` to undo)
`{p} mention off` — receive listings without being pinged
`{p} status` — what the watcher is doing

Rule syntax: `word`, `a + b` (both), `"exact phrase"`, `=wholeword`, `re:<regex>`.
"""

HELP_ADMIN = """
**Admin**
`{p} users` — everyone subscribed
`{p} user add <name>` — add a person
`{p} user remove <name>`
`{p} user set <name> channel <channel id>` — post there as the bot
`{p} user set <name> webhook <url>` — or use a webhook instead
`{p} user set <name> telegram <chat id>` — their Telegram chat
`{p} user set <name> discord <user id>` — let them manage their own rules
`{p} user enable <name>` / `{p} user disable <name>`
`{p} for <name> add <rule>` — edit someone else's rules
`{p} telegram ids` — chat ids of people who messaged the Telegram bot

`{p} group add <url> [| name]` / `{p} group remove <name or id>`
`{p} list` — groups and every person's rules
`{p} pause` / `{p} resume` · `{p} check` · `{p} interval <seconds>`
"""

# Commands only an admin may run.
ADMIN_COMMANDS = {
    "users", "user", "for", "group", "groups", "pause", "mute", "resume",
    "unmute", "check", "interval", "list", "telegram",
}


def _safe_name(display_name: str) -> str:
    """Turn a Discord username into something usable as a subscriber name."""
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", (display_name or "").lower()).strip("-._")
    return cleaned[:32] if re.match(r"^[a-z0-9]", cleaned or "") else ""


class DiscordControl:
    """Polls a Discord channel for commands and applies them."""

    def __init__(self, cfg, watcher, session: requests.Session | None = None):
        self.cfg = cfg
        self.watcher = watcher
        self.token = (cfg.discord_bot_token or "").strip()
        self.channel_id = str(cfg.discord_control_channel_id or "").strip()
        self.prefix = (cfg.command_prefix or "!fbw").strip()
        self.allowed = {str(u) for u in (cfg.control_allowed_user_ids or [])}
        self.session = session or requests.Session()

        self._after: str | None = None
        self._disabled = False
        self._warned_about_content = False

    @property
    def enabled(self) -> bool:
        return bool(
            self.cfg.control_enabled and self.token and self.channel_id and not self._disabled
        )

    # -- HTTP ------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> requests.Response | None:
        headers = {
            "Authorization": f"Bot {self.token}",
            "User-Agent": "fbwatch (https://localhost, 1.0)",
        }
        for _ in range(3):
            try:
                resp = self.session.request(
                    method, f"{API_ROOT}{path}", headers=headers, timeout=15, **kwargs
                )
            except requests.RequestException as exc:
                log.debug("Discord control request failed: %s", exc)
                return None

            if resp.status_code == 429:
                wait = 5.0
                try:
                    wait = float(resp.json().get("retry_after", wait))
                except (ValueError, requests.exceptions.JSONDecodeError):
                    pass
                time.sleep(min(wait + 0.5, 30))
                continue

            if resp.status_code in (401, 403):
                log.error(
                    "Discord control disabled: %s. Check discord_bot_token, and that the "
                    "bot can View Channel / Read Message History / Send Messages in %s.",
                    resp.status_code, self.channel_id,
                )
                self._disabled = True
                return None

            if resp.status_code == 404:
                log.error(
                    "Discord control disabled: channel %s not found (is the bot in that "
                    "server?)", self.channel_id,
                )
                self._disabled = True
                return None

            return resp
        return None

    def verify(self) -> str:
        """Check the token and channel.  Returns '' when everything is fine."""
        if not self.token:
            return "no discord_bot_token configured"
        if not self.channel_id:
            return "no discord_control_channel_id configured"
        resp = self._request("GET", f"/channels/{self.channel_id}")
        if resp is None:
            return "could not reach the channel (see the log for why)"
        if resp.status_code != 200:
            return f"Discord returned {resp.status_code}: {resp.text[:200]}"
        return ""

    def start(self) -> None:
        """Anchor to the newest message so old commands are not replayed."""
        if not self.enabled:
            return
        resp = self._request("GET", f"/channels/{self.channel_id}/messages", params={"limit": 1})
        if resp is not None and resp.status_code == 200:
            messages = resp.json()
            if messages:
                self._after = messages[0]["id"]
        if self.enabled:
            log.info("Discord control is live - type `%s help` in the channel.", self.prefix)

    def reply(self, text: str) -> None:
        if not self.enabled:
            return
        self._request(
            "POST",
            f"/channels/{self.channel_id}/messages",
            json={"content": truncate(text, MAX_MESSAGE), "allowed_mentions": {"parse": []}},
        )

    # -- polling ---------------------------------------------------------
    def poll(self) -> dict:
        """Read new messages and run any commands.  Returns control flags."""
        flags: dict = {}
        if not self.enabled:
            return flags

        params = {"limit": 50}
        if self._after:
            params["after"] = self._after
        resp = self._request("GET", f"/channels/{self.channel_id}/messages", params=params)
        if resp is None or resp.status_code != 200:
            return flags

        messages = resp.json()
        if not messages:
            return flags
        self._after = messages[0]["id"]  # newest first

        for message in reversed(messages):
            if message.get("author", {}).get("bot") or message.get("webhook_id"):
                continue  # our own notifications, and other bots
            content = (message.get("content") or "").strip()
            if not content:
                self._warn_missing_content()
                continue
            if not content.lower().startswith(self.prefix.lower()):
                continue

            author = message.get("author", {})
            author_id = str(author.get("id", ""))
            if self.allowed and author_id not in self.allowed:
                self.reply(f":no_entry: <@{author_id}> is not allowed to configure fbwatch.")
                continue

            command = content[len(self.prefix):].strip()
            log.info("Discord command from %s: %s", author.get("username", "?"), command)
            try:
                reply, command_flags = self.handle(
                    command, author_id, author.get("username", "")
                )
            except Exception as exc:  # noqa: BLE001 - a bad command must not kill the loop
                log.exception("command failed: %s", command)
                reply, command_flags = f":x: That failed: `{exc}`", {}
            flags.update(command_flags)
            if reply:
                self.reply(reply)

        return flags

    def _warn_missing_content(self) -> None:
        if not self._warned_about_content:
            log.warning(
                "A message arrived with empty content. If commands are being ignored, "
                "enable the MESSAGE CONTENT INTENT for the bot at "
                "https://discord.com/developers/applications"
            )
            self._warned_about_content = True

    # -- who is asking ----------------------------------------------------
    def _resolve(self, author_id: str) -> tuple[Subscriber | None, bool]:
        """Map a Discord user to their subscription and admin rights.

        Admin rights key off whether *an admin* has linked a Discord account,
        not whether anyone has.  Linking a normal user first would otherwise
        lock everybody out of the admin commands, recoverable only by editing
        subscribers.json on the machine itself.
        """
        subs = self.watcher.subscribers
        me = find_by_discord_id(subs, author_id)

        if not any(s.discord_user_id for s in subs if s.admin):
            # No admin has claimed a Discord account, so trust the channel:
            # whoever can post in it administers, as they did before anyone
            # was linked.  Linking an admin turns this off.
            primary = next((s for s in subs if s.admin), subs[0] if subs else None)
            return (me or primary), True

        return me, bool(me and me.admin)

    # -- commands ---------------------------------------------------------
    def handle(self, command: str, author_id: str = "", author_name: str = "") -> tuple[str, dict]:
        """Run one command.  Returns (reply text, control flags)."""
        me, is_admin = self._resolve(author_id)

        if not command:
            return self._help(is_admin), {}

        verb, _, rest = command.partition(" ")
        verb = verb.lower()
        rest = rest.strip()

        if verb in ("help", "commands"):
            return self._help(is_admin), {}

        if verb in ADMIN_COMMANDS and not is_admin:
            return f":no_entry: `{verb}` is admin-only. `{self.prefix} help` shows what you can do.", {}

        # Admin acting on someone else: `for ana add oddam + soba`
        target = me
        if verb == "for":
            name, _, remainder = rest.partition(" ")
            target = find_subscriber(self.watcher.subscribers, name)
            if target is None:
                return f":x: No subscriber called `{name}`. `{self.prefix} users` lists them.", {}
            if not remainder.strip():
                return self._mine_text(target), {}
            verb, _, rest = remainder.strip().partition(" ")
            verb, rest = verb.lower(), rest.strip()

        handlers = {
            "status": lambda r, t: (self._cmd_status(), {}),
            "list": lambda r, t: (self._cmd_list(), {}),
            "mine": lambda r, t: (self._mine_text(t), {}),
            "me": lambda r, t: (self._mine_text(t), {}),
            "whoami": lambda r, t: (self._mine_text(t), {}),
            "add": self._cmd_add,
            "remove": self._cmd_remove,
            "rm": self._cmd_remove,
            "delete": self._cmd_remove,
            "exclude": self._cmd_exclude,
            "test": self._cmd_test,
            "channel": self._cmd_channel,
            "mention": self._cmd_mention,
            "join": lambda r, t: (self._mine_text(t), {}),
            "users": lambda r, t: (self._users_text(), {}),
            "user": self._cmd_user,
            "telegram": self._cmd_telegram,
            "group": self._cmd_group,
            "groups": lambda r, t: (self._groups_text(), {}),
            "pause": self._cmd_pause,
            "mute": self._cmd_pause,
            "resume": self._cmd_resume,
            "unmute": self._cmd_resume,
            "check": self._cmd_check,
            "interval": self._cmd_interval,
        }
        handler = handlers.get(verb)
        if handler is None:
            return f":grey_question: Unknown command `{verb}`. Try `{self.prefix} help`.", {}

        # Everything below this point acts on somebody's own subscription.
        personal = (
            "add", "remove", "rm", "delete", "exclude", "mine", "me", "whoami",
            "test", "channel", "mention", "join",
        )
        if verb in personal and target is None:
            target, problem = self._sign_up(author_id, author_name)
            if target is None:
                return problem, {}
            if verb == "join":
                return (
                    f":wave: Welcome, **{target.name}**. Tell me what to watch for with "
                    f"`{self.prefix} add oddam + soba`, then check it with "
                    f"`{self.prefix} mine`.",
                    {},
                )
        return handler(rest, target)

    def _sign_up(self, author_id: str, author_name: str) -> tuple[Subscriber | None, str]:
        """Create a subscription for whoever just spoke, if that's allowed."""
        if not self.cfg.allow_self_signup:
            return None, (
                ":wave: You don't have a subscription yet. An admin can add you with "
                f"`{self.prefix} user add <name>` and link you with "
                f"`{self.prefix} user set <name> discord {author_id}`."
            )
        if not author_id:
            return None, ":x: I couldn't tell who you are, so I can't set you up."

        name = _safe_name(author_name) or f"user{author_id[-6:]}"
        if find_subscriber(self.watcher.subscribers, name):
            name = f"{name}-{author_id[-4:]}"  # two people, same display name
        try:
            sub = Subscriber(name=name, discord_user_id=author_id)
        except SubscriberError as exc:
            return None, f":x: {exc}"

        save_subscribers(self.cfg, list(self.watcher.subscribers) + [sub])
        ensure_keywords_file(self.cfg, sub)
        try:
            self.watcher.reload_inputs()
        except (ValueError, KeywordSyntaxError, SubscriberError, FileNotFoundError) as exc:
            return None, f":warning: Could not set you up: {exc}"

        created = find_subscriber(self.watcher.subscribers, name)
        log.info("self-signup: created subscriber '%s' for Discord user %s", name, author_id)
        return created, ""

    def _help(self, is_admin: bool) -> str:
        text = HELP_EVERYONE.format(p=self.prefix)
        if is_admin:
            text += HELP_ADMIN.format(p=self.prefix)
        return text

    # -- reporting --------------------------------------------------------
    def _cmd_status(self) -> str:
        w = self.watcher
        hours, remainder = divmod(int(time.time() - w.started_at), 3600)
        state = (
            ":pause_button: paused (recording, not notifying)"
            if w.paused else ":green_circle: watching"
        )
        active = w.active_subscribers
        lines = [
            f"**fbwatch** — {state}",
            f"Groups: **{len(w.groups)}**  ·  Subscribers: **{len(active)} active** "
            f"of {len(w.subscribers)}",
            f"Cycles: **{w.cycles}**  ·  Notifications sent: **{w.total_sent}**",
            f"Uptime: **{hours}h {remainder // 60}m**  ·  Interval: **{self.cfg.poll_interval_seconds}s**",
        ]
        if w.last_cycle_at:
            lines.append(f"Last cycle: <t:{int(w.last_cycle_at)}:R>")
        idle = [s for s in w.subscribers if s.enabled and not s.deliverable]
        if idle:
            lines.append(
                "\n:warning: receiving nothing — "
                + ", ".join(f"**{s.name}** ({s.why_idle()})" for s in idle)
            )
        return "\n".join(lines)

    def _cmd_list(self) -> str:
        parts = [self._groups_text(), self._users_text()]
        return "\n\n".join(parts)

    def _groups_text(self) -> str:
        groups = load_groups(self.cfg.groups_path)
        if not groups:
            return "**Groups:** none configured."
        listed = "\n".join(f"· {g.name}  — `{g.slug}`" for g in groups)
        return f"**Groups ({len(groups)})**\n{listed}"

    def _users_text(self) -> str:
        subs = self.watcher.subscribers
        if not subs:
            return "**Subscribers:** none."
        lines = []
        for sub in subs:
            marks = []
            if sub.admin:
                marks.append("admin")
            if not sub.enabled:
                marks.append("disabled")
            where = ", ".join(sub.destinations) or "nowhere"
            rules = len(sub.matcher.includes) if sub.matcher else 0
            suffix = f"  ({', '.join(marks)})" if marks else ""
            idle = "" if sub.deliverable else f"  :warning: {sub.why_idle()}"
            lines.append(f"· **{sub.name}**{suffix} — {rules} rule(s) → {where}{idle}")
        return f"**Subscribers ({len(subs)})**\n" + "\n".join(lines)

    def _mine_text(self, sub: Subscriber) -> str:
        matcher = sub.matcher or KeywordMatcher([])
        triggers = "\n".join(f"· `{r.raw}`" for r in matcher.includes) or "· *(none yet)*"
        text = (
            f"**{sub.name}** → {', '.join(sub.destinations) or 'nowhere'}\n"
            f"**Trigger rules ({len(matcher.includes)})**\n{triggers}"
        )
        if matcher.excludes:
            text += "\n**Exclusions**\n" + "\n".join(f"· `!{r.raw}`" for r in matcher.excludes)
        if sub.groups:
            text += f"\n**Only these groups:** {', '.join(sub.groups)}"
        if not sub.deliverable:
            text += f"\n\n:warning: Receiving nothing — {sub.why_idle()}."
        return text

    # -- trigger words ----------------------------------------------------
    def _cmd_add(self, rest: str, sub: Subscriber) -> tuple[str, dict]:
        if not rest:
            return f":grey_question: Usage: `{self.prefix} add oddam + soba`", {}
        try:
            parse_rule(rest)
        except KeywordSyntaxError as exc:
            return f":x: I can't read that rule: {exc}", {}

        path = ensure_keywords_file(self.cfg, sub)
        if _find_line(path, rest) is not None:
            return f":information_source: `{rest}` is already on **{sub.name}**'s list.", {}
        _append_line(path, rest)
        return self._reload(f":white_check_mark: Added `{rest}` for **{sub.name}**.")

    def _cmd_remove(self, rest: str, sub: Subscriber) -> tuple[str, dict]:
        if not rest:
            return f":grey_question: Usage: `{self.prefix} remove oddam + soba`", {}
        path = sub.keywords_path(self.cfg)
        if not path.exists():
            return f":x: **{sub.name}** has no rules yet.", {}
        removed = _remove_line(path, rest)
        if not removed:
            return f":x: `{rest}` isn't on **{sub.name}**'s list. `{self.prefix} mine` shows it.", {}
        return self._reload(f":wastebasket: Removed `{removed}` for **{sub.name}**.")

    def _cmd_exclude(self, rest: str, sub: Subscriber) -> tuple[str, dict]:
        if not rest:
            return f":grey_question: Usage: `{self.prefix} exclude agencija`", {}
        rule = rest if rest.startswith("!") else f"!{rest}"
        try:
            parse_rule(rule)
        except KeywordSyntaxError as exc:
            return f":x: I can't read that: {exc}", {}
        path = ensure_keywords_file(self.cfg, sub)
        if _find_line(path, rule) is not None:
            return f":information_source: `{rule}` is already excluded for **{sub.name}**.", {}
        _append_line(path, rule)
        return self._reload(
            f":mute: **{sub.name}** won't see posts containing `{rule.lstrip('!')}`."
        )

    def _cmd_test(self, rest: str, sub: Subscriber) -> tuple[str, dict]:
        if not rest:
            return f":grey_question: Usage: `{self.prefix} test Oddam sobo v Ljubljani`", {}
        matcher = sub.matcher or KeywordMatcher([])
        result = matcher.match(rest)
        matched = result.matched and bool(matcher.includes)
        verdict = (
            f":white_check_mark: **YES** — this would notify **{sub.name}**"
            if matched else f":no_bell: **No** — **{sub.name}** would not be notified"
        )
        reason = result.reason if matcher.includes else "no trigger words set"
        return f"{verdict}\nReason: `{reason}`", {}

    def _cmd_channel(self, rest: str, sub: Subscriber) -> tuple[str, dict]:
        """Redirect my own listings to another channel."""
        value = rest.strip()

        if not value:
            where = self.watcher.dispatcher.describe(sub) if self.watcher.dispatcher else "?"
            return (
                f"**{sub.name}** → {where}\n"
                f"Send them elsewhere with `{self.prefix} channel <channel id>`, or "
                f"`{self.prefix} channel here` to use this one.",
                {},
            )

        if value.lower() in ("here", "default", "reset", "none"):
            sub.discord_channel_id = ""
            save_subscribers(self.cfg, self.watcher.subscribers)
            return self._reload(
                f":white_check_mark: **{sub.name}**'s listings will arrive in this channel."
            )

        if not value.isdigit():
            return (
                ":x: A channel id is a number — turn on **Settings → Advanced → "
                "Developer Mode**, then right-click the channel → **Copy Channel ID**.",
                {},
            )
        if not (self.cfg.discord_bot_token or "").strip():
            return ":x: Posting into a channel needs `discord_bot_token` in config.json.", {}
        if sub.discord_webhook_url:
            return (
                f":warning: **{sub.name}** has a webhook, which takes precedence. An admin "
                f"can clear it with `{self.prefix} user set {sub.name} webhook`.",
                {},
            )

        sub.discord_channel_id = value
        save_subscribers(self.cfg, self.watcher.subscribers)
        return self._reload(
            f":white_check_mark: **{sub.name}**'s listings will go to <#{value}>.\n"
            "*(The bot needs View Channel, Send Messages and Embed Links there.)*"
        )

    def _cmd_mention(self, rest: str, sub: Subscriber) -> tuple[str, dict]:
        """Be pinged on my listings, or not."""
        value = rest.strip().lower()
        if value in ("on", "yes", "true", "1"):
            sub.mention = True
        elif value in ("off", "no", "false", "0"):
            sub.mention = False
        else:
            state = "on" if sub.mention else "off"
            return f":grey_question: Mentions are **{state}**. Use `{self.prefix} mention off`.", {}

        save_subscribers(self.cfg, self.watcher.subscribers)
        word = "will @-mention you" if sub.mention else "won't ping you"
        return self._reload(f":white_check_mark: Listings for **{sub.name}** {word}.")

    # -- people -----------------------------------------------------------
    def _cmd_user(self, rest: str, _sub) -> tuple[str, dict]:
        action, _, argument = rest.partition(" ")
        action, argument = action.lower(), argument.strip()

        if action in ("", "list"):
            return self._users_text(), {}

        if action == "add":
            return self._user_add(argument)
        if action in ("remove", "rm", "delete"):
            return self._user_remove(argument)
        if action == "set":
            return self._user_set(argument)
        if action in ("enable", "disable"):
            return self._user_toggle(argument, action == "enable")
        return f":grey_question: Try `{self.prefix} user add <name>` or `{self.prefix} users`.", {}

    def _user_add(self, name: str) -> tuple[str, dict]:
        if not name:
            return f":grey_question: Usage: `{self.prefix} user add ana`", {}
        if find_subscriber(self.watcher.subscribers, name):
            return f":information_source: **{name}** already exists.", {}
        try:
            sub = Subscriber(name=name, enabled=True)
        except SubscriberError as exc:
            return f":x: {exc}", {}

        subs = list(self.watcher.subscribers) + [sub]
        save_subscribers(self.cfg, subs)
        ensure_keywords_file(self.cfg, sub)
        return self._reload(
            f":white_check_mark: Added **{name}**.\n"
            f"Next: `{self.prefix} user set {name} webhook <url>` "
            f"(or `telegram <chat id>`), then `{self.prefix} for {name} add <rule>`.\n"
            f"They get nothing until both a destination and at least one rule are set."
        )

    def _user_remove(self, name: str) -> tuple[str, dict]:
        sub = find_subscriber(self.watcher.subscribers, name)
        if sub is None:
            return f":x: No subscriber called `{name}`.", {}
        if sub.admin and sum(1 for s in self.watcher.subscribers if s.admin) == 1:
            return ":x: That's the only admin - make someone else an admin first.", {}
        remaining = [s for s in self.watcher.subscribers if s.name != sub.name]
        save_subscribers(self.cfg, remaining)
        self.watcher.store.drop_subscriber(sub.name)
        self.watcher.store.save()
        return self._reload(
            f":wastebasket: Removed **{sub.name}**. Their rules file "
            f"(`{sub.keywords_path(self.cfg).name}`) is left on disk."
        )

    def _user_set(self, argument: str) -> tuple[str, dict]:
        name, _, remainder = argument.partition(" ")
        field, _, value = remainder.strip().partition(" ")
        field, value = field.lower(), value.strip()

        sub = find_subscriber(self.watcher.subscribers, name)
        if sub is None:
            return f":x: No subscriber called `{name}`.", {}
        if not field:
            return (
                f":grey_question: Usage: `{self.prefix} user set {name} "
                "webhook|telegram|discord|admin <value>`",
                {},
            )

        if field in ("webhook", "discord_webhook", "url"):
            if value and not value.startswith("https://discord.com/api/webhooks/"):
                return ":x: That doesn't look like a Discord webhook URL.", {}
            sub.discord_webhook_url = value
            told = f"webhook {'set' if value else 'cleared'}"
        elif field in ("channel", "discord_channel", "discord_channel_id"):
            if value and not value.isdigit():
                return (
                    ":x: A channel id is a number — turn on Developer Mode, then "
                    "right-click the channel → Copy Channel ID.",
                    {},
                )
            if value and not (self.cfg.discord_bot_token or "").strip():
                return ":x: Posting into a channel needs `discord_bot_token` in config.json.", {}
            sub.discord_channel_id = value
            if value and sub.discord_webhook_url:
                told = (
                    f"channel set, but their webhook still wins — clear it with "
                    f"`{self.prefix} user set {sub.name} webhook`"
                )
            else:
                told = f"channel {'set' if value else 'cleared'}"
        elif field in ("telegram", "chat", "telegram_chat_id"):
            if value and not value.lstrip("-").isdigit():
                return ":x: A Telegram chat id is a number, e.g. `123456789`.", {}
            sub.telegram_chat_id = value
            told = f"Telegram chat {'set' if value else 'cleared'}"
        elif field in ("discord", "discord_user_id", "id"):
            if value and not value.isdigit():
                return ":x: A Discord user id is a number - right-click a user → Copy User ID.", {}
            sub.discord_user_id = value
            told = f"Discord account {'linked' if value else 'unlinked'}"
        elif field == "admin":
            sub.admin = value.lower() in ("1", "true", "yes", "on")
            told = f"admin = {sub.admin}"
        elif field in ("groups", "group"):
            sub.groups = [g.strip() for g in value.split(",") if g.strip()]
            told = f"groups = {', '.join(sub.groups) or 'all'}"
        else:
            return f":grey_question: Unknown field `{field}`.", {}

        save_subscribers(self.cfg, self.watcher.subscribers)
        return self._reload(f":white_check_mark: **{sub.name}**: {told}.")

    def _user_toggle(self, name: str, enable: bool) -> tuple[str, dict]:
        sub = find_subscriber(self.watcher.subscribers, name)
        if sub is None:
            return f":x: No subscriber called `{name}`.", {}
        sub.enabled = enable
        save_subscribers(self.cfg, self.watcher.subscribers)
        word = "enabled" if enable else "disabled"
        extra = ""
        if enable and not sub.deliverable:
            extra = f" — but still idle: {sub.why_idle()}"
        return self._reload(f":white_check_mark: **{sub.name}** {word}{extra}.")

    def _cmd_telegram(self, rest: str, _sub) -> tuple[str, dict]:
        action = (rest.split(" ")[0] if rest else "").lower()
        if action in ("ids", "chats", ""):
            if not (self.cfg.telegram_bot_token or "").strip():
                return (
                    ":x: No `telegram_bot_token` in config.json. Create a bot with "
                    "@BotFather on Telegram and paste the token there.",
                    {},
                )
            chats = recent_chats(self.cfg, session=self.session)
            if not chats:
                return (
                    ":mag: Nobody has messaged the Telegram bot recently. Ask them to open "
                    "it and send `/start`, then run this again.\n"
                    "*(Telegram won't let a bot message someone who hasn't written first.)*",
                    {},
                )
            listed = "\n".join(
                f"· `{c['chat_id']}` — {c['name']}"
                + (f" (@{c['username']})" if c["username"] else "")
                for c in chats
            )
            return (
                f"**Recent Telegram chats**\n{listed}\n\n"
                f"Link one with `{self.prefix} user set <name> telegram <chat id>`.",
                {},
            )
        return f":grey_question: Try `{self.prefix} telegram ids`.", {}

    # -- shared settings ---------------------------------------------------
    def _cmd_group(self, rest: str, _sub) -> tuple[str, dict]:
        action, _, argument = rest.partition(" ")
        action, argument = action.lower(), argument.strip()

        if action == "add":
            if not argument:
                return f":grey_question: Usage: `{self.prefix} group add <url> | Name`", {}
            try:
                group = parse_group_line(argument)
            except ValueError as exc:
                return f":x: {exc}", {}
            if group is None:
                return ":x: That doesn't look like a group.", {}
            if any(g.slug == group.slug for g in load_groups(self.cfg.groups_path)):
                return f":information_source: **{group.name}** is already watched.", {}
            _append_line(self.cfg.groups_path, argument)
            return self._reload(f":white_check_mark: Now watching **{group.name}**.")

        if action in ("remove", "rm", "delete"):
            if not argument:
                return f":grey_question: Usage: `{self.prefix} group remove <name or id>`", {}
            needle = argument.lower()
            for group in load_groups(self.cfg.groups_path):
                if needle in (group.slug.lower(), group.name.lower()):
                    _remove_line(self.cfg.groups_path, predicate=lambda ln: group.slug in ln)
                    return self._reload(f":wastebasket: Stopped watching **{group.name}**.")
            return f":x: No group matches `{argument}`.", {}

        return self._groups_text(), {}

    def _cmd_pause(self, rest: str, _sub) -> tuple[str, dict]:
        if self.watcher.paused:
            return ":information_source: Already paused.", {}
        self.watcher.paused = True
        return (
            ":pause_button: Paused for everyone. New posts are still recorded, so nobody "
            f"gets a flood when you `{self.prefix} resume`.",
            {},
        )

    def _cmd_resume(self, rest: str, _sub) -> tuple[str, dict]:
        if not self.watcher.paused:
            return ":information_source: Not paused.", {}
        self.watcher.paused = False
        return ":green_circle: Resumed. Only posts from now on will be sent.", {}

    def _cmd_check(self, rest: str, _sub) -> tuple[str, dict]:
        return ":arrows_counterclockwise: Checking every group now...", {"force_check": True}

    def _cmd_interval(self, rest: str, _sub) -> tuple[str, dict]:
        try:
            seconds = int(rest.strip())
        except ValueError:
            return f":grey_question: Usage: `{self.prefix} interval 300`", {}
        if seconds < 60:
            return ":x: Minimum is 60 seconds — polling harder than that gets you blocked.", {}
        self.cfg.poll_interval_seconds = seconds
        return (
            f":stopwatch: Polling every **{seconds}s** from the next cycle.\n"
            "*(This lasts until restart; edit `config.json` to make it permanent.)*",
            {},
        )

    # -- helpers -----------------------------------------------------------
    def _reload(self, message: str) -> tuple[str, dict]:
        """Apply an edit immediately, reporting any problem it introduced."""
        try:
            self.watcher.reload_inputs()
        except (ValueError, KeywordSyntaxError, SubscriberError, FileNotFoundError) as exc:
            return f":warning: Saved, but there's now a problem: {exc}", {}
        return message, {}


# ---------------------------------------------------------------------------
def _find_line(path: Path, wanted: str) -> int | None:
    if not path.exists():
        return None
    wanted = wanted.strip().lower()
    for index, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = raw.strip()
        if line and not line.startswith("#") and line.lower() == wanted:
            return index
    return None


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(f"{text}{line.strip()}\n", encoding="utf-8")


def _remove_line(path: Path, wanted: str | None = None, predicate=None) -> str | None:
    """Delete the first matching non-comment line.  Returns what was removed."""
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    target = wanted.strip().lower() if wanted else None

    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        hit = predicate(line) if predicate else (line.lower() == target)
        if hit:
            del lines[index]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return line
    return None
