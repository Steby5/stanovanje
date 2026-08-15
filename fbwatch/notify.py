"""Discord webhook delivery."""

from __future__ import annotations

import logging
import time

import requests

from .matcher import MatchResult
from .models import Post
from .textutil import truncate

log = logging.getLogger(__name__)

# Discord's documented limits, with a little headroom.
MAX_DESCRIPTION = 4000
MAX_TITLE = 250
MAX_FIELD_VALUE = 1000

# Webhooks allow roughly 5 requests per 2 seconds; stay well under it.
MIN_SECONDS_BETWEEN_SENDS = 1.3


class DiscordNotifier:
    """Sends notifications to one Discord webhook.

    `webhook_url` overrides the one in config.json, which is how each
    subscriber gets their own channel.
    """

    def __init__(self, cfg, webhook_url: str | None = None, session: requests.Session | None = None):
        self.webhook_url = (
            webhook_url if webhook_url is not None else cfg.discord_webhook_url or ""
        ).strip()
        self.username = cfg.discord_username
        self.avatar_url = cfg.discord_avatar_url
        self.color = cfg.embed_color
        self.include_images = cfg.include_images
        self.session = session or requests.Session()
        self._last_send = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    # -- payload building -----------------------------------------------
    def build_embed(self, post: Post, result: MatchResult) -> dict:
        text = post.text.strip() or "_(post has no text - open it on Facebook)_"

        title = post.group_name or "Facebook"
        if post.author:
            title = f"{title} — {post.author}"

        embed: dict = {
            "title": truncate(title, MAX_TITLE),
            "description": truncate(text, MAX_DESCRIPTION),
            "color": self.color,
            "fields": [],
        }
        if post.url:
            embed["url"] = post.url

        if result.matched_rules:
            embed["fields"].append(
                {
                    "name": "Matched",
                    "value": truncate(
                        ", ".join(f"`{r}`" for r in result.matched_rules), MAX_FIELD_VALUE
                    ),
                    "inline": False,
                }
            )
        if post.url:
            embed["fields"].append(
                {"name": "Link", "value": f"[Open post]({post.url})", "inline": True}
            )
        if post.timestamp:
            embed["fields"].append(
                {"name": "Posted", "value": truncate(post.timestamp, 100), "inline": True}
            )

        if post.author and post.author_url:
            embed["author"] = {"name": truncate(post.author, MAX_TITLE), "url": post.author_url}
        elif post.author:
            embed["author"] = {"name": truncate(post.author, MAX_TITLE)}

        if self.include_images and post.images:
            embed["image"] = {"url": post.images[0]}

        embed["footer"] = {"text": truncate(post.group_name or "Facebook group", 100)}
        embed["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        return embed

    # -- sending --------------------------------------------------------
    def send_post(self, post: Post, result: MatchResult) -> bool:
        if not self.enabled:
            log.warning("No Discord webhook configured - not sending %s", post.url or post.post_id)
            return False
        content = post.url or None  # gives a clickable link even if embeds are off
        return self._post({"embeds": [self.build_embed(post, result)], "content": content})

    def send_text(self, message: str) -> bool:
        if not self.enabled:
            return False
        return self._post({"content": truncate(message, 1900)})

    def _post(self, payload: dict, attempts: int = 4) -> bool:
        payload.setdefault("username", self.username)
        if self.avatar_url:
            payload.setdefault("avatar_url", self.avatar_url)
        payload.setdefault("allowed_mentions", {"parse": []})

        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                resp = self.session.post(self.webhook_url, json=payload, timeout=20)
            except requests.RequestException as exc:
                log.warning("Discord request failed (attempt %d/%d): %s", attempt, attempts, exc)
                time.sleep(min(2**attempt, 30))
                continue

            if resp.status_code in (200, 204):
                return True

            if resp.status_code == 429:
                retry_after = 5.0
                try:
                    retry_after = float(resp.json().get("retry_after", retry_after))
                except (ValueError, requests.exceptions.JSONDecodeError):
                    pass
                log.info("Discord rate limited, waiting %.1fs", retry_after)
                time.sleep(min(retry_after + 0.5, 60))
                continue

            if 400 <= resp.status_code < 500:
                # Bad webhook URL or malformed embed - retrying will not help.
                log.error(
                    "Discord rejected the message (%s): %s",
                    resp.status_code,
                    resp.text[:400],
                )
                return False

            log.warning("Discord server error %s (attempt %d/%d)", resp.status_code, attempt, attempts)
            time.sleep(min(2**attempt, 30))

        log.error("Giving up on a Discord message after %d attempts", attempts)
        return False

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_send
        if elapsed < MIN_SECONDS_BETWEEN_SENDS:
            time.sleep(MIN_SECONDS_BETWEEN_SENDS - elapsed)
        self._last_send = time.monotonic()
