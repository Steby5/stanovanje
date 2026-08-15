"""Routing a post to one person's destinations.

A subscriber can have a Discord webhook, a Telegram chat, or both.  A delivery
counts as successful if at least one destination accepted it, so a broken
Telegram chat id doesn't cost someone their Discord notifications.
"""

from __future__ import annotations

import logging

import requests

from .matcher import MatchResult
from .models import Post
from .notify import DiscordNotifier
from .subscribers import Subscriber
from .telegram import TelegramNotifier

log = logging.getLogger(__name__)


class Mailbox:
    """Everywhere one subscriber should be notified."""

    def __init__(self, cfg, sub: Subscriber, session: requests.Session | None = None):
        self.cfg = cfg
        self.name = sub.name
        self.channels: list = []

        if sub.discord_webhook_url:
            self.channels.append(
                DiscordNotifier(cfg, webhook_url=sub.discord_webhook_url, session=session)
            )
        if sub.telegram_chat_id and (cfg.telegram_bot_token or "").strip():
            self.channels.append(TelegramNotifier(cfg, sub.telegram_chat_id, session=session))
        elif sub.telegram_chat_id:
            log.warning(
                "%s has a Telegram chat id but no telegram_bot_token is configured",
                sub.name,
            )

    @property
    def enabled(self) -> bool:
        return any(getattr(c, "enabled", False) for c in self.channels)

    def send_post(self, post: Post, result: MatchResult) -> bool:
        delivered = False
        for channel in self.channels:
            try:
                if channel.send_post(post, result):
                    delivered = True
            except Exception as exc:  # noqa: BLE001 - one channel must not break another
                log.exception("%s: %s delivery failed: %s", self.name, type(channel).__name__, exc)
        return delivered

    def send_text(self, message: str) -> bool:
        delivered = False
        for channel in self.channels:
            try:
                if channel.send_text(message):
                    delivered = True
            except Exception as exc:  # noqa: BLE001
                log.exception("%s: %s delivery failed: %s", self.name, type(channel).__name__, exc)
        return delivered

    def describe(self) -> str:
        return ", ".join(
            "Discord" if isinstance(c, DiscordNotifier) else "Telegram" for c in self.channels
        ) or "nowhere"
