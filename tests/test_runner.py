"""End-to-end tests of the watch cycle with the browser and Discord stubbed out.

Covers the behaviour that is easy to get wrong and expensive to debug live:
no duplicate notifications, no flood on the first poll, and a failed Discord
delivery being retried rather than silently dropped.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import StubScraper, make_group, make_post, stub_dispatcher  # noqa: E402

from fbwatch.config import Config  # noqa: E402
from fbwatch.notify import DiscordNotifier  # noqa: E402
from fbwatch.runner import Watcher  # noqa: E402

GROUP = make_group()
ME = "me"  # the implicit single-user subscriber


def post(pid: str, text: str):
    return make_post(pid, text, GROUP)


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "groups.txt").write_text(
            "https://www.facebook.com/groups/555000 | Test Group\n", encoding="utf-8"
        )
        (base / "keywords.txt").write_text("oddam + soba\ngarsonjera\n!agencija\n", encoding="utf-8")

        self.cfg = Config()
        self.cfg.base_dir = base
        self.cfg.discord_webhook_url = "https://discord.test/webhook"
        self.cfg.notify_on_first_run = True  # most tests want notifications

    def tearDown(self):
        self.tmp.cleanup()

    def watcher(self, fail: bool = False):
        """A Watcher whose delivery is captured instead of sent.

        Returns (watcher, notifier) where `notifier.sent` is what the single
        implicit subscriber received, keeping the older assertions readable.
        """
        inbox: dict = {}
        w = Watcher(
            self.cfg,
            dispatcher_factory=stub_dispatcher(inbox, fail=(ME,) if fail else ()),
        )
        w.reload_inputs()
        return w, _InboxView(inbox, ME)


class _InboxView:
    """Reads one subscriber's captured deliveries as `.sent`."""

    def __init__(self, inbox: dict, name: str):
        self._inbox = inbox
        self._name = name

    @property
    def sent(self):
        return [item for item in self._inbox.get(self._name, []) if item[0] != "text"]


class TestFiltering(RunnerTestCase):
    def test_only_matching_posts_are_sent(self):
        scraper = StubScraper([
            post("1", "Oddam sobo v Ljubljani, 400 EUR"),
            post("2", "Prodam rabljeno kolo"),
            post("3", "Oddam garsonjero v centru"),
        ])
        w, notifier = self.watcher()
        stats = w.check_group(scraper, GROUP)

        self.assertEqual(stats["seen"], 3)
        self.assertEqual(stats["matched"], 2)
        self.assertEqual(stats["sent"], 2)
        self.assertEqual([p.post_id for p, _ in notifier.sent], ["3", "1"])

    def test_excluded_posts_are_not_sent(self):
        scraper = StubScraper([post("1", "Oddam sobo preko agencija, provizija")])
        w, notifier = self.watcher()
        stats = w.check_group(scraper, GROUP)
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(notifier.sent, [])

    def test_notification_carries_the_direct_link_and_reason(self):
        scraper = StubScraper([post("42", "Oddam sobo, Bezigrad")])
        w, notifier = self.watcher()
        w.check_group(scraper, GROUP)

        sent_post, result = notifier.sent[0]
        self.assertEqual(sent_post.url, "https://www.facebook.com/groups/555000/posts/42/")
        self.assertEqual(result.matched_rules, ["oddam + soba"])

    def test_oldest_post_is_notified_first(self):
        # scrape_group returns newest-first, so notifications must be reversed.
        scraper = StubScraper([post("3", "Oddam sobo C"), post("1", "Oddam sobo A")])
        w, notifier = self.watcher()
        w.check_group(scraper, GROUP)
        self.assertEqual([p.post_id for p, _ in notifier.sent], ["1", "3"])


class TestDeduplication(RunnerTestCase):
    def test_the_same_post_is_only_sent_once(self):
        scraper = StubScraper([post("1", "Oddam sobo v Ljubljani")])
        w, notifier = self.watcher()

        first = w.check_group(scraper, GROUP)
        second = w.check_group(scraper, GROUP)

        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(second["new"], 0)
        self.assertEqual(len(notifier.sent), 1)

    def test_dedup_survives_a_restart(self):
        scraper = StubScraper([post("1", "Oddam sobo v Ljubljani")])
        w1, _ = self.watcher()
        w1.check_group(scraper, GROUP)

        w2, notifier2 = self.watcher()  # fresh Watcher reads the state file
        stats = w2.check_group(scraper, GROUP)
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(notifier2.sent, [])

    def test_a_new_post_among_old_ones_is_still_caught(self):
        old = [post("1", "Oddam sobo A")]
        scraper = StubScraper(old)
        w, notifier = self.watcher()
        w.check_group(scraper, GROUP)

        scraper.posts = [post("2", "Oddam sobo B")] + old
        stats = w.check_group(scraper, GROUP)
        self.assertEqual(stats["new"], 1)
        self.assertEqual([p.post_id for p, _ in notifier.sent], ["1", "2"])


class TestFirstRun(RunnerTestCase):
    def test_first_poll_is_silent_by_default(self):
        self.cfg.notify_on_first_run = False
        scraper = StubScraper([post("1", "Oddam sobo A"), post("2", "Oddam sobo B")])
        w, notifier = self.watcher()

        stats = w.check_group(scraper, GROUP)
        self.assertEqual(stats["new"], 2)
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(notifier.sent, [])

    def test_posts_arriving_after_the_first_poll_do_notify(self):
        self.cfg.notify_on_first_run = False
        scraper = StubScraper([post("1", "Oddam sobo A")])
        w, notifier = self.watcher()
        w.check_group(scraper, GROUP)

        scraper.posts = [post("2", "Oddam sobo B")] + scraper.posts
        stats = w.check_group(scraper, GROUP)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(notifier.sent[0][0].post_id, "2")


class TestDeliveryFailure(RunnerTestCase):
    def test_failed_delivery_is_retried_next_cycle(self):
        scraper = StubScraper([post("1", "Oddam sobo v Ljubljani")])
        w, view = self.watcher(fail=True)
        stats = w.check_group(scraper, GROUP)
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(view.sent, [])

        # Delivery starts working: the post must come back rather than be lost.
        inbox: dict = {}
        w._dispatcher_factory = stub_dispatcher(inbox)
        w.reload_inputs()
        stats = w.check_group(scraper, GROUP)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(inbox[ME][0][0].post_id, "1")


class TestDryRun(RunnerTestCase):
    def test_dry_run_sends_nothing(self):
        scraper = StubScraper([post("1", "Oddam sobo v Ljubljani")])
        w, notifier = self.watcher()
        stats = w.check_group(scraper, GROUP, notify=False)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(notifier.sent, [])

    def test_dry_run_leaves_no_trace(self):
        # Otherwise checking your rules would quietly eat the posts that a real
        # run was about to notify you about.
        scraper = StubScraper([post("1", "Oddam sobo v Ljubljani")])
        w, notifier = self.watcher()
        w.check_group(scraper, GROUP, notify=False)
        self.assertEqual(w.store.count(), 0)

        stats = w.check_group(scraper, GROUP)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(len(notifier.sent), 1)

    def test_repeated_dry_runs_report_the_same_thing(self):
        scraper = StubScraper([post("1", "Oddam sobo v Ljubljani")])
        w, _ = self.watcher()
        first = w.check_group(scraper, GROUP, notify=False)
        second = w.check_group(scraper, GROUP, notify=False)
        self.assertEqual(first, second)


class TestEmbedBuilding(RunnerTestCase):
    def test_embed_has_text_link_and_match_reason(self):
        from fbwatch.matcher import KeywordMatcher

        p = post("42", "Oddam sobo v Ljubljani, 400 EUR")
        p.images = ["https://scontent.xx.fbcdn.net/photo.jpg"]
        result = KeywordMatcher.from_lines(["oddam + soba"]).match(p.text)

        embed = DiscordNotifier(self.cfg).build_embed(p, result)
        self.assertIn("Oddam sobo v Ljubljani", embed["description"])
        self.assertEqual(embed["url"], p.url)
        self.assertIn("Test Group", embed["title"])
        self.assertIn("Ana Novak", embed["title"])
        self.assertEqual(embed["image"]["url"], p.images[0])
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertIn("oddam + soba", fields["Matched"])
        self.assertIn(p.url, fields["Link"])

    def test_very_long_post_is_truncated_to_discord_limits(self):
        from fbwatch.matcher import MatchResult

        p = post("42", "Oddam sobo. " + "besedilo " * 2000)
        embed = DiscordNotifier(self.cfg).build_embed(p, MatchResult(matched=True))
        self.assertLessEqual(len(embed["description"]), 4000)

    def test_post_without_text_still_produces_an_embed(self):
        from fbwatch.matcher import MatchResult

        p = post("42", "")
        embed = DiscordNotifier(self.cfg).build_embed(p, MatchResult(matched=True))
        self.assertTrue(embed["description"])
        self.assertEqual(embed["url"], p.url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
