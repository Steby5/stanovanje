"""Getting one post to everyone it matched.

Delivery is post-centric rather than person-centric, because several people can
share a Discord channel.  When they do, the post is sent there **once** and each
matching person is @-mentioned, instead of the same listing arriving twice.
People with their own webhook are simply a group of one, so nothing changes for
them.

Telegram is always per-person - a chat has exactly one reader.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

import requests

from .models import Post
from .notify import DiscordBotNotifier, DiscordNotifier
from .subscribers import Subscriber
from .telegram import TelegramNotifier

log = logging.getLogger(__name__)


class Dispatcher:
    """Routes posts to subscribers, sharing a message where they share a channel."""

    def __init__(self, cfg, subscribers: list[Subscriber], session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session
        self._subscribers = list(subscribers)
        self._discord: dict[str, DiscordNotifier] = {}
        self._telegram: dict[str, TelegramNotifier] = {}
        self._has_telegram_token = bool((cfg.telegram_bot_token or "").strip())
        self._has_bot_token = bool((cfg.discord_bot_token or "").strip())

        for sub in subscribers:
            key = self.target_of(sub)
            if key and key not in self._discord:
                if key.startswith("webhook:"):
                    self._discord[key] = DiscordNotifier(
                        cfg, webhook_url=sub.discord_webhook_url, session=session
                    )
                else:
                    self._discord[key] = DiscordBotNotifier(
                        cfg, sub.effective_channel_id, session=session
                    )
            if sub.discord_channel_id and not self._has_bot_token:
                log.warning(
                    "%s has a Discord channel id but no discord_bot_token is configured",
                    sub.name,
                )
            if sub.telegram_chat_id:
                if self._has_telegram_token:
                    self._telegram[sub.name] = TelegramNotifier(
                        cfg, sub.telegram_chat_id, session=session
                    )
                else:
                    log.warning(
                        "%s has a Telegram chat id but no telegram_bot_token is configured",
                        sub.name,
                    )

    # -- delivery ---------------------------------------------------------
    def deliver(self, post: Post, matches: list[tuple]) -> set[str]:
        """Send one post to everyone it matched.

        `matches` is [(Subscriber, MatchResult)].  Returns the names actually
        delivered to, so the caller can retry the rest on the next cycle.
        """
        delivered: set[str] = set()

        # One message per Discord channel, however many people read it.
        batches: dict[str, list] = OrderedDict()
        for sub, result in matches:
            key = self.target_of(sub)
            if key:
                batches.setdefault(key, []).append((sub, result))

        for key, recipients in batches.items():
            notifier = self._discord.get(key)
            if notifier is None:
                continue
            try:
                if notifier.send_post_to(post, recipients):
                    delivered.update(sub.name for sub, _ in recipients)
            except Exception as exc:  # noqa: BLE001 - one channel must not break another
                log.exception("Discord delivery failed for %d recipient(s): %s",
                              len(recipients), exc)

        # Telegram is inherently one reader per chat.
        for sub, result in matches:
            notifier = self._telegram.get(sub.name)
            if notifier is None:
                continue
            try:
                if notifier.send_post(post, result):
                    delivered.add(sub.name)
            except Exception as exc:  # noqa: BLE001
                log.exception("Telegram delivery failed for %s: %s", sub.name, exc)

        return delivered

    # -- routing ----------------------------------------------------------
    def target_of(self, sub: Subscriber) -> str | None:
        """The Discord channel this person is served by, as a grouping key.

        A webhook URL and a channel id both identify one channel; two people
        with the same key share a message.  A webhook wins if both are set,
        since it works without the bot being present.
        """
        if sub.discord_webhook_url:
            return f"webhook:{sub.discord_webhook_url}"
        if sub.effective_channel_id and self._has_bot_token:
            return f"channel:{sub.effective_channel_id}"
        return None

    # -- reporting --------------------------------------------------------
    def describe(self, sub: Subscriber) -> str:
        """Where this person's notifications go, for logs and `list`."""
        where = []
        key = self.target_of(sub)
        if key:
            how = ("Discord" if key.startswith("webhook:")
                   else "this channel" if sub.uses_default_channel else "Discord (bot)")
            shared = self.shared_with(sub)
            where.append(f"{how} — shared with {shared}" if shared else how)
        if sub.telegram_chat_id and self._has_telegram_token:
            where.append("Telegram")
        return ", ".join(where) or "nowhere"

    def shared_with(self, sub: Subscriber) -> int:
        """How many other people read the same Discord channel."""
        key = self.target_of(sub)
        if not key:
            return 0
        return max(0, sum(1 for s in self._subscribers if self.target_of(s) == key) - 1)
