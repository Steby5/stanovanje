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
from .notify import DiscordNotifier
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

        for sub in subscribers:
            if sub.discord_webhook_url and sub.discord_webhook_url not in self._discord:
                self._discord[sub.discord_webhook_url] = DiscordNotifier(
                    cfg, webhook_url=sub.discord_webhook_url, session=session
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
            if sub.discord_webhook_url:
                batches.setdefault(sub.discord_webhook_url, []).append((sub, result))

        for webhook_url, recipients in batches.items():
            notifier = self._discord.get(webhook_url)
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

    # -- reporting --------------------------------------------------------
    def describe(self, sub: Subscriber) -> str:
        """Where this person's notifications go, for logs and `list`."""
        where = []
        if sub.discord_webhook_url:
            shared = self.shared_with(sub)
            where.append(f"Discord (shared with {shared})" if shared else "Discord")
        if sub.telegram_chat_id and self._has_telegram_token:
            where.append("Telegram")
        return ", ".join(where) or "nowhere"

    def shared_with(self, sub: Subscriber) -> int:
        """How many other people read the same Discord channel."""
        if not sub.discord_webhook_url:
            return 0
        return max(0, self._readers(sub.discord_webhook_url) - 1)

    def _readers(self, webhook_url: str) -> int:
        return sum(1 for s in self._subscribers if s.discord_webhook_url == webhook_url)
