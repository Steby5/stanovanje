"""Moving a login to a machine that has no screen.

A Chromium profile directory is not portable - cookies are encrypted with the
OS keystore - so the session has to travel as decrypted cookies instead.  The
round-trip test uses two real browser profiles to prove that actually works.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fbwatch.config import Config  # noqa: E402
from fbwatch.facebook import facebook_cookies  # noqa: E402

try:
    from playwright.sync_api import sync_playwright  # noqa: F401

    HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    HAVE_PLAYWRIGHT = False


class TestCookieFiltering(unittest.TestCase):
    def test_keeps_only_facebook_cookies(self):
        kept = facebook_cookies([
            {"name": "c_user", "value": "1", "domain": ".facebook.com", "path": "/"},
            {"name": "ads", "value": "x", "domain": ".doubleclick.net", "path": "/"},
        ])
        self.assertEqual([c["name"] for c in kept], ["c_user"])

    def test_drops_fields_playwright_rejects(self):
        kept = facebook_cookies([{
            "name": "xs", "value": "a", "domain": ".facebook.com", "path": "/",
            "session": True, "sourceScheme": "Secure",  # not accepted by add_cookies
        }])
        self.assertEqual(sorted(kept[0]), ["domain", "name", "path", "value"])

    def test_keeps_the_fields_that_matter(self):
        kept = facebook_cookies([{
            "name": "xs", "value": "a", "domain": ".facebook.com", "path": "/",
            "expires": 123.0, "httpOnly": True, "secure": True, "sameSite": "None",
        }])
        self.assertEqual(kept[0]["expires"], 123.0)
        self.assertTrue(kept[0]["httpOnly"])
        self.assertEqual(kept[0]["sameSite"], "None")

    def test_skips_malformed_entries(self):
        kept = facebook_cookies([
            {"name": "", "value": "y", "domain": ".facebook.com"},   # no name
            {"name": "ok", "domain": ".facebook.com"},                # no value
            {"name": "ok", "value": "v"},                             # no domain
        ])
        self.assertEqual(kept, [])

    def test_tolerates_empty_input(self):
        self.assertEqual(facebook_cookies([]), [])
        self.assertEqual(facebook_cookies(None), [])


@unittest.skipUnless(HAVE_PLAYWRIGHT, "playwright is not installed")
class TestSessionRoundTrip(unittest.TestCase):
    """Export from one profile, import into another, as if moving to a server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _scraper(self, profile_name: str):
        from fbwatch.facebook import FacebookScraper

        cfg = Config()
        cfg.base_dir = self.base
        cfg.browser_profile_dir = profile_name
        cfg.block_media = False
        return FacebookScraper(cfg, headless=True)

    def test_a_session_survives_the_move(self):
        from fbwatch.facebook import FacebookScraper

        fake_session = [{
            "name": "c_user", "value": "100012345", "domain": ".facebook.com",
            "path": "/", "httpOnly": False, "secure": True, "sameSite": "None",
        }, {
            "name": "xs", "value": "abc%3Adef", "domain": ".facebook.com",
            "path": "/", "httpOnly": True, "secure": True, "sameSite": "None",
        }]

        # The "desktop": plant a session, then export it the way the CLI does.
        try:
            with self._scraper("profile_desktop") as desktop:
                self.assertFalse(desktop.is_logged_in())
                desktop.import_cookies(fake_session)
                self.assertTrue(desktop.is_logged_in())
                exported = desktop.export_cookies()
        except Exception as exc:  # noqa: BLE001 - no browser binary in this env
            raise unittest.SkipTest(f"chromium unavailable: {exc}") from exc

        self.assertIn("c_user", [c["name"] for c in exported])

        # The "server": a clean profile that has never seen a login.
        with self._scraper("profile_server") as server:
            self.assertFalse(server.is_logged_in())
            loaded = server.import_cookies(exported)
            self.assertGreaterEqual(loaded, 2)
            self.assertTrue(server.is_logged_in())

    def test_importing_nothing_useful_reports_zero(self):
        try:
            with self._scraper("profile_empty") as scraper:
                self.assertEqual(scraper.import_cookies([]), 0)
                self.assertEqual(
                    scraper.import_cookies([{"name": "a", "value": "b", "domain": ".example.com"}]),
                    0,
                )
                self.assertFalse(scraper.is_logged_in())
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"chromium unavailable: {exc}") from exc

    def test_export_needs_a_started_browser(self):
        from fbwatch.facebook import FacebookScraper

        cfg = Config()
        cfg.base_dir = self.base
        with self.assertRaises(RuntimeError):
            FacebookScraper(cfg).export_cookies()


if __name__ == "__main__":
    unittest.main(verbosity=2)
