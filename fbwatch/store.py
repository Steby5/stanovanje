"""Persistent record of what each subscriber has already been notified about.

State is keyed by subscriber, not just by group: two people watching the same
group have independent histories, so adding someone later doesn't replay the
backlog at them, and a failed delivery to one doesn't affect the other.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
MAX_IDS_PER_GROUP = 4000


def _replace_with_retry(tmp: Path, target: Path, attempts: int = 3) -> None:
    """os.replace, retried briefly.

    On Windows an antivirus scanner or the search indexer can hold the
    destination open for a moment, which surfaces as PermissionError on an
    otherwise fine write.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


class SeenStore:
    """subscriber -> group slug -> {post id: unix timestamp first seen}."""

    def __init__(self, path: Path, retention_days: int = 30):
        self.path = Path(path)
        self.retention_days = retention_days
        # The watch loop and the Discord control thread both reach in here:
        # the loop records posts while a command may be removing a subscriber,
        # and save() walks the whole structure while that happens.
        self._lock = threading.RLock()
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
        with self._lock:
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
        with self._lock:
            return bool(self._subs.get(subscriber, {}).get(slug))

    def has(self, subscriber: str, slug: str, post_id: str) -> bool:
        with self._lock:
            return post_id in self._subs.get(subscriber, {}).get(slug, {})

    def add(self, subscriber: str, slug: str, post_id: str, when: float | None = None) -> None:
        with self._lock:
            posts = self._subs.setdefault(subscriber, {}).setdefault(slug, {})
            posts[post_id] = when if when is not None else time.time()
            self._dirty = True

    def forget(self, subscriber: str, slug: str, post_id: str) -> None:
        """Un-see a post so the next cycle retries it after a failed delivery."""
        with self._lock:
            posts = self._subs.get(subscriber, {}).get(slug, {})
            if posts.pop(post_id, None) is not None:
                self._dirty = True

    def drop_subscriber(self, subscriber: str) -> None:
        with self._lock:
            if self._subs.pop(subscriber, None) is not None:
                self._dirty = True

    def count(self, subscriber: str | None = None) -> int:
        with self._lock:
            if subscriber is not None:
                return sum(len(v) for v in self._subs.get(subscriber, {}).values())
            return sum(len(g) for s in self._subs.values() for g in s.values())

    # -- housekeeping ----------------------------------------------------
    def prune(self) -> int:
        """Drop entries older than the retention window and cap group size."""
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        with self._lock:
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

    def save(self, force: bool = False) -> bool:
        """Write the state file.  Returns True when something was written.

        The whole operation is under the lock, including the write.  Both the
        watch loop and the Discord control thread call this, and with the write
        outside the lock they raced on one temp filename: `os.replace` could
        fail outright on Windows, and the older payload could land last while
        `_dirty` was already cleared, so it was never rewritten - which shows up
        as already-notified posts arriving again after a restart.

        `_dirty` is cleared only once the file is actually in place, so a failed
        write is retried on the next cycle rather than silently discarded.
        """
        with self._lock:
            if not (self._dirty or force):
                return False
            payload = json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "updated": time.time(),
                    "subscribers": self._subs,
                },
                indent=1,
            )

            # A unique temp name, so two savers can never share one.
            tmp = self.path.with_suffix(
                f"{self.path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(payload, encoding="utf-8")
                _replace_with_retry(tmp, self.path)
            except OSError as exc:
                # Disk full, permissions, an antivirus holding the destination.
                # Leave _dirty set so the next save tries again, and never let
                # this kill the watch loop.
                log.error("Could not write %s: %s", self.path, exc)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return False

            self._dirty = False
            return True


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
