"""A shared server: people set themselves up from the channel they're in.

The flow this covers: someone types a command in the control channel, gets a
subscription without an admin doing anything, and their listings arrive in that
same channel - unless they point their own channel id at it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import StubScraper, make_group, make_post, stub_dispatcher  # noqa: E402

from fbwatch.config import Config  # noqa: E402
from fbwatch.control import DiscordControl, _safe_name  # noqa: E402
from fbwatch.delivery import Dispatcher  # noqa: E402
from fbwatch.runner import Watcher  # noqa: E402
from fbwatch.subscribers import Subscriber, find_subscriber, load_subscribers  # noqa: E402

CONTROL_CHANNEL = "778625216570064902"
OTHER_CHANNEL = "999888777666555444"
WEBHOOK = "https://discord.com/api/webhooks/1/abc"
GROUP = make_group()


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.sent: list[str] = []
        self.inbox: list[list[dict]] = []

    def request(self, method, url, **kwargs):
        if method == "POST":
            self.sent.append(kwargs["json"]["content"])
            return FakeResponse({}, 200)
        if "/messages" in url:
            return FakeResponse(self.inbox.pop(0) if self.inbox else [], 200)
        return FakeResponse({"id": "1"}, 200)


def message(content, author_id="700", username="ana", msg_id="1"):
    return {
        "id": msg_id,
        "content": content,
        "author": {"id": author_id, "username": username, "bot": False},
    }


class SharedServerTestCase(unittest.TestCase):
    subscribers: dict | None = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "groups.txt").write_text(
            "https://www.facebook.com/groups/555000 | Test Group\n", encoding="utf-8"
        )
        (self.base / "keywords.txt").write_text("oddam + soba\n", encoding="utf-8")
        if self.subscribers is not None:
            (self.base / "subscribers.json").write_text(
                json.dumps(self.subscribers), encoding="utf-8"
            )

        self.cfg = Config()
        self.cfg.base_dir = self.base
        self.cfg.discord_bot_token = "bot-token"
        self.cfg.discord_control_channel_id = CONTROL_CHANNEL
        self.cfg.notify_on_first_run = True

        self.inbox: dict = {}
        self.batches: list = []
        self.watcher = Watcher(
            self.cfg, dispatcher_factory=stub_dispatcher(self.inbox, batches=self.batches)
        )
        self.watcher.reload_inputs()
        self.session = FakeSession()
        self.control = DiscordControl(self.cfg, self.watcher, session=self.session)

    def tearDown(self):
        self.tmp.cleanup()

    def run_command(self, text, author_id="700", username="ana"):
        reply, _ = self.control.handle(text, author_id, username)
        return reply

    def sub(self, name):
        return find_subscriber(self.watcher.subscribers, name)

    def rules_of(self, name):
        path = self.sub(name).keywords_path(self.cfg)
        return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
class TestDefaultChannel(SharedServerTestCase):
    """With no destination of their own, people are notified where they type."""

    def test_the_control_channel_is_the_default_destination(self):
        me = self.watcher.subscribers[0]
        me.discord_webhook_url = ""  # ignore config.json's webhook for this test
        self.assertEqual(me.effective_channel_id, CONTROL_CHANNEL)
        self.assertTrue(me.uses_default_channel)
        self.assertTrue(me.has_destination)

    def test_a_new_person_only_needs_trigger_words(self):
        ana = Subscriber(name="ana")
        ana.fallback_channel_id = CONTROL_CHANNEL
        self.assertTrue(ana.has_destination)
        self.assertIn("no trigger words", ana.why_idle())

    def test_it_is_not_written_into_subscribers_json(self):
        # The default must stay dynamic, or moving the control channel would
        # leave everyone pointed at the old one.
        ana = Subscriber(name="ana")
        ana.fallback_channel_id = CONTROL_CHANNEL
        self.assertNotIn("fallback_channel_id", ana.to_dict())
        self.assertNotIn("discord_channel_id", ana.to_dict())

    def test_no_default_without_a_bot(self):
        cfg = Config()
        cfg.base_dir = self.base
        cfg.discord_control_channel_id = CONTROL_CHANNEL  # but no bot token
        subs = load_subscribers(cfg)
        self.assertEqual(subs[0].fallback_channel_id, "")

    def test_the_default_can_be_turned_off(self):
        self.cfg.notify_in_control_channel = False
        subs = load_subscribers(self.cfg)
        self.assertEqual(subs[0].fallback_channel_id, "")

    def test_everyone_on_the_default_shares_one_message(self):
        subs = [Subscriber(name="ana"), Subscriber(name="bo")]
        for s in subs:
            s.fallback_channel_id = CONTROL_CHANNEL
        dispatcher = Dispatcher(self.cfg, subs)
        self.assertEqual(dispatcher.target_of(subs[0]), dispatcher.target_of(subs[1]))
        self.assertEqual(dispatcher.describe(subs[0]), "this channel — shared with 1")


class TestSelfSignup(SharedServerTestCase):
    subscribers = {"domin": {"admin": True, "keywords_file": "keywords.txt",
                             "discord_user_id": "42"}}

    def test_a_stranger_subscribes_by_using_a_command(self):
        reply = self.run_command("add garsonjera", author_id="700", username="ana")
        self.assertIn("Added", reply)
        self.assertIsNotNone(self.sub("ana"))
        self.assertIn("garsonjera", self.rules_of("ana"))

    def test_they_are_linked_to_their_discord_account(self):
        self.run_command("add garsonjera", author_id="700", username="ana")
        self.assertEqual(self.sub("ana").discord_user_id, "700")

    def test_join_introduces_without_adding_a_rule(self):
        reply = self.run_command("join", author_id="700", username="ana")
        self.assertIn("Welcome", reply)
        self.assertIsNotNone(self.sub("ana"))

    def test_a_second_command_reuses_the_same_subscription(self):
        self.run_command("add garsonjera", author_id="700", username="ana")
        self.run_command("add dvosobno", author_id="700", username="ana")
        self.assertEqual(len([s for s in self.watcher.subscribers if s.name == "ana"]), 1)

    def test_two_people_with_the_same_username_do_not_collide(self):
        self.run_command("join", author_id="700", username="ana")
        self.run_command("join", author_id="8001234", username="ana")
        names = sorted(s.name for s in self.watcher.subscribers)
        self.assertEqual(len(set(names)), len(names))
        self.assertIn("ana", names)

    def test_an_unusable_username_still_works(self):
        self.run_command("join", author_id="700123456", username="!!!")
        self.assertIsNotNone(self.sub("user123456"))

    def test_they_land_on_the_shared_channel_by_default(self):
        self.run_command("add garsonjera", author_id="700", username="ana")
        self.assertEqual(self.sub("ana").effective_channel_id, CONTROL_CHANNEL)

    def test_signup_does_not_grant_admin(self):
        self.run_command("join", author_id="700", username="ana")
        self.assertFalse(self.sub("ana").admin)
        self.assertIn("admin-only", self.run_command("pause", author_id="700", username="ana"))

    def test_it_survives_a_reload(self):
        self.run_command("add garsonjera", author_id="700", username="ana")
        self.assertIn("ana", json.loads(
            (self.base / "subscribers.json").read_text(encoding="utf-8")))


class TestChannelCommand(SharedServerTestCase):
    subscribers = {
        "domin": {"admin": True, "keywords_file": "keywords.txt", "discord_user_id": "42"},
        "ana": {"keywords_file": "keywords.txt", "discord_user_id": "700"},
    }

    def test_a_user_redirects_their_own_listings(self):
        reply = self.run_command(f"channel {OTHER_CHANNEL}", author_id="700")
        self.assertIn(OTHER_CHANNEL, reply)
        self.assertEqual(self.sub("ana").discord_channel_id, OTHER_CHANNEL)

    def test_it_does_not_touch_anyone_else(self):
        self.run_command(f"channel {OTHER_CHANNEL}", author_id="700")
        self.assertEqual(self.sub("domin").discord_channel_id, "")

    def test_here_puts_them_back_on_the_shared_channel(self):
        self.run_command(f"channel {OTHER_CHANNEL}", author_id="700")
        self.run_command("channel here", author_id="700")
        self.assertEqual(self.sub("ana").discord_channel_id, "")
        self.assertEqual(self.sub("ana").effective_channel_id, CONTROL_CHANNEL)

    def test_bare_channel_reports_where_listings_go(self):
        self.assertIn("ana", self.run_command("channel", author_id="700"))

    def test_a_non_numeric_id_is_refused(self):
        self.run_command("channel #stanovanja", author_id="700")
        self.assertEqual(self.sub("ana").discord_channel_id, "")

    def test_setting_a_channel_is_not_admin_only(self):
        reply = self.run_command(f"channel {OTHER_CHANNEL}", author_id="700")
        self.assertNotIn("admin-only", reply)

    def test_a_webhook_takes_precedence_and_says_so(self):
        self.sub("ana").discord_webhook_url = WEBHOOK
        reply = self.run_command(f"channel {OTHER_CHANNEL}", author_id="700")
        self.assertIn("webhook", reply.lower())


class TestMentionCommand(SharedServerTestCase):
    subscribers = {
        "domin": {"admin": True, "keywords_file": "keywords.txt", "discord_user_id": "42"},
        "ana": {"keywords_file": "keywords.txt", "discord_user_id": "700"},
    }

    def test_a_user_turns_their_own_pings_off(self):
        self.run_command("mention off", author_id="700")
        self.assertFalse(self.sub("ana").mention)
        self.assertTrue(self.sub("domin").mention)

    def test_and_back_on(self):
        self.run_command("mention off", author_id="700")
        self.run_command("mention on", author_id="700")
        self.assertTrue(self.sub("ana").mention)

    def test_bare_mention_reports_the_setting(self):
        self.assertIn("on", self.run_command("mention", author_id="700").lower())


class TestEndToEnd(SharedServerTestCase):
    """Someone joins, sets rules, and a matching post reaches the right channel."""

    subscribers = {"domin": {"admin": True, "keywords_file": "keywords.txt",
                             "discord_user_id": "42"}}

    def test_join_then_receive_in_the_shared_channel(self):
        self.session.inbox = [[message("!fbw add garsonjera", author_id="700", username="ana")]]
        self.control.poll()

        scraper = StubScraper([make_post("1", "Oddam garsonjero v centru", GROUP)])
        self.watcher.check_group(scraper, GROUP)

        self.assertEqual(self.batches, [(f"channel:{CONTROL_CHANNEL}", ["ana"])])

    def test_after_redirecting_they_receive_elsewhere(self):
        self.run_command("add garsonjera", author_id="700", username="ana")
        self.run_command(f"channel {OTHER_CHANNEL}", author_id="700", username="ana")

        scraper = StubScraper([make_post("1", "Oddam garsonjero v centru", GROUP)])
        self.watcher.check_group(scraper, GROUP)

        self.assertEqual(self.batches, [(f"channel:{OTHER_CHANNEL}", ["ana"])])


class TestCommandsDuringACycle(SharedServerTestCase):
    """Commands must be answered mid-cycle, not only between cycles.

    A pass over several groups takes minutes; a command left unanswered that
    long looks like a dead bot, which is exactly how this was first reported.
    """

    def test_the_between_groups_pause_reads_discord(self):
        self.cfg.min_delay_between_groups = 0.01
        self.cfg.max_delay_between_groups = 0.02
        self.cfg.control_poll_seconds = 2  # longer than the pause
        (self.base / "groups.txt").write_text(
            "https://www.facebook.com/groups/555000 | One\n"
            "https://www.facebook.com/groups/777000 | Two\n",
            encoding="utf-8",
        )
        self.watcher.reload_inputs()
        self.watcher.control = self.control
        self.session.inbox = [[message("!fbw pause", author_id="42", username="domin")]]

        class Scraper:
            def scrape_group(self, group, limit=None):
                return []

        self.watcher.run_cycle(Scraper())
        self.assertTrue(self.watcher.paused)  # handled during the cycle

    def test_a_sleep_without_control_still_sleeps(self):
        self.watcher.control = None
        self.assertEqual(self.watcher._sleep(0.01), {})


class TestSafeName(unittest.TestCase):
    def test_usernames_become_usable_names(self):
        self.assertEqual(_safe_name("Domin"), "domin")
        self.assertEqual(_safe_name("ana.novak"), "ana.novak")
        self.assertEqual(_safe_name("Marko K!"), "marko-k")

    def test_unusable_ones_yield_nothing(self):
        self.assertEqual(_safe_name(""), "")
        self.assertEqual(_safe_name("!!!"), "")
        self.assertEqual(_safe_name("čšž"), "")

    def test_length_is_capped(self):
        self.assertLessEqual(len(_safe_name("a" * 80)), 32)


if __name__ == "__main__":
    unittest.main(verbosity=2)
