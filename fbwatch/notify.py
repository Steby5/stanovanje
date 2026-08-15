"""Discord webhook delivery."""

from __future__ import annotations

import logging
import time

import requests

from .matcher import MatchResult
from .models import Post
from .textutil import truncate

log = logging.getLogger(__name__)

API_ROOT = "https://discord.com/api/v10"

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

    def build_shared_embed(self, post: Post, recipients: list[tuple]) -> dict:
        """Embed for one post going to a channel several people share.

        With a single recipient this is the ordinary embed.  With more, the
        per-person "Matched" line is replaced by a breakdown, so everyone can
        see at a glance whether the post is for them.
        """
        if len(recipients) == 1:
            return self.build_embed(post, recipients[0][1])

        embed = self.build_embed(post, MatchResult(matched=True))
        lines = []
        for sub, result in recipients:
            who = f"<@{sub.discord_user_id}>" if sub.discord_user_id else f"**{sub.name}**"
            rules = ", ".join(f"`{r}`" for r in result.matched_rules) or "—"
            lines.append(f"{who} — {rules}")
        embed["fields"].insert(
            0,
            {
                "name": f"Matched for {len(recipients)}",
                "value": truncate("\n".join(lines), MAX_FIELD_VALUE),
                "inline": False,
            },
        )
        return embed

    # -- sending --------------------------------------------------------
    def send_post(self, post: Post, result: MatchResult) -> bool:
        if not self.enabled:
            log.warning("No Discord webhook configured - not sending %s", post.url or post.post_id)
            return False
        content = post.url or None  # gives a clickable link even if embeds are off
        return self._post({"embeds": [self.build_embed(post, result)], "content": content})

    def send_post_to(self, post: Post, recipients: list[tuple]) -> bool:
        """Send one message to this webhook, @-mentioning whoever it matched.

        `recipients` is [(Subscriber, MatchResult)] for people sharing this
        webhook.  Only their ids are allowed as mentions, so a post whose text
        contains "@everyone" still cannot ping the server.
        """
        if not self.enabled:
            log.warning("No Discord webhook configured - not sending %s", post.url or post.post_id)
            return False

        mention_ids = [
            sub.discord_user_id for sub, _ in recipients if sub.discord_user_id and sub.mention
        ]
        mentions = " ".join(f"<@{uid}>" for uid in mention_ids)
        content = f"{mentions}\n{post.url}".strip() if mentions else (post.url or None)

        return self._post({
            "embeds": [self.build_shared_embed(post, recipients)],
            "content": content,
            "allowed_mentions": {"parse": [], "users": mention_ids},
        })

    def send_text(self, message: str) -> bool:
        if not self.enabled:
            return False
        return self._post({"content": truncate(message, 1900)})

    # -- transport (overridden to post as the bot instead) ---------------
    @property
    def _endpoint(self) -> str:
        return self.webhook_url

    def _headers(self) -> dict:
        return {}

    def _decorate(self, payload: dict) -> None:
        """Webhooks can set a per-message name and avatar; bots cannot."""
        payload.setdefault("username", self.username)
        if self.avatar_url:
            payload.setdefault("avatar_url", self.avatar_url)

    def _post(self, payload: dict, attempts: int = 4) -> bool:
        self._decorate(payload)
        payload.setdefault("allowed_mentions", {"parse": []})

        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                resp = self.session.post(
                    self._endpoint, json=payload, headers=self._headers(), timeout=20
                )
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


class DiscordBotNotifier(DiscordNotifier):
    """Posts to a channel as the bot, instead of through a webhook.

    Useful once the bot is already in the server for commands: a channel id is
    not a secret, so nothing sensitive has to sit in subscribers.json, and there
    is no webhook to create per person.  The bot needs View Channel, Send
    Messages and Embed Links in that channel - the last one is easy to miss,
    because webhooks never needed it.
    """

    def __init__(self, cfg, channel_id: str, session: requests.Session | None = None):
        super().__init__(cfg, webhook_url="", session=session)
        self.channel_id = str(channel_id or "").strip()
        self.token = (cfg.discord_bot_token or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.channel_id)

    @property
    def _endpoint(self) -> str:
        return f"{API_ROOT}/channels/{self.channel_id}/messages"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bot {self.token}",
            "User-Agent": "fbwatch (https://localhost, 1.0)",
        }

    def _decorate(self, payload: dict) -> None:
        # A bot posts under its own name; username/avatar are webhook-only and
        # Discord rejects them here.
        payload.pop("username", None)
        payload.pop("avatar_url", None)
