"""Playwright-driven scraping of Facebook group feeds.

Housing groups are almost always private, so this drives a real Chromium with
a persistent profile: you log in once with `python main.py login`, and the
session cookie in that profile is reused on every later run.
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .extract_js import EXTRACT_POSTS_JS, LOGIN_MARKERS_JS, SEE_MORE_LABELS
from .models import Group, Post, make_post_id

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-notifications",
    "--disable-features=Translate,MediaRouter",
]

# Hides the tell-tale `navigator.webdriver === true` that automation sets.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
"""


class LoginRequired(RuntimeError):
    """The saved session is gone or Facebook wants a checkpoint cleared."""


class ScrapeError(RuntimeError):
    """A group could not be read this cycle; the next cycle may succeed."""


class FacebookScraper:
    """Owns the browser.  Use as a context manager."""

    def __init__(self, cfg, headless: bool | None = None):
        self.cfg = cfg
        self.headless = cfg.headless if headless is None else headless
        self._pw = None
        self._ctx = None
        self._page = None

    # -- lifecycle ------------------------------------------------------
    def __enter__(self) -> "FacebookScraper":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        profile = Path(self.cfg.profile_path)
        profile.mkdir(parents=True, exist_ok=True)

        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=self.headless,
            args=CHROMIUM_ARGS,
            user_agent=USER_AGENT,
            locale=self.cfg.locale,
            timezone_id=self.cfg.timezone,
            viewport={"width": 1366, "height": 900},
        )
        self._ctx.add_init_script(STEALTH_JS)
        self._ctx.set_default_timeout(self.cfg.page_timeout_seconds * 1000)

        if self.cfg.block_media:
            self._ctx.route("**/*", self._route)

        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        log.debug("Browser started (headless=%s, profile=%s)", self.headless, profile)

    def stop(self) -> None:
        for closer in (
            lambda: self._ctx and self._ctx.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                log.debug("Ignoring error while closing browser: %s", exc)
        self._ctx = self._pw = self._page = None

    def _route(self, route) -> None:
        """Skip images/media/fonts - the feed still renders and loads faster."""
        try:
            if route.request.resource_type in ("image", "media", "font"):
                route.abort()
            else:
                route.continue_()
        except PlaywrightError:
            pass  # the page navigated away mid-request

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("FacebookScraper.start() was not called")
        return self._page

    # -- session --------------------------------------------------------
    def is_logged_in(self) -> bool:
        """A `c_user` cookie is Facebook's marker for an authenticated session."""
        if self._ctx is None:
            return False
        for cookie in self._ctx.cookies():
            if cookie.get("name") == "c_user" and cookie.get("value"):
                return True
        return False

    def interactive_login(self, timeout_seconds: int = 600) -> bool:
        """Open a real window and wait for the user to finish logging in."""
        self.page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.is_logged_in():
                time.sleep(3)  # let Facebook finish writing session cookies
                return True
            time.sleep(2)
        return False

    # -- scraping -------------------------------------------------------
    def scrape_group(self, group: Group, limit: int | None = None) -> list[Post]:
        """Load a group's newest-first feed and return the posts on it."""
        limit = limit or self.cfg.posts_per_group
        page = self.page

        try:
            page.goto(group.feed_url, wait_until="domcontentloaded")
        except PlaywrightTimeout as exc:
            raise ScrapeError(f"timed out loading {group.feed_url}") from exc

        blocked = page.evaluate(LOGIN_MARKERS_JS)
        if blocked:
            raise LoginRequired(f"{group.name}: {blocked}")

        try:
            page.wait_for_selector('div[role="article"]', timeout=25000)
        except PlaywrightTimeout:
            # Either an empty group, a members-only wall, or a layout change.
            if page.evaluate(LOGIN_MARKERS_JS):
                raise LoginRequired(f"{group.name}: session expired") from None
            raise ScrapeError(
                f"{group.name}: no posts found - are you a member of this group?"
            ) from None

        self._scroll_until(limit)
        self._expand_see_more()

        try:
            raw = page.evaluate(EXTRACT_POSTS_JS)
        except PlaywrightError as exc:
            raise ScrapeError(f"{group.name}: extraction failed: {exc}") from exc

        posts: list[Post] = []
        seen_ids: set[str] = set()
        for item in raw[:limit]:
            permalink = self._canonical_url(item.get("permalink", ""), group)
            text = (item.get("text") or "").strip()
            author = (item.get("author") or "").strip()

            post_id = make_post_id(item.get("permalink", ""), author, text)
            if post_id in seen_ids:
                continue  # feed sometimes renders the same post twice
            seen_ids.add(post_id)

            posts.append(
                Post(
                    post_id=post_id,
                    url=permalink or group.url,
                    text=text,
                    author=author,
                    author_url=(item.get("author_url") or "").strip(),
                    timestamp=(item.get("timestamp") or "").strip(),
                    images=list(item.get("images") or []),
                    group=group,
                    text_source=item.get("text_source", "selector"),
                )
            )

        log.debug("%s: extracted %d post(s)", group.name, len(posts))
        return posts

    def _scroll_until(self, wanted: int, max_scrolls: int = 12) -> None:
        """Lazy-loaded feed: scroll until enough posts exist or it stops growing."""
        page = self.page
        stalled = 0
        for _ in range(max_scrolls):
            count = page.evaluate(
                '() => document.querySelectorAll(\'div[role="article"]\').length'
            )
            if count >= wanted:
                return
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(random.randint(700, 1400))
            new_count = page.evaluate(
                '() => document.querySelectorAll(\'div[role="article"]\').length'
            )
            if new_count <= count:
                stalled += 1
                if stalled >= 2:
                    return  # end of the feed
            else:
                stalled = 0

    def _expand_see_more(self, max_clicks: int = 40) -> None:
        """Click the truncation toggles so full post text is in the DOM."""
        page = self.page
        clicks = 0
        try:
            buttons = page.query_selector_all('div[role="article"] div[role="button"]')
        except PlaywrightError:
            return

        for button in buttons:
            if clicks >= max_clicks:
                break
            try:
                label = (button.inner_text() or "").strip().lower()
                if label in SEE_MORE_LABELS:
                    button.click(timeout=2500)
                    clicks += 1
                    page.wait_for_timeout(90)
            except (PlaywrightError, PlaywrightTimeout):
                continue  # detached, covered, or navigated - not worth retrying
        if clicks:
            page.wait_for_timeout(350)

    @staticmethod
    def _canonical_url(href: str, group: Group) -> str:
        """Strip Facebook's tracking query junk off a permalink."""
        if not href:
            return ""
        import re
        from urllib.parse import parse_qs, urlparse

        m = re.search(r"/groups/([^/]+)/(?:posts|permalink)/(\d+)", href)
        if m:
            return f"https://www.facebook.com/groups/{m.group(1)}/posts/{m.group(2)}/"

        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        for key in ("multi_permalink_id", "story_fbid"):
            if key in params:
                return f"{group.url}/posts/{params[key][0]}/"
        # Unknown shape: keep the path, drop the query string.
        return f"https://www.facebook.com{parsed.path}" if parsed.path else href

    # -- debugging ------------------------------------------------------
    def dump(self, group: Group, out_dir: Path) -> dict:
        """Save HTML, a screenshot and the parsed posts for troubleshooting."""
        out_dir.mkdir(parents=True, exist_ok=True)
        page = self.page
        page.goto(group.feed_url, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        self._scroll_until(self.cfg.posts_per_group)
        self._expand_see_more()

        stem = out_dir / f"dump_{group.slug}"
        stem.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        try:
            page.screenshot(path=str(stem.with_suffix(".png")), full_page=False)
        except PlaywrightError as exc:
            log.debug("Screenshot failed: %s", exc)
        return {
            "url": page.url,
            "articles": page.evaluate(
                '() => document.querySelectorAll(\'div[role="article"]\').length'
            ),
            "login_marker": page.evaluate(LOGIN_MARKERS_JS),
            "posts": page.evaluate(EXTRACT_POSTS_JS),
            "files": str(stem),
        }
