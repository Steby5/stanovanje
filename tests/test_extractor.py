"""Runs the in-page extraction JS against a fixture feed in a real browser.

This is the only test that needs Playwright and its Chromium download; it is
skipped automatically when either is missing.  It catches the failure mode that
matters most: a syntax error or a bad selector in extract_js.py, which would
otherwise only show up against the live site.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fbwatch.extract_js import EXTRACT_POSTS_JS, LOGIN_MARKERS_JS  # noqa: E402
from fbwatch.models import make_post_id  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixture_feed.html").resolve()

try:
    from playwright.sync_api import sync_playwright

    HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    HAVE_PLAYWRIGHT = False


@unittest.skipUnless(HAVE_PLAYWRIGHT, "playwright is not installed")
class TestExtractor(unittest.TestCase):
    posts: list = []

    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        try:
            cls._browser = cls._pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - browser binary not downloaded
            cls._pw.stop()
            raise unittest.SkipTest(f"chromium unavailable: {exc}") from exc
        page = cls._browser.new_page()
        page.goto(FIXTURE.as_uri())
        cls.posts = page.evaluate(EXTRACT_POSTS_JS)
        cls.login_marker = page.evaluate(LOGIN_MARKERS_JS)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_browser"):
            cls._browser.close()
        if hasattr(cls, "_pw"):
            cls._pw.stop()

    # -- structure -------------------------------------------------------
    def test_finds_every_post_in_both_markups(self):
        # three in the old role="article" form, two in the current feed form
        self.assertEqual(len(self.posts), 5)

    def test_skips_the_empty_article_and_the_placeholders(self):
        # virtualised aria-posinset items with nothing rendered in them
        for post in self.posts:
            self.assertTrue(post["text"] or post["permalink"])

    def test_comment_is_not_reported_as_a_post(self):
        for post in self.posts:
            self.assertNotIn("Je še prosto?", post["text"])

    def test_login_marker_is_clear_on_a_normal_page(self):
        self.assertEqual(self.login_marker, "")

    # -- fields ----------------------------------------------------------
    def test_reads_text_from_the_message_container(self):
        post = self.posts[0]
        self.assertEqual(post["text_source"], "selector")
        self.assertIn("Oddam lepo sobo v Ljubljani", post["text"])
        self.assertIn("400 EUR", post["text"])

    def test_reads_author_and_permalink(self):
        post = self.posts[0]
        self.assertEqual(post["author"], "Ana Novak")
        self.assertIn("/posts/111222333", post["permalink"])
        self.assertEqual(post["timestamp"], "4 h")

    def test_fallback_path_recovers_text_without_a_message_container(self):
        post = self.posts[1]
        self.assertEqual(post["text_source"], "fallback")
        self.assertIn("Iščem sostanovalca", post["text"])

    def test_fallback_drops_buttons_and_counters(self):
        text = self.posts[1]["text"]
        for chrome in ("Prikaži več", "Deli", "12 komentarjev"):
            self.assertNotIn(chrome, text)

    def test_permalink_from_query_string(self):
        self.assertIn("multi_permalink_id=777888999", self.posts[2]["permalink"])

    # -- images ----------------------------------------------------------
    def test_keeps_a_content_image(self):
        self.assertIn(
            "https://scontent.xx.fbcdn.net/v/p720x720/photo1.jpg", self.posts[0]["images"]
        )

    def test_skips_small_avatars(self):
        self.assertEqual(self.posts[2]["images"], [])

    # -- the current Facebook markup --------------------------------------
    def test_a_feed_item_is_read_as_a_post(self):
        post = self.posts[3]
        self.assertEqual(post["author"], "Boris Banjanin")
        self.assertIn("enoposteljno sobo", post["text"])

    def test_the_byline_identifies_a_post_without_a_message_container(self):
        self.assertEqual(self.posts[4]["author"], "Anonymous participant")
        self.assertIn("garsonjero", self.posts[4]["text"])

    def test_an_inline_comment_does_not_leak_into_the_post(self):
        post = self.posts[3]
        self.assertNotIn("041703375", post["text"])
        self.assertNotIn(
            "https://scontent.xx.fbcdn.net/v/p60x60/commenter.jpg", post["images"]
        )

    def test_the_timestamp_is_resolved_through_the_svg_sprite(self):
        # The characters live outside the post, referenced by <use>; nothing
        # scoped to the post's own subtree can see them.
        self.assertEqual(self.posts[3]["timestamp"], "3 days ago")

    def test_a_second_sprite_is_not_mistaken_for_the_time(self):
        # The same post carries a "Learn More" sprite from a link preview.
        self.assertNotIn("Learn", self.posts[3]["timestamp"])

    def test_an_absolute_date_sprite_is_read_too(self):
        self.assertIn("July 22", self.posts[4]["timestamp"])

    def test_a_comment_link_yields_the_parent_post_permalink(self):
        # The post's own anchor has no path, but an inline comment's does; the
        # comment_id is stripped so it points at the post rather than the reply.
        permalink = self.posts[3]["permalink"]
        self.assertIn("/posts/10163383368366317", permalink)
        self.assertNotIn("comment_id", permalink)

    def test_posts_come_back_in_feed_order(self):
        # The watcher reverses this list to notify oldest-first, so document
        # order has to survive being collected by several selectors.
        self.assertIn("Ana Novak", self.posts[0]["author"])
        self.assertEqual(self.posts[4]["author"], "Anonymous participant")

    # -- ids -------------------------------------------------------------
    def test_post_ids_come_out_of_the_permalinks(self):
        ids = [make_post_id(p["permalink"], p["author"], p["text"]) for p in self.posts[:3]]
        self.assertEqual(ids, ["111222333", "444555666", "777888999"])

    def test_a_post_without_a_permalink_still_gets_a_stable_id(self):
        post = self.posts[4]
        self.assertEqual(post["permalink"], "")
        first = make_post_id(post["permalink"], post["author"], post["text"])
        again = make_post_id(post["permalink"], post["author"], post["text"])
        self.assertTrue(first.startswith("fp_"))
        self.assertEqual(first, again)


if __name__ == "__main__":
    unittest.main(verbosity=2)
