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
    def test_finds_three_posts_and_skips_the_empty_article(self):
        self.assertEqual(len(self.posts), 3)

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

    # -- ids -------------------------------------------------------------
    def test_post_ids_come_out_of_the_permalinks(self):
        ids = [make_post_id(p["permalink"], p["author"], p["text"]) for p in self.posts]
        self.assertEqual(ids, ["111222333", "444555666", "777888999"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
