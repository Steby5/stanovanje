"""Reading Facebook's post times, and the age cut built on them.

The parser is hand-rolled because `dateutil.parser.parse(..., fuzzy=True)` is
confidently wrong on every relative form - it reads the leading number as a day
or a year. Several tests below pin exactly those cases, because a wrong age is
worse than no age: it decides whether a listing is shown at all.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import StubScraper, make_group, make_post, stub_dispatcher  # noqa: E402

from fbwatch.config import Config  # noqa: E402
from fbwatch.postdate import describe_age, parse_posted_at  # noqa: E402
from fbwatch.runner import Watcher  # noqa: E402

NOW = datetime(2026, 8, 17, 14, 0)
GROUP = make_group()


class TestRelativeForms(unittest.TestCase):
    def parse(self, text):
        return parse_posted_at(text, NOW)

    def test_days_ago(self):
        self.assertEqual(self.parse("3 days ago"), NOW - timedelta(days=3))

    def test_minutes_ago_is_minutes_not_a_year(self):
        # dateutil fuzzy turns this into the year 2045.
        self.assertEqual(self.parse("45 mins ago"), NOW - timedelta(minutes=45))

    def test_hours_ago_is_hours_not_a_day(self):
        # dateutil fuzzy turns this into the 2nd of the month.
        self.assertEqual(self.parse("2 hrs ago"), NOW - timedelta(hours=2))

    def test_bare_short_forms(self):
        self.assertEqual(self.parse("1 h"), NOW - timedelta(hours=1))
        self.assertEqual(self.parse("5d"), NOW - timedelta(days=5))
        self.assertEqual(self.parse("2w"), NOW - timedelta(weeks=2))

    def test_just_now(self):
        self.assertEqual(self.parse("Just now"), NOW)

    def test_yesterday_with_and_without_a_time(self):
        self.assertEqual(self.parse("Yesterday at 18:12"), datetime(2026, 8, 16, 18, 12))
        self.assertEqual(self.parse("Yesterday"), NOW - timedelta(days=1))

    def test_slovenian_forms(self):
        self.assertEqual(self.parse("pred 3 dnevi"), NOW - timedelta(days=3))
        self.assertEqual(self.parse("včeraj ob 18:12"), datetime(2026, 8, 16, 18, 12))
        self.assertEqual(self.parse("1 t"), NOW - timedelta(weeks=1))


class TestAbsoluteForms(unittest.TestCase):
    def parse(self, text):
        return parse_posted_at(text, NOW)

    def test_month_day_and_time(self):
        self.assertEqual(self.parse("July 22 at 3:41 PM"), datetime(2026, 7, 22, 15, 41))

    def test_a_narrow_no_break_space_is_tolerated(self):
        # Facebook writes the time with U+202F before AM/PM.
        self.assertEqual(
            self.parse("July 22 at 3:41 PM"), datetime(2026, 7, 22, 15, 41)
        )

    def test_twenty_four_hour_time(self):
        self.assertEqual(self.parse("August 3 at 09:15"), datetime(2026, 8, 3, 9, 15))

    def test_with_an_explicit_year(self):
        self.assertEqual(
            self.parse("December 31, 2025 at 11:59 PM"), datetime(2025, 12, 31, 23, 59)
        )

    def test_a_date_ahead_of_us_belongs_to_last_year(self):
        # No year given and December is months away, so it is last December.
        self.assertEqual(self.parse("December 1 at 10:00"), datetime(2025, 12, 1, 10, 0))

    def test_slovenian_day_first(self):
        self.assertEqual(self.parse("22. julij ob 15:41"), datetime(2026, 7, 22, 15, 41))

    def test_midnight_and_noon(self):
        self.assertEqual(self.parse("July 1 at 12:00 AM"), datetime(2026, 7, 1, 0, 0))
        self.assertEqual(self.parse("July 1 at 12:00 PM"), datetime(2026, 7, 1, 12, 0))


class TestRejects(unittest.TestCase):
    """The sprite that carries the time also carries other things."""

    def test_non_time_sprites_are_not_times(self):
        for text in ("Learn More", "Comment", "Send", "Anonymous participant",
                     "Vsec mi je", "Shop Now", ""):
            self.assertIsNone(parse_posted_at(text, NOW), text)

    def test_nonsense_numbers_are_rejected(self):
        self.assertIsNone(parse_posted_at("99 bottles", NOW))
        self.assertIsNone(parse_posted_at("July 99 at 3:41 PM", NOW))
        self.assertIsNone(parse_posted_at("July 22 at 99:99", NOW))


class TestDescribeAge(unittest.TestCase):
    def test_reads_as_a_person_would_say_it(self):
        cases = [
            (timedelta(seconds=10), "just now"),
            (timedelta(minutes=20), "20 min ago"),
            (timedelta(hours=3), "3 h ago"),
            (timedelta(days=1), "1 day ago"),
            (timedelta(days=3), "3 days ago"),
            (timedelta(days=10), "1 week ago"),
        ]
        for delta, expected in cases:
            self.assertEqual(describe_age(NOW - delta, NOW), expected, str(delta))

    def test_unknown_stays_blank(self):
        self.assertEqual(describe_age(None, NOW), "")


# ---------------------------------------------------------------------------
class TestAgeCut(unittest.TestCase):
    """Old listings must not be notified as if they were fresh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "groups.txt").write_text(
            "https://www.facebook.com/groups/555000 | Test Group\n", encoding="utf-8"
        )
        (base / "keywords.txt").write_text("oddam + soba\n", encoding="utf-8")

        self.cfg = Config()
        self.cfg.base_dir = base
        self.cfg.discord_webhook_url = "https://discord.com/api/webhooks/1/abc"
        self.cfg.notify_on_first_run = True
        self.cfg.max_post_age_hours = 12

        self.inbox: dict = {}
        self.watcher = Watcher(self.cfg, dispatcher_factory=stub_dispatcher(self.inbox))
        self.watcher.reload_inputs()

    def tearDown(self):
        self.tmp.cleanup()

    def post_aged(self, pid, hours):
        post = make_post(pid, "Oddam sobo v Ljubljani", GROUP)
        post.posted_at = datetime.now() - timedelta(hours=hours)
        return post

    def received(self):
        return [p.post_id for p, _ in self.inbox.get("me", []) if p != "text"]

    def test_a_fresh_post_is_notified(self):
        self.watcher.check_group(StubScraper([self.post_aged("1", 2)]), GROUP)
        self.assertEqual(self.received(), ["1"])

    def test_an_old_post_is_not(self):
        self.watcher.check_group(StubScraper([self.post_aged("1", 30)]), GROUP)
        self.assertEqual(self.received(), [])

    def test_an_old_post_is_still_recorded_so_it_never_comes_back(self):
        scraper = StubScraper([self.post_aged("1", 30)])
        self.watcher.check_group(scraper, GROUP)
        self.assertTrue(self.watcher.store.has("me", GROUP.slug, "1"))

    def test_an_unknown_age_is_never_cut(self):
        # A post we cannot date must not be assumed stale - that would lose it.
        post = make_post("1", "Oddam sobo v Ljubljani", GROUP)
        self.assertIsNone(post.posted_at)
        self.watcher.check_group(StubScraper([post]), GROUP)
        self.assertEqual(self.received(), ["1"])

    def test_the_cut_can_be_turned_off(self):
        self.cfg.max_post_age_hours = 0
        self.watcher.check_group(StubScraper([self.post_aged("1", 300)]), GROUP)
        self.assertEqual(self.received(), ["1"])

    def test_the_boundary_is_respected(self):
        self.watcher.check_group(
            StubScraper([self.post_aged("fresh", 11), self.post_aged("stale", 13)]), GROUP
        )
        self.assertEqual(self.received(), ["fresh"])


class TestEmbedShowsRealTime(unittest.TestCase):
    def test_the_embed_carries_the_post_time_not_the_send_time(self):
        from fbwatch.matcher import MatchResult
        from fbwatch.notify import DiscordNotifier

        cfg = Config()
        post = make_post("1", "Oddam sobo", GROUP)
        post.posted_at = datetime(2026, 8, 14, 9, 30)
        embed = DiscordNotifier(cfg).build_embed(post, MatchResult(matched=True))

        self.assertIn("2026-08-14", embed["timestamp"])
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertIn("ago", fields["Posted"])

    def test_an_unknown_time_leaves_the_stamp_off_entirely(self):
        from fbwatch.matcher import MatchResult
        from fbwatch.notify import DiscordNotifier

        post = make_post("1", "Oddam sobo", GROUP)
        embed = DiscordNotifier(Config()).build_embed(post, MatchResult(matched=True))
        self.assertNotIn("timestamp", embed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
