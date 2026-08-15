"""Persistent record of what each subscriber has already been notified about.

State is keyed by subscriber, not just by group: two people watching the same
group have independent histories, so adding someone later doesn't replay the
backlog at them, and a failed delivery to one doesn't affect the other.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

SCHEMA_VERSION = 2
MAX_IDS_PER_GROUP = 4000


class SeenStore:
    """subscriber -> group slug -> {post id: unix timestamp first seen}."""

    def __init__(self, path: Path, retention_days: int = 30):
        self.path = Path(path)
        self.retention_days = retention_days
        self._subs: dict[str, dict[str, dict[str, float]]] = {}
        self._legacy: dict[str, dict[str, float]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A truncated state file must not stop the watcher.  Keep the old
            # file around for inspection and start clean.
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return
        if not isinstance(data, dict):
            return

        if isinstance(data.get("subscribers"), dict):
            for name, groups in data["subscribers"].items():
                if isinstance(groups, dict):
                    self._subs[name] = _clean_groups(groups)
        elif isinstance(data.get("groups"), dict):
            # Version 1 was single-user: one flat group -> posts mapping.
            self._legacy = _clean_groups(data["groups"])

    # -- migration -------------------------------------------------------
    def adopt_legacy(self, subscriber: str) -> int:
        """Hand pre-multi-user state to whoever inherits it.

        Called once at startup with the primary subscriber's name.  Without
        this, upgrading would look like a first run and re-notify everything.
        """
        if not self._legacy:
            return 0
        target = self._subs.setdefault(subscriber, {})
        moved = 0
        for slug, posts in self._legacy.items():
            existing = target.setdefault(slug, {})
            for post_id, when in posts.items():
                existing.setdefault(post_id, when)
                moved += 1
        self._legacy = {}
        self._dirty = True
        return moved

    # -- queries ---------------------------------------------------------
    def knows_group(self, subscriber: str, slug: str) -> bool:
        """True if this person has been polled for this group before."""
        return bool(self._subs.get(subscriber, {}).get(slug))

    def has(self, subscriber: str, slug: str, post_id: str) -> bool:
        return post_id in self._subs.get(subscriber, {}).get(slug, {})

    def add(self, subscriber: str, slug: str, post_id: str, when: float | None = None) -> None:
        posts = self._subs.setdefault(subscriber, {}).setdefault(slug, {})
        posts[post_id] = when if when is not None else time.time()
        self._dirty = True

    def forget(self, subscriber: str, slug: str, post_id: str) -> None:
        """Un-see a post so the next cycle retries it after a failed delivery."""
        posts = self._subs.get(subscriber, {}).get(slug, {})
        if posts.pop(post_id, None) is not None:
            self._dirty = True

    def drop_subscriber(self, subscriber: str) -> None:
        if self._subs.pop(subscriber, None) is not None:
            self._dirty = True

    def count(self, subscriber: str | None = None) -> int:
        if subscriber is not None:
            return sum(len(v) for v in self._subs.get(subscriber, {}).values())
        return sum(len(g) for s in self._subs.values() for g in s.values())

    # -- housekeeping ----------------------------------------------------
    def prune(self) -> int:
        """Drop entries older than the retention window and cap group size."""
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for groups in self._subs.values():
            for slug, posts in list(groups.items()):
                fresh = {pid: ts for pid, ts in posts.items() if ts >= cutoff}
                if len(fresh) > MAX_IDS_PER_GROUP:
                    newest = sorted(fresh.items(), key=lambda kv: kv[1], reverse=True)
                    fresh = dict(newest[:MAX_IDS_PER_GROUP])
                removed += len(posts) - len(fresh)
                groups[slug] = fresh
        if removed:
            self._dirty = True
        return removed

    def save(self, force: bool = False) -> None:
        """Atomic write, so a crash mid-save cannot corrupt the state file."""
        if not (self._dirty or force):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "updated": time.time(),
            "subscribers": self._subs,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        self._dirty = False


def _clean_groups(raw: dict) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for slug, posts in raw.items():
        if isinstance(posts, dict):
            out[str(slug)] = {
                str(pid): float(ts)
                for pid, ts in posts.items()
                if isinstance(ts, (int, float))
            }
    return out
