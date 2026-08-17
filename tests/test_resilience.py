"""The watch loop's failure handling.

None of this was reachable before: `run_forever` built its scraper inline, so
267 tests passed without touching any of the code that decides whether the
watcher survives a bad config file, a lost session, or a restart.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import StubScraper, make_group, make_post, stub_dispatcher, stub_scraper  # noqa: E402

from fbwatch.config import Config  # noqa: E402
from fbwatch.facebook import BrowserUnavailable, LoginRequired, ScrapeError  # noqa: E402
from fbwatch.runner import Watcher  # noqa: E402
from fbwatch.store import SeenStore  # noqa: E402

GROUP = make_group()


class RecordingNotifier:
    """Captures alerts, and can be told the network is down."""

    def __init__(self, working=True):
        self.enabled = True
        self.sent: list[str] = []
        self.working = working

    def send_text(self, message, attempts=4):
        if not self.working:
            return False
        self.sent.append(message)
        return True

    def send_post(self, post, result):
        return True


class LoopTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "groups.txt").write_text(
            "https://www.facebook.com/groups/555000 | Test Group\n", encoding="utf-8"
        )
        (self.base / "keywords.txt").write_text("oddam + soba\n", encoding="utf-8")

        self.cfg = Config()
        self.cfg.base_dir = self.base
        self.cfg.discord_webhook_url = "https://discord.com/api/webhooks/1/abc"
        # Set directly rather than through Config.load, which enforces a 60s
        # floor; the wait path is still exercised, just not in real time.
        self.cfg.poll_interval_seconds = 0.05
        self.cfg.jitter_seconds = 0
        self.cfg.min_delay_between_groups = 0
        self.cfg.max_delay_between_groups = 0
        self.cfg.notify_on_first_run = True

        self.inbox: dict = {}
        self.notifier = RecordingNotifier()

    def tearDown(self):
        self.tmp.cleanup()

    def watcher(self, scraper=None, **kw):
        w = Watcher(
            self.cfg,
            notifier=self.notifier,
            dispatcher_factory=stub_dispatcher(self.inbox),
            scraper_factory=stub_scraper(scraper) if scraper else None,
            **kw,
        )
        w.reload_inputs()
        return w


# ---------------------------------------------------------------------------
class TestBadConfigDoesNotKillTheDaemon(LoopTestCase):
    """A typo in keywords.txt used to terminate the watcher outright."""

    def test_a_broken_keywords_file_is_survived(self):
        w = self.watcher()
        scraper = StubScraper([make_post("1", "Oddam sobo v Ljubljani", GROUP)])
        (self.base / "keywords.txt").write_text("re:[unclosed\n", encoding="utf-8")

        totals = w.run_cycle(scraper)  # must not raise

        self.assertEqual(totals["seen"], 1)
        # ...and the previous good rules are still in force
        self.assertTrue(w.subscribers[0].matcher.match("Oddam sobo").matched)

    def test_a_broken_subscribers_file_is_survived(self):
        w = self.watcher()
        (self.base / "subscribers.json").write_text("{oops", encoding="utf-8")
        w.run_cycle(StubScraper([]))
        self.assertEqual(len(w.subscribers), 1)

    def test_it_says_so_once_not_every_cycle(self):
        w = self.watcher()
        (self.base / "keywords.txt").write_text("re:[unclosed\n", encoding="utf-8")
        for _ in range(3):
            w.run_cycle(StubScraper([]))
        complaints = [m for m in self.notifier.sent if "cannot read its configuration" in m]
        self.assertEqual(len(complaints), 1)

    def test_a_fixed_file_is_picked_up_and_announced(self):
        w = self.watcher()
        (self.base / "keywords.txt").write_text("re:[unclosed\n", encoding="utf-8")
        w.run_cycle(StubScraper([]))
        (self.base / "keywords.txt").write_text("garsonjera\n", encoding="utf-8")
        w.run_cycle(StubScraper([]))

        self.assertTrue(w.subscribers[0].matcher.match("Oddam garsonjero").matched)
        self.assertTrue(any("readable again" in m for m in self.notifier.sent))


class TestAlertsSurviveAnOutage(LoopTestCase):
    """The alert you need is the one sent while the network is down."""

    def test_a_failed_alert_is_retried_rather_than_latched(self):
        self.notifier.working = False
        w = self.watcher()
        for _ in range(4):
            w._note_failure({"errors": 1, "seen": 0})
        self.assertFalse(w._alerted)  # not latched, so it will try again

        self.notifier.working = True
        w._note_failure({"errors": 1, "seen": 0})
        self.assertTrue(w._alerted)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_a_successful_alert_is_sent_once(self):
        w = self.watcher()
        for _ in range(5):
            w._note_failure({"errors": 1, "seen": 0})
        self.assertEqual(len(self.notifier.sent), 1)

    def test_recovery_is_announced(self):
        w = self.watcher()
        for _ in range(3):
            w._note_failure({"errors": 1, "seen": 0})
        w._note_success()
        self.assertTrue(any("reading groups again" in m for m in self.notifier.sent))

    def test_alerts_fall_back_to_the_control_channel(self):
        # The README says webhooks are optional if you use the bot; without a
        # fallback every operational alert was discarded in that setup.
        class NoWebhook:
            enabled = False

            def send_text(self, message, attempts=4):
                return False

        replies: list[str] = []

        class Control:
            enabled = True

            def reply(self, text):
                replies.append(text)

        w = self.watcher()
        w.notifier = NoWebhook()
        w.control = Control()
        self.assertTrue(w._alert("something broke"))
        self.assertEqual(len(replies), 1)

    def test_nothing_is_sent_when_error_alerts_are_off(self):
        self.cfg.notify_errors = False
        w = self.watcher()
        self.assertFalse(w._alert("quiet please"))
        self.assertEqual(self.notifier.sent, [])


class TestZeroPostsIsNotSuccess(LoopTestCase):
    """The failure that actually happened, and went unnoticed.

    Facebook changed its markup, extraction returned [] with no exception, and
    every cycle was recorded as healthy - so nothing ever complained.
    """

    def setUp(self):
        super().setUp()
        (self.base / "groups.txt").write_text(
            "https://www.facebook.com/groups/555000 | One\n"
            "https://www.facebook.com/groups/777000 | Two\n"
            "https://www.facebook.com/groups/888000 | Three\n",
            encoding="utf-8",
        )

    def test_reading_nothing_anywhere_is_a_failure(self):
        w = self.watcher()
        for _ in range(4):
            w.run_cycle(StubScraper([]))
            if w._consecutive_failures == 0:
                self.fail("a cycle that read no posts was recorded as a success")
        self.assertTrue(any("no posts at all" in m for m in self.notifier.sent))

    def test_the_message_names_the_likely_cause(self):
        w = self.watcher()
        for _ in range(3):
            w.run_cycle(StubScraper([]))
        alert = next(m for m in self.notifier.sent if "no posts at all" in m)
        self.assertIn("markup", alert)
        self.assertIn("dump", alert)  # the command that diagnoses it

    def test_an_errors_cycle_still_says_errors(self):
        w = self.watcher()
        broken = {s: ScrapeError("nope") for s in ("555000", "777000", "888000")}
        for _ in range(3):
            w.run_cycle(StubScraper(per_group=broken))
        self.assertTrue(any("error(s) last cycle" in m for m in self.notifier.sent))

    def test_a_healthy_cycle_resets_the_count(self):
        w = self.watcher()
        w.run_cycle(StubScraper([]))
        self.assertEqual(w._consecutive_failures, 1)
        w.run_cycle(StubScraper([make_post("1", "Oddam sobo", GROUP)]))
        self.assertEqual(w._consecutive_failures, 0)


class TestOneDeadGroupIsNoticed(LoopTestCase):
    """One working group used to hide every other group being dead."""

    def setUp(self):
        super().setUp()
        (self.base / "groups.txt").write_text(
            "https://www.facebook.com/groups/555000 | Working\n"
            "https://www.facebook.com/groups/777000 | Dead\n",
            encoding="utf-8",
        )

    def scraper(self):
        # One group returns posts, the other silently returns nothing.
        return StubScraper(per_group={
            "555000": [make_post("1", "Oddam sobo v Ljubljani", GROUP)],
            "777000": [],
        })

    def test_the_cycle_looks_healthy_but_the_group_is_flagged(self):
        w = self.watcher()
        for _ in range(5):
            w.run_cycle(self.scraper())

        # The cycle totals are fine - that is exactly why this was invisible.
        self.assertEqual(w._consecutive_failures, 0)
        self.assertTrue(any("returned nothing" in m for m in self.notifier.sent))

    def test_it_names_the_group(self):
        w = self.watcher()
        for _ in range(5):
            w.run_cycle(self.scraper())
        alert = next(m for m in self.notifier.sent if "returned nothing" in m)
        self.assertIn("Dead", alert)
        self.assertNotIn("Working", alert)

    def test_it_alerts_once_not_every_cycle(self):
        w = self.watcher()
        for _ in range(10):
            w.run_cycle(self.scraper())
        self.assertEqual(len([m for m in self.notifier.sent if "returned nothing" in m]), 1)

    def test_a_group_coming_back_clears_it(self):
        w = self.watcher()
        for _ in range(5):
            w.run_cycle(self.scraper())
        both_working = StubScraper([make_post("2", "Oddam sobo v Ljubljani", GROUP)])
        w.run_cycle(both_working)
        self.assertEqual(w._group_health.get("777000", 0), 0)

    def test_a_healthy_group_is_never_flagged(self):
        w = self.watcher()
        for _ in range(6):
            w.run_cycle(self.scraper())
        self.assertNotIn("555000", w._group_health)


class TestStopping(LoopTestCase):
    """systemd sends SIGTERM; the loop must exit through its cleanup."""

    def test_request_stop_ends_the_loop_and_saves_state(self):
        scraper = StubScraper([make_post("1", "Oddam sobo v Ljubljani", GROUP)])
        w = self.watcher(scraper)

        def stop_soon():
            time.sleep(0.3)
            w.request_stop("test")

        threading.Thread(target=stop_soon, daemon=True).start()
        code = w.run_forever()

        self.assertEqual(code, 0)
        self.assertTrue(self.cfg.state_path.exists())  # the finally ran
        saved = json.loads(self.cfg.state_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["subscribers"])

    def test_the_browser_is_closed_on_the_way_out(self):
        scraper = StubScraper([])
        w = self.watcher(scraper)
        w.run_forever(max_cycles=1)
        self.assertEqual(scraper.stops, 1)

    def test_stopping_before_it_starts_does_nothing_and_returns_cleanly(self):
        scraper = StubScraper([])
        w = self.watcher(scraper)
        w.request_stop()
        self.assertEqual(w.run_forever(), 0)
        self.assertEqual(scraper.calls, 0)

    def test_a_bounded_run_stops_on_its_own(self):
        scraper = StubScraper([])
        w = self.watcher(scraper)
        self.assertEqual(w.run_forever(max_cycles=2), 0)
        self.assertEqual(scraper.calls, 2)  # one group, two cycles


class TestStartupFailures(LoopTestCase):
    """Previously unreachable: run_forever built its own scraper."""

    def test_a_browser_that_will_not_start_exits_3_and_alerts(self):
        class Broken(StubScraper):
            def start(self):
                raise BrowserUnavailable("Chromium could not start.\nmissing libs\ndetail")

        w = self.watcher(Broken([]))
        self.assertEqual(w.run_forever(max_cycles=1), 3)
        self.assertTrue(any("cannot start Chromium" in m for m in self.notifier.sent))

    def test_a_lost_session_exits_2_and_alerts(self):
        w = self.watcher(StubScraper([], logged_in=False))
        self.assertEqual(w.run_forever(max_cycles=1), 2)
        self.assertTrue(any("session is gone" in m for m in self.notifier.sent))

    def test_login_required_mid_cycle_exits_2(self):
        w = self.watcher(StubScraper(per_group={"555000": LoginRequired("checkpoint")}))
        self.assertEqual(w.run_forever(max_cycles=1), 2)
        self.assertTrue(any("session expired" in m for m in self.notifier.sent))

    def test_a_scrape_error_does_not_end_the_run(self):
        w = self.watcher(StubScraper(per_group={"555000": ScrapeError("temporary")}))
        self.assertEqual(w.run_forever(max_cycles=2), 0)


# ---------------------------------------------------------------------------
class TestStoreWrites(unittest.TestCase):
    """The state file is written by two threads; losing it re-notifies posts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_saves_keep_every_id(self):
        store = SeenStore(self.path)
        errors: list[Exception] = []

        def writer(tag):
            try:
                for i in range(150):
                    store.add(tag, "555000", f"{tag}-{i}")
                    store.save()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b", "c")]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        store.save(force=True)

        self.assertEqual(errors, [])
        reloaded = SeenStore(self.path)
        for tag in ("a", "b", "c"):
            self.assertTrue(reloaded.has(tag, "555000", f"{tag}-149"), tag)

    def test_no_temp_files_are_left_behind(self):
        store = SeenStore(self.path)
        store.add("me", "g", "p")
        store.save()
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_a_failed_write_keeps_the_data_for_the_next_try(self):
        # _dirty used to be cleared before the write, so a disk-full error lost
        # the ids permanently - the next save saw nothing to do.
        store = SeenStore(self.path)
        store.add("me", "g", "p1")

        original = Path.write_text

        def explode(self, *a, **kw):
            if self.suffix.endswith("tmp") or ".tmp" in self.name:
                raise OSError(28, "No space left on device")
            return original(self, *a, **kw)

        Path.write_text = explode
        try:
            self.assertFalse(store.save())
        finally:
            Path.write_text = original

        self.assertTrue(store.save())  # retried, not silently dropped
        self.assertTrue(SeenStore(self.path).has("me", "g", "p1"))

    def test_a_failed_write_does_not_raise(self):
        store = SeenStore(self.path / "nope" / "deeper")  # unwritable parent chain
        store.add("me", "g", "p")
        try:
            store.save()
        except OSError:
            self.fail("save() must never propagate OSError into the watch loop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
