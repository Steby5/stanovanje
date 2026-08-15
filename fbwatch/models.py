"""Data types plus parsing of the user-editable `groups.txt` file."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .textutil import clean

# https://www.facebook.com/groups/<id-or-vanity-name>/...
_GROUP_URL_RE = re.compile(r"facebook\.com/groups/([^/?#\s]+)", re.I)
# A bare id or vanity slug typed on its own line.
_BARE_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Strip trailing `# comment`, but only when the # follows whitespace so that
# URL fragments are left alone.
_TRAILING_COMMENT_RE = re.compile(r"\s+#.*$")


@dataclass(frozen=True)
class Group:
    """One watched group."""

    slug: str  # numeric id or vanity name, used as the state key
    url: str  # canonical group URL
    name: str  # display name (alias from groups.txt, or the slug)

    @property
    def feed_url(self) -> str:
        """Group feed sorted by newest post rather than by 'top' activity."""
        return f"{self.url}?sorting_setting=CHRONOLOGICAL"


@dataclass
class Post:
    """A single scraped post."""

    post_id: str
    url: str
    text: str
    author: str = ""
    author_url: str = ""
    timestamp: str = ""
    images: list[str] = field(default_factory=list)
    group: Group | None = None
    text_source: str = "selector"  # or "fallback", for debugging

    @property
    def group_name(self) -> str:
        return self.group.name if self.group else ""


def _fingerprint(*parts: str) -> str:
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()
    return f"fp_{digest[:20]}"


def make_post_id(permalink: str, author: str, text: str) -> str:
    """Stable identity for a post.

    Prefer the numeric id embedded in the permalink.  Facebook does not always
    expose one (shared posts, some photo posts), so fall back to a hash of the
    author plus the text, which is stable across polls.
    """
    if permalink:
        for pattern in (
            r"/groups/[^/]+/(?:posts|permalink)/(\d+)",
            r"multi_permalink_id=(\d+)",
            r"story_fbid=(\d+)",
            r"[?&]fbid=(\d+)",
            r"/videos/(\d+)",
            r"/share/p/([A-Za-z0-9]+)",
        ):
            m = re.search(pattern, permalink)
            if m:
                return m.group(1)
    return _fingerprint(author, clean(text)[:400])


def parse_group_line(line: str) -> Group | None:
    """Parse one line of groups.txt into a Group, or None for blanks/comments."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    line = _TRAILING_COMMENT_RE.sub("", line).strip()
    if not line:
        return None

    alias = ""
    if "|" in line:
        line, alias = (part.strip() for part in line.split("|", 1))
    if not line:
        return None

    match = _GROUP_URL_RE.search(line)
    if match:
        slug = match.group(1)
    elif _BARE_SLUG_RE.match(line):
        slug = line
    else:
        raise ValueError(f"cannot read a group id or URL from: {line!r}")

    return Group(
        slug=slug,
        url=f"https://www.facebook.com/groups/{slug}",
        name=alias or slug,
    )


def load_groups(path: Path) -> list[Group]:
    """Read groups.txt.  Later duplicates of the same group are dropped."""
    if not path.exists():
        raise FileNotFoundError(f"groups file not found: {path}")

    groups: list[Group] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            group = parse_group_line(raw)
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
        if group and group.slug not in seen:
            seen.add(group.slug)
            groups.append(group)
    return groups
