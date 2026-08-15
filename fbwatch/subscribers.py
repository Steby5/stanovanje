"""Per-user subscriptions: who gets notified, about what, and where.

Facebook is scraped once per cycle no matter how many people are subscribed -
the posts are then fanned out, each person filtered by their own trigger words
and delivered to their own Discord webhook and/or Telegram chat.

Subscribers live in `subscribers.json`.  When that file doesn't exist the
watcher runs in single-user mode off `config.json` and `keywords.txt`, so an
existing setup keeps working untouched.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

from .matcher import KeywordMatcher, KeywordSyntaxError

log = logging.getLogger(__name__)

DEFAULT_NAME = "me"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$", re.I)


class SubscriberError(ValueError):
    pass


@dataclass
class Subscriber:
    """One person receiving notifications."""

    name: str
    enabled: bool = True
    admin: bool = False
    keywords_file: str = ""
    discord_webhook_url: str = ""
    telegram_chat_id: str = ""
    discord_user_id: str = ""
    # Which groups they care about.  Empty means all of them.
    groups: list = field(default_factory=list)

    # Filled in at load time, not persisted.
    matcher: KeywordMatcher | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not NAME_RE.match(self.name or ""):
            raise SubscriberError(
                f"{self.name!r} is not a usable name - use letters, digits, dot, dash "
                "or underscore (max 32 characters)"
            )
        self.telegram_chat_id = str(self.telegram_chat_id or "").strip()
        self.discord_user_id = str(self.discord_user_id or "").strip()
        self.discord_webhook_url = (self.discord_webhook_url or "").strip()

    # -- files ----------------------------------------------------------
    def keywords_path(self, cfg) -> Path:
        """Where this person's trigger words live."""
        if self.keywords_file:
            return cfg.path(self.keywords_file)
        return cfg.path(f"keywords/{self.name}.txt")

    def load_matcher(self, cfg) -> None:
        """Read the trigger words, tolerating a missing file."""
        path = self.keywords_path(cfg)
        if not path.exists():
            self.matcher = KeywordMatcher([])
            return
        self.matcher = KeywordMatcher.from_file(path)

    # -- routing --------------------------------------------------------
    def watches(self, group) -> bool:
        if not self.groups:
            return True
        wanted = {str(g).strip().lower() for g in self.groups}
        return group.slug.lower() in wanted or group.name.lower() in wanted

    @property
    def destinations(self) -> list[str]:
        out = []
        if self.discord_webhook_url:
            out.append("discord")
        if self.telegram_chat_id:
            out.append("telegram")
        return out

    @property
    def has_destination(self) -> bool:
        return bool(self.destinations)

    @property
    def has_triggers(self) -> bool:
        """No trigger words means no notifications.

        Deliberately not "match everything": a newly added person would
        otherwise be buried under every post in every group.
        """
        return bool(self.matcher and self.matcher.includes)

    @property
    def deliverable(self) -> bool:
        return self.enabled and self.has_destination and self.has_triggers

    def why_idle(self) -> str:
        """Explain why this person isn't getting anything, for logs and Discord."""
        if not self.enabled:
            return "disabled"
        if not self.has_destination:
            return "no Discord webhook or Telegram chat id set"
        if not self.has_triggers:
            return "no trigger words set"
        return ""

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        skip = {"name", "matcher"}
        out = {}
        for f in fields(self):
            if f.name in skip:
                continue
            value = getattr(self, f.name)
            # Keep the file tidy: omit empty optional values.
            if value in ("", [], None) and f.name not in ("enabled",):
                continue
            out[f.name] = value
        return out


# ---------------------------------------------------------------------------
def _implicit_subscriber(cfg) -> Subscriber:
    """Single-user mode: one admin built from config.json + keywords.txt."""
    sub = Subscriber(
        name=DEFAULT_NAME,
        admin=True,
        keywords_file=cfg.keywords_file,
        discord_webhook_url=cfg.discord_webhook_url,
    )
    sub.load_matcher(cfg)
    return sub


def load_subscribers(cfg) -> list[Subscriber]:
    """Read subscribers.json, or synthesise the single-user default."""
    path = cfg.subscribers_path
    if not path.exists():
        return [_implicit_subscriber(cfg)]

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SubscriberError(f"{path} is not valid JSON: {exc}") from exc

    if isinstance(data, dict) and isinstance(data.get("subscribers"), dict):
        data = data["subscribers"]
    if not isinstance(data, dict):
        raise SubscriberError(f"{path} must be a JSON object of name -> settings")

    known = {f.name for f in fields(Subscriber)} - {"name", "matcher"}
    subscribers: list[Subscriber] = []
    for name, raw in data.items():
        if name.startswith("_"):
            continue  # a note to the reader
        if not isinstance(raw, dict):
            raise SubscriberError(f"{path}: entry {name!r} must be an object")
        unknown = sorted(k for k in raw if k not in known and not k.startswith("_"))
        if unknown:
            raise SubscriberError(
                f"{path}: {name!r} has unknown setting(s): {', '.join(unknown)}. "
                f"Valid: {', '.join(sorted(known))}"
            )
        sub = Subscriber(name=name, **{k: v for k, v in raw.items() if k in known})

        # Environment override, so a webhook need not sit in the file:
        # FBWATCH_WEBHOOK_ANA / FBWATCH_TELEGRAM_ANA
        env_hook = os.environ.get(f"FBWATCH_WEBHOOK_{name.upper()}", "").strip()
        if env_hook:
            sub.discord_webhook_url = env_hook
        env_chat = os.environ.get(f"FBWATCH_TELEGRAM_{name.upper()}", "").strip()
        if env_chat:
            sub.telegram_chat_id = env_chat

        sub.load_matcher(cfg)
        subscribers.append(sub)

    if not subscribers:
        raise SubscriberError(f"{path} has no subscribers in it")

    linked = [s.discord_user_id for s in subscribers if s.discord_user_id]
    if len(linked) != len(set(linked)):
        raise SubscriberError(f"{path}: two subscribers share the same discord_user_id")

    return subscribers


def save_subscribers(cfg, subscribers: list[Subscriber]) -> None:
    """Write subscribers.json atomically."""
    path = cfg.subscribers_path
    payload = {
        "_comment": (
            "Who gets notified, about what, and where. Each person has their own "
            "trigger-word file and their own Discord webhook and/or Telegram chat id."
        ),
    }
    payload.update({sub.name: sub.to_dict() for sub in subscribers})

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def ensure_keywords_file(cfg, sub: Subscriber) -> Path:
    """Create an empty, commented trigger-word file for a new person."""
    path = sub.keywords_path(cfg)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# Trigger words for {sub.name}.\n"
        "# One rule per line:  soba  |  oddam + ljubljana  |  \"exact phrase\"\n"
        "#                     =wholeword  |  re:<regex>  |  !exclude-this\n"
        "#\n"
        "# Until there is at least one rule here, this person gets nothing.\n",
        encoding="utf-8",
    )
    return path


def find_subscriber(subscribers: list[Subscriber], needle: str) -> Subscriber | None:
    needle = (needle or "").strip().lower()
    for sub in subscribers:
        if sub.name.lower() == needle:
            return sub
    return None


def find_by_discord_id(subscribers: list[Subscriber], user_id: str) -> Subscriber | None:
    user_id = str(user_id or "").strip()
    if not user_id:
        return None
    for sub in subscribers:
        if sub.discord_user_id == user_id:
            return sub
    return None
