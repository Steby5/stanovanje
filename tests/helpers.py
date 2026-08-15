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


class StubDispatcher:
    """Captures delivery instead of doing any HTTP.

    `inbox` is keyed by subscriber name so tests can assert who received what,
    and `batches` records each grouped send as (webhook_url, [names]) so tests
    can assert that people sharing a channel got a single message.
    """

    def __init__(self, cfg, subscribers, session=None, inbox=None, batches=None, fail=()):
        self.cfg = cfg
        self.subscribers = list(subscribers)
        self.inbox = inbox if inbox is not None else {}
        self.batches = batches if batches is not None else []
        self.fail = fail
        for sub in self.subscribers:
            self.inbox.setdefault(sub.name, [])

    def deliver(self, post, matches):
        delivered = set()
        grouped: dict[str, list] = {}
        for sub, result in matches:
            grouped.setdefault(sub.discord_webhook_url or f"~{sub.name}", []).append((sub, result))

        for target, recipients in grouped.items():
            names = [s.name for s, _ in recipients]
            if any(n in self.fail for n in names):
                continue  # whole batch fails, as a failed HTTP send would
            self.batches.append((target, names))
            for sub, result in recipients:
                self.inbox[sub.name].append((post, result))
                delivered.add(sub.name)
        return delivered

    def describe(self, sub):
        return "stub"

    def shared_with(self, sub):
        same = sum(
            1 for s in self.subscribers
            if s.discord_webhook_url and s.discord_webhook_url == sub.discord_webhook_url
        )
        return max(0, same - 1)


def stub_dispatcher(inbox: dict, fail=(), batches=None):
    """Build a dispatcher_factory for Watcher that writes into `inbox`."""

    def factory(cfg, subscribers, session=None):
        return StubDispatcher(
            cfg, subscribers, session=session, inbox=inbox, batches=batches, fail=fail
        )

    return factory
