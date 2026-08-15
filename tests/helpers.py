"""Shared stubs so tests can exercise the watcher without a browser or Discord."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fbwatch.models import Group, Post  # noqa: E402
from fbwatch.notify import DiscordNotifier  # noqa: E402


def make_group(slug: str = "555000", name: str = "Test Group") -> Group:
    return Group(slug=slug, url=f"https://www.facebook.com/groups/{slug}", name=name)


def make_post(pid: str, text: str, group: Group | None = None) -> Post:
    group = group or make_group()
    return Post(
        post_id=pid,
        url=f"{group.url}/posts/{pid}/",
        text=text,
        author="Ana Novak",
        timestamp="4 h",
        group=group,
    )


class StubScraper:
    """Stands in for FacebookScraper; returns whatever posts the test sets."""

    def __init__(self, posts: list[Post]):
        self.posts = posts
        self.calls = 0

    def scrape_group(self, group, limit=None):
        self.calls += 1
        return list(self.posts)


class StubNotifier(DiscordNotifier):
    """Records what would have been sent; can be told to fail."""

    def __init__(self, cfg, fail: bool = False):
        super().__init__(cfg)
        self.webhook_url = "https://discord.test/webhook"
        self.sent: list[tuple] = []
        self.texts: list[str] = []
        self.fail = fail

    def send_post(self, post, result):
        if self.fail:
            return False
        self.sent.append((post, result))
        return True

    def send_text(self, message):
        self.texts.append(message)
        return True


class StubMailbox:
    """Captures per-subscriber delivery instead of doing any HTTP.

    Instances share `inbox`, keyed by subscriber name, so a test can assert on
    who received what after a fan-out.
    """

    def __init__(self, cfg, sub, session=None, inbox=None, fail=()):
        self.cfg = cfg
        self.name = sub.name
        self.inbox = inbox if inbox is not None else {}
        self.fail = fail
        self.inbox.setdefault(self.name, [])

    @property
    def enabled(self):
        return True

    def send_post(self, post, result):
        if self.name in self.fail:
            return False
        self.inbox[self.name].append((post, result))
        return True

    def send_text(self, message):
        self.inbox[self.name].append(("text", message))
        return True

    def describe(self):
        return "stub"


def stub_mailboxes(inbox: dict, fail=()):
    """Build a mailbox_factory for Watcher that writes into `inbox`."""

    def factory(cfg, sub, session=None):
        return StubMailbox(cfg, sub, session=session, inbox=inbox, fail=fail)

    return factory
