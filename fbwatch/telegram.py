"""Telegram delivery.

Notifications only - Telegram is a one-way destination here, and configuration
stays in Discord.  One bot serves everyone; each person is identified by their
own chat id.
"""

from __future__ import annotations

import html
import logging
import time

import requests

from .matcher import MatchResult
from .models import Post
from .textutil import truncate

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"

# Telegram allows 4096 characters per message; leave room for the header.
MAX_BODY = 3200

MIN_SECONDS_BETWEEN_SENDS = 1.1


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


class TelegramNotifier:
    """Sends one person's notifications to their Telegram chat."""

    def __init__(self, cfg, chat_id: str, session: requests.Session | None = None):
        self.token = (cfg.telegram_bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.session = session or requests.Session()
        self._last_send = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    # -- formatting ------------------------------------------------------
    def build_message(self, post: Post, result: MatchResult) -> str:
        header = f"<b>{_esc(post.group_name or 'Facebook')}</b>"
        if post.author:
            header += f" — {_esc(post.author)}"

        body = truncate(post.text.strip(), MAX_BODY) or "<i>(no text — open the post)</i>"
        lines = [header, "", _esc(body) if post.text.strip() else body]

        if result.matched_rules:
            lines.append("")
            lines.append(f"<i>Matched: {_esc(', '.join(result.matched_rules))}</i>")
        if post.timestamp:
            lines.append(f"<i>Posted: {_esc(post.timestamp)}</i>")
        if post.url:
            lines.append("")
            lines.append(f'<a href="{_esc(post.url)}">Open post on Facebook</a>')
        return "\n".join(lines)

    # -- sending ---------------------------------------------------------
    def send_post(self, post: Post, result: MatchResult) -> bool:
        if not self.enabled:
            return False
        return self._send(self.build_message(post, result), preview=bool(post.images))

    def send_text(self, message: str) -> bool:
        if not self.enabled:
            return False
        return self._send(_esc(truncate(message, MAX_BODY)))

    def _send(self, text: str, preview: bool = False, attempts: int = 4) -> bool:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        }
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                resp = self.session.post(
                    f"{API_ROOT}/bot{self.token}/sendMessage", json=payload, timeout=20
                )
            except requests.RequestException as exc:
                log.warning("Telegram request failed (%d/%d): %s", attempt, attempts, exc)
                time.sleep(min(2**attempt, 30))
                continue

            if resp.status_code == 200:
                return True

            if resp.status_code == 429:
                wait = 5.0
                try:
                    wait = float(resp.json()["parameters"]["retry_after"])
                except (ValueError, KeyError, TypeError, requests.exceptions.JSONDecodeError):
                    pass
                log.info("Telegram rate limited, waiting %.1fs", wait)
                time.sleep(min(wait + 0.5, 60))
                continue

            if 400 <= resp.status_code < 500:
                # Wrong chat id, bot blocked, or the user never messaged it.
                log.error(
                    "Telegram rejected the message for chat %s (%s): %s",
                    self.chat_id, resp.status_code, resp.text[:300],
                )
                return False

            log.warning("Telegram server error %s (%d/%d)", resp.status_code, attempt, attempts)
            time.sleep(min(2**attempt, 30))

        log.error("Giving up on a Telegram message after %d attempts", attempts)
        return False

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_send
        if elapsed < MIN_SECONDS_BETWEEN_SENDS:
            time.sleep(MIN_SECONDS_BETWEEN_SENDS - elapsed)
        self._last_send = time.monotonic()


# ---------------------------------------------------------------------------
def verify_token(cfg, session: requests.Session | None = None) -> tuple[bool, str]:
    """Check the bot token.  Returns (ok, bot username or error)."""
    token = (cfg.telegram_bot_token or "").strip()
    if not token:
        return False, "no telegram_bot_token configured"
    session = session or requests.Session()
    try:
        resp = session.get(f"{API_ROOT}/bot{token}/getMe", timeout=15)
    except requests.RequestException as exc:
        return False, f"could not reach Telegram: {exc}"
    if resp.status_code != 200:
        return False, f"Telegram returned {resp.status_code}: {resp.text[:200]}"
    return True, resp.json().get("result", {}).get("username", "?")


def recent_chats(cfg, session: requests.Session | None = None) -> list[dict]:
    """List people who recently messaged the bot, so they can be linked.

    Telegram will not let a bot message someone who has never written to it
    first, so this doubles as the check that a new person did their part.
    """
    token = (cfg.telegram_bot_token or "").strip()
    if not token:
        return []
    session = session or requests.Session()
    try:
        resp = session.get(f"{API_ROOT}/bot{token}/getUpdates", timeout=20)
    except requests.RequestException as exc:
        log.error("Could not read Telegram updates: %s", exc)
        return []
    if resp.status_code != 200:
        log.error("Telegram getUpdates failed (%s): %s", resp.status_code, resp.text[:200])
        return []

    seen: dict[str, dict] = {}
    for update in resp.json().get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", "")).strip()
        if not chat_id:
            continue
        name = " ".join(
            part for part in (chat.get("first_name"), chat.get("last_name")) if part
        ) or chat.get("title", "")
        seen[chat_id] = {
            "chat_id": chat_id,
            "name": name or "(no name)",
            "username": chat.get("username", ""),
        }
    return list(seen.values())
