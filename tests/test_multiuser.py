"""Multi-user fan-out: one scrape, independent filtering and history per person.

The properties that matter here are isolation properties - one person's rules,
history, or broken destination must never affect anyone else's notifications.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import StubScraper, make_group, make_post, stub_dispatcher  # noqa: E402

from fbwatch.config import Config  # noqa: E402
from fbwatch.runner import Watcher  # noqa: E402
from fbwatch.store import SeenStore  # noqa: E402
from fbwatch.subscribers import (  # noqa: E402
    Subscriber,
    SubscriberError,
    load_subscribers,
    save_subscribers,
)

WEBHOOK = "https://discord.com/api/webhooks/1/abc"
WEBHOOK2 = "https://discord.com/api/webhooks/2/def"
GROUP = make_group()
OTHER_GROUP = make_group("777000", "Other Group")


class MultiUserBase(unittest.TestCase):
    subscribers: dict = {}
    keyword_files: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "groups.txt").write_text(
            "https://www.facebook.com/groups/555000 | Test Group\n"
            "https://www.facebook.com/groups/777000 | Other Group\n",
            encoding="utf-8",
        )
        (self.base / "keywords.txt").write_text("oddam + soba\n", encoding="utf-8")
        (self.base / "keywords").mkdir(exist_ok=True)
        for name, text in self.keyword_files.items():
            (self.base / "keywords" / f"{name}.txt").write_text(text, encoding="utf-8")
        if self.subscribers:
            (self.base / "subscribers.json").write_text(
                json.dumps(self.subscribers), encoding="utf-8"
            )

        self.cfg = Config()
        self.cfg.base_dir = self.base
        self.cfg.discord_webhook_url = WEBHOOK
        self.cfg.notify_on_first_run = True

        self.inbox: dict = {}
        self.watcher = Watcher(self.cfg, dispatcher_factory=stub_dispatcher(self.inbox))
        self.watcher.reload_inputs()

    def tearDown(self):
        self.tmp.cleanup()

    def received(self, name: str) -> list[str]:
        return [post.post_id for post, _ in self.inbox.get(name, []) if post != "text"]


class TestFanOut(MultiUserBase):
    subscribers = {
        "domin": {"admin": True, "keywords_file": "keywords.txt", "discord_webhook_url": WEBHOOK},
        "ana": {"keywords_file": "keywords/ana.txt", "discord_webhook_url": WEBHOOK2},
    }
    keyword_files = {"ana": "garsonjera\n"}

    def test_each_person_gets_only_what_matches_their_rules(self):
        scraper = StubScraper([
            make_post("1", "Oddam sobo v Ljubljani", GROUP),
            make_post("2", "Oddam garsonjero v centru", GROUP),
            make_post("3", "Prodam kolo", GROUP),
        ])
        self.watcher.check_group(scraper, GROUP)

        self.assertEqual(self.received("domin"), ["1"])
        self.assertEqual(self.received("ana"), ["2"])

    def test_a_post_matching_both_goes_to_both(self):
        scraper = StubScraper([make_post("9", "Oddam sobo in garsonjera", GROUP)])
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(self.received("domin"), ["9"])
        self.assertEqual(self.received("ana"), ["9"])

    def test_facebook_is_scraped_once_regardless_of_subscriber_count(self):
        scraper = StubScraper([make_post("1", "Oddam sobo", GROUP)])
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(scraper.calls, 1)

    def test_totals_count_every_recipient(self):
        scraper = StubScraper([make_post("9", "Oddam sobo in garsonjera", GROUP)])
        stats = self.watcher.check_group(scraper, GROUP)
        self.assertEqual(stats["seen"], 1)  # one post on the page
        self.assertEqual(stats["sent"], 2)  # delivered to two people


class TestHistoryIsolation(MultiUserBase):
    subscribers = {
        "domin": {"admin": True, "keywords_file": "keywords.txt", "discord_webhook_url": WEBHOOK},
        "ana": {"keywords_file": "keywords/ana.txt", "discord_webhook_url": WEBHOOK2},
    }
    keyword_files = {"ana": "oddam + soba\n"}

    def test_nobody_gets_the_same_post_twice(self):
        scraper = StubScraper([make_post("1", "Oddam sobo", GROUP)])
        self.watcher.check_group(scraper, GROUP)
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(self.received("domin"), ["1"])
        self.assertEqual(self.received("ana"), ["1"])

    def test_a_person_added_later_does_not_get_the_backlog(self):
        self.cfg.notify_on_first_run = False
        scraper = StubScraper([make_post("1", "Oddam sobo", GROUP)])
        self.watcher.check_group(scraper, GROUP)  # first poll seeds silently

        # A new person appears; their own first poll must also be silent.
        subs = list(self.watcher.subscribers)
        bojan = Subscriber(name="bojan", discord_webhook_url=WEBHOOK2)
        (self.base / "keywords" / "bojan.txt").write_text("oddam + soba\n", encoding="utf-8")
        save_subscribers(self.cfg, subs + [bojan])
        self.watcher.reload_inputs()

        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(self.received("bojan"), [])

        # ...but they do get the next genuinely new post.
        scraper.posts = [make_post("2", "Oddam sobo drugje", GROUP)] + scraper.posts
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(self.received("bojan"), ["2"])

    def test_a_failed_delivery_only_retries_for_that_person(self):
        self.watcher._dispatcher_factory = stub_dispatcher(self.inbox, fail=("ana",))
        self.watcher.reload_inputs()
        scraper = StubScraper([make_post("1", "Oddam sobo", GROUP)])
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(self.received("domin"), ["1"])
        self.assertEqual(self.received("ana"), [])

        # Ana's delivery starts working: she gets it, domin is not re-sent.
        self.watcher._dispatcher_factory = stub_dispatcher(self.inbox)
        self.watcher.reload_inputs()
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(self.received("domin"), ["1"])
        self.assertEqual(self.received("ana"), ["1"])


class TestGroupSelection(MultiUserBase):
    subscribers = {
        "domin": {"admin": True, "keywords_file": "keywords.txt", "discord_webhook_url": WEBHOOK},
        "ana": {
            "keywords_file": "keywords/ana.txt",
            "discord_webhook_url": WEBHOOK2,
            "groups": ["777000"],
        },
    }
    keyword_files = {"ana": "oddam + soba\n"}

    def test_a_person_can_watch_a_subset_of_groups(self):
        scraper = StubScraper([make_post("1", "Oddam sobo", GROUP)])
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(self.received("domin"), ["1"])
        self.assertEqual(self.received("ana"), [])

    def test_they_still_get_their_own_group(self):
        scraper = StubScraper([make_post("2", "Oddam sobo", OTHER_GROUP)])
        self.watcher.check_group(scraper, OTHER_GROUP)
        self.assertEqual(self.received("ana"), ["2"])


class TestIdleSubscribers(MultiUserBase):
    subscribers = {
        "domin": {"admin": True, "keywords_file": "keywords.txt", "discord_webhook_url": WEBHOOK},
        "nodest": {"keywords_file": "keywords/nodest.txt"},
        "norules": {"keywords_file": "keywords/norules.txt", "discord_webhook_url": WEBHOOK2},
        "off": {
            "enabled": False,
            "keywords_file": "keywords/off.txt",
            "discord_webhook_url": WEBHOOK2,
        },
    }
    keyword_files = {"nodest": "oddam + soba\n", "norules": "# nothing\n", "off": "oddam + soba\n"}

    def test_only_fully_configured_people_are_active(self):
        self.assertEqual([s.name for s in self.watcher.active_subscribers], ["domin"])

    def test_each_idle_reason_is_explained(self):
        reasons = {s.name: s.why_idle() for s in self.watcher.subscribers}
        self.assertEqual(reasons["domin"], "")
        self.assertIn("no Discord channel", reasons["nodest"])
        self.assertIn("no trigger words", reasons["norules"])
        self.assertEqual(reasons["off"], "disabled")

    def test_no_trigger_words_means_nothing_rather_than_everything(self):
        # The safety property: an empty rule file must not mean "send me all".
        scraper = StubScraper([make_post("1", "Prodam kolo", GROUP)])
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(self.received("norules"), [])


class TestSubscriberFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "keywords.txt").write_text("soba\n", encoding="utf-8")
        self.cfg = Config()
        self.cfg.base_dir = self.base
        self.cfg.discord_webhook_url = WEBHOOK

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data: dict):
        (self.base / "subscribers.json").write_text(json.dumps(data), encoding="utf-8")

    def test_missing_file_yields_single_user_mode(self):
        subs = load_subscribers(self.cfg)
        self.assertEqual(len(subs), 1)
        self.assertTrue(subs[0].admin)
        self.assertEqual(subs[0].discord_webhook_url, WEBHOOK)

    def test_round_trip(self):
        original = [
            Subscriber(name="domin", admin=True, discord_webhook_url=WEBHOOK),
            Subscriber(name="ana", telegram_chat_id="123", groups=["555000"]),
        ]
        save_subscribers(self.cfg, original)
        loaded = load_subscribers(self.cfg)
        self.assertEqual([s.name for s in loaded], ["domin", "ana"])
        self.assertTrue(loaded[0].admin)
        self.assertEqual(loaded[1].telegram_chat_id, "123")
        self.assertEqual(loaded[1].groups, ["555000"])

    def test_comment_keys_are_ignored(self):
        self.write({"_comment": "hello", "ana": {"discord_webhook_url": WEBHOOK}})
        self.assertEqual([s.name for s in load_subscribers(self.cfg)], ["ana"])

    def test_unknown_setting_is_reported(self):
        self.write({"ana": {"webhook": WEBHOOK}})
        with self.assertRaises(SubscriberError) as ctx:
            load_subscribers(self.cfg)
        self.assertIn("webhook", str(ctx.exception))

    def test_bad_name_is_rejected(self):
        with self.assertRaises(SubscriberError):
            Subscriber(name="not a name!")

    def test_duplicate_discord_ids_are_rejected(self):
        self.write({
            "a": {"discord_user_id": "1", "discord_webhook_url": WEBHOOK},
            "b": {"discord_user_id": "1", "discord_webhook_url": WEBHOOK},
        })
        with self.assertRaises(SubscriberError):
            load_subscribers(self.cfg)

    def test_invalid_json_is_reported(self):
        (self.base / "subscribers.json").write_text("{oops", encoding="utf-8")
        with self.assertRaises(SubscriberError):
            load_subscribers(self.cfg)

    def test_environment_overrides_the_webhook(self):
        import os

        self.write({"ana": {"keywords_file": "keywords.txt"}})
        os.environ["FBWATCH_WEBHOOK_ANA"] = WEBHOOK2
        try:
            self.assertEqual(load_subscribers(self.cfg)[0].discord_webhook_url, WEBHOOK2)
        finally:
            del os.environ["FBWATCH_WEBHOOK_ANA"]


class TestStoreMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_1_state_is_inherited_not_replayed(self):
        # What an older single-user install left behind.
        self.path.write_text(
            json.dumps({"version": 1, "groups": {"555000": {"111": time.time()}}}),
            encoding="utf-8",
        )
        store = SeenStore(self.path)
        self.assertEqual(store.adopt_legacy("domin"), 1)
        self.assertTrue(store.has("domin", "555000", "111"))
        self.assertTrue(store.knows_group("domin", "555000"))
        # Someone else starts fresh, so their own first poll seeds silently.
        self.assertFalse(store.knows_group("ana", "555000"))

    def test_adopting_twice_is_harmless(self):
        self.path.write_text(
            json.dumps({"version": 1, "groups": {"555000": {"111": time.time()}}}),
            encoding="utf-8",
        )
        store = SeenStore(self.path)
        store.adopt_legacy("domin")
        self.assertEqual(store.adopt_legacy("domin"), 0)

    def test_histories_are_independent(self):
        store = SeenStore(self.path)
        store.add("domin", "555000", "111")
        self.assertTrue(store.has("domin", "555000", "111"))
        self.assertFalse(store.has("ana", "555000", "111"))

    def test_removing_a_person_drops_their_history(self):
        store = SeenStore(self.path)
        store.add("ana", "555000", "111")
        store.drop_subscriber("ana")
        self.assertFalse(store.has("ana", "555000", "111"))

    def test_round_trip_through_disk(self):
        store = SeenStore(self.path)
        store.add("domin", "555000", "111")
        store.save()
        self.assertTrue(SeenStore(self.path).has("domin", "555000", "111"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
