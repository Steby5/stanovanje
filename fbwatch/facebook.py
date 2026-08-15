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

from .extract_js import (
    EXPAND_SEE_MORE_JS,
    EXTRACT_POSTS_JS,
    LOGIN_MARKERS_JS,
    POST_MARKER_SELECTOR,
    SEE_MORE_LABELS,
)
from .models import Group, Post, make_post_id

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# How long to let a scroll settle before counting what loaded.  Long enough for
# the feed to render the next batch, short enough not to dominate the scan.
SCROLL_SETTLE_MS = (450, 850)

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


# Playwright accepts exactly these keys when adding a cookie back.
_COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


def facebook_cookies(cookies: list[dict]) -> list[dict]:
    """Keep only facebook.com cookies, trimmed to the fields Playwright wants."""
    out = []
    for cookie in cookies or []:
        domain = str(cookie.get("domain", ""))
        if "facebook.com" not in domain:
            continue
        trimmed = {k: cookie[k] for k in _COOKIE_FIELDS if k in cookie}
        if trimmed.get("name") and "value" in trimmed and trimmed.get("domain"):
            out.append(trimmed)
    return out


class BrowserUnavailable(RuntimeError):
    """Chromium could not be started at all."""


def _launch_advice(exc: Exception, profile: Path) -> str:
    """Translate a Chromium launch failure into the thing to actually do.

    Playwright reports these as a wall of subprocess output; the useful signal
    is buried in it.  Exit code 127 in particular means the binary or one of
    its shared libraries is missing, which on Linux is the usual case of having
    downloaded the browser without its system dependencies.
    """
    detail = str(exc)
    lines = ["Chromium could not start."]

    if "127" in detail or "error while loading shared libraries" in detail:
        lines.append(
            "It is missing system libraries. On Debian/Ubuntu:\n"
            "    sudo python -m playwright install-deps chromium\n"
            "    python -m playwright install chromium"
        )
    elif "Executable doesn't exist" in detail or "please run" in detail.lower():
        lines.append("The browser is not downloaded yet:\n    python -m playwright install chromium")
    elif "ProcessSingleton" in detail or "SingletonLock" in detail or "in use" in detail:
        lines.append(
            f"Another instance is already using {profile}. Stop the other fbwatch, "
            "or give this one its own browser_profile_dir."
        )
    else:
        lines.append(
            "If this is a fresh machine, install the browser and its dependencies:\n"
            "    python -m playwright install --with-deps chromium"
        )

    lines.append(f"Original error: {detail.strip().splitlines()[0][:300]}")
    return "\n".join(lines)


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
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=self.headless,
                args=CHROMIUM_ARGS,
                user_agent=USER_AGENT,
                locale=self.cfg.locale,
                timezone_id=self.cfg.timezone,
                viewport={"width": 1366, "height": 900},
            )
        except Exception as exc:  # noqa: BLE001 - turn a cryptic failure into advice
            self.stop()
            raise BrowserUnavailable(_launch_advice(exc, profile)) from exc
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

    # -- portable sessions ----------------------------------------------
    def export_cookies(self) -> list[dict]:
        """Facebook cookies from this profile, for moving to another machine.

        The profile directory itself is not portable - Chromium encrypts
        cookies with the OS keystore, so a copied profile decrypts to nothing
        on a different machine or OS.  These are the decrypted values.
        """
        if self._ctx is None:
            raise RuntimeError("FacebookScraper.start() was not called")
        return facebook_cookies(self._ctx.cookies())

    def import_cookies(self, cookies: list[dict]) -> int:
        """Load an exported session into this profile.  Returns how many took."""
        if self._ctx is None:
            raise RuntimeError("FacebookScraper.start() was not called")
        usable = facebook_cookies(cookies)
        if not usable:
            return 0
        self._ctx.add_cookies(usable)
        return len(usable)

    # -- scraping -------------------------------------------------------
    def scrape_group(self, group: Group, limit: int | None = None) -> list[Post]:
        """Load a group's newest-first feed and return the posts on it.

        Commands are answered on their own thread, so nothing here needs to
        yield for them.
        """
        limit = limit or self.cfg.posts_per_group
        page = self.page

        try:
            page.goto(group.feed_url, wait_until="domcontentloaded")
        except PlaywrightTimeout as exc:
            raise ScrapeError(f"timed out loading {group.feed_url}") from exc

        blocked = page.evaluate(LOGIN_MARKERS_JS)
        if blocked:
            raise LoginRequired(f"{group.name}: {blocked}")

        # Waited in slices rather than one long block, so the pauses stay short
        # enough to keep answering commands while a slow group loads.
        deadline = time.monotonic() + 25
        while True:
            try:
                page.wait_for_selector(POST_MARKER_SELECTOR, timeout=3000)
                break
            except PlaywrightTimeout:
                if time.monotonic() >= deadline:
                    # An empty group, a members-only wall, or a layout change.
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

    def _loaded_posts(self) -> list[dict]:
        """The posts that have actually rendered so far.

        Extracts rather than counting DOM nodes: the feed is virtualised, so
        most `aria-posinset` items are empty placeholders and counting those
        would stop the scroll long before any real post had loaded.
        """
        try:
            return self.page.evaluate(EXTRACT_POSTS_JS)
        except PlaywrightError:
            return []

    def _scroll_until(self, wanted: int, max_scrolls: int = 12) -> None:
        """Lazy-loaded feed: scroll until enough posts exist or it stops growing.

        Stopping early at the first already-handled post would be the obvious
        optimisation, but it cannot be done safely here: ids are hashed from
        the post text, and during scrolling that text is still truncated -
        "See more" has not been clicked yet - so nothing matches what was
        stored.  Making the hash truncation-proof would mean hashing a short
        prefix, which collides across similar posts by the same author and
        costs listings.  Depth is capped by `posts_per_group` instead.
        """
        page = self.page
        stalled = 0
        posts = self._loaded_posts()
        for _ in range(max_scrolls):
            count = len(posts)
            if count >= wanted:
                return
            page.mouse.wheel(0, 2200)
            # Short settle while the feed is keeping up, longer once it isn't:
            # a slow batch must not be mistaken for the end of the feed, which
            # silently costs posts.
            settle = random.randint(SCROLL_SETTLE_MS[0], SCROLL_SETTLE_MS[1])
            page.wait_for_timeout(settle * (1 + stalled))
            # Carried into the next pass rather than re-measured at the top of
            # it, since measuring means running the extractor over the feed.
            posts = self._loaded_posts()
            if len(posts) <= count:
                stalled += 1
                if stalled >= 3:
                    return  # genuinely the end of the feed
            else:
                stalled = 0

    def _expand_see_more(self, max_clicks: int = 40) -> None:
        """Click the truncation toggles so full post text is in the DOM.

        Done inside the page: reading each button's label from Python cost a
        round trip apiece, and a loaded feed carries several hundred buttons.
        """
        try:
            clicked = self.page.evaluate(
                EXPAND_SEE_MORE_JS, [sorted(SEE_MORE_LABELS), max_clicks]
            )
        except PlaywrightError:
            return
        if clicked:
            self.page.wait_for_timeout(400)  # let the expanded text render

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

        # Some shapes carry the id only in the query - photo.php?fbid=…,
        # watch/?v=… - so dropping the query leaves a bare, dead path like
        # https://www.facebook.com/photo.php.  That is worse than no link at
        # all, because the caller's `permalink or group.url` fallback sees a
        # truthy value and keeps it.  Keep the identifying parameter.
        for key in ("fbid", "v"):
            if key in params and parsed.path:
                return f"https://www.facebook.com{parsed.path}?{key}={params[key][0]}"
        if not parsed.path or parsed.path == "/":
            return ""  # nothing usable; let the caller fall back to the group

        return f"https://www.facebook.com{parsed.path}"

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
            "feed_items": page.evaluate(
                f"() => document.querySelectorAll({POST_MARKER_SELECTOR!r}).length"
            ),
            "login_marker": page.evaluate(LOGIN_MARKERS_JS),
            "posts": page.evaluate(EXTRACT_POSTS_JS),
            "files": str(stem),
        }
