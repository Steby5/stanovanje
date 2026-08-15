"""Tests for configuring the watcher from Discord.

The HTTP layer is stubbed, so these exercise command parsing, permissions, and
the edits made to groups.txt / the per-person keyword files / subscribers.json -
without a bot token.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import StubScraper, make_group, make_post, stub_mailboxes  # noqa: E402

from fbwatch.config import Config  # noqa: E402
from fbwatch.control import DiscordControl  # noqa: E402
from fbwatch.runner import Watcher  # noqa: E402
from fbwatch.subscribers import find_subscriber  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/1/abc"
WEBHOOK2 = "https://discord.com/api/webhooks/2/def"


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Records outgoing calls and replays queued message lists."""

    def __init__(self):
        self.sent: list[str] = []
        self.inbox: list[list[dict]] = []

    def request(self, method, url, **kwargs):
        if method == "POST":
            self.sent.append(kwargs["json"]["content"])
            return FakeResponse({}, 200)
        if "/messages" in url:
            return FakeResponse(self.inbox.pop(0) if self.inbox else [], 200)
        return FakeResponse({"id": "1", "name": "control"}, 200)


def message(content, author_id="42", bot=False, msg_id="100"):
    return {
        "id": msg_id,
        "content": content,
        "author": {"id": author_id, "username": "domin", "bot": bot},
    }


class ControlTestCase(unittest.TestCase):
    #: subscribers.json contents; None means single-user mode.
    subscribers: dict | None = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "groups.txt").write_text(
            "# my groups\nhttps://www.facebook.com/groups/555000 | Test Group\n", encoding="utf-8"
        )
        (self.base / "keywords.txt").write_text(
            "# triggers\noddam + soba\ngarsonjera\n!agencija\n", encoding="utf-8"
        )
        if self.subscribers is not None:
            (self.base / "subscribers.json").write_text(
                json.dumps(self.subscribers), encoding="utf-8"
            )

        self.cfg = Config()
        self.cfg.base_dir = self.base
        self.cfg.discord_webhook_url = WEBHOOK
        self.cfg.discord_bot_token = "test-token"
        self.cfg.discord_control_channel_id = "999"

        self.inbox: dict = {}
        self.watcher = Watcher(self.cfg, mailbox_factory=stub_mailboxes(self.inbox))
        self.watcher.reload_inputs()
        self.session = FakeSession()
        self.control = DiscordControl(self.cfg, self.watcher, session=self.session)

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------
    def rules_of(self, name: str) -> list[str]:
        sub = find_subscriber(self.watcher.subscribers, name)
        path = sub.keywords_path(self.cfg)
        if not path.exists():
            return []
        return [
            ln.strip()
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        ]

    def matcher_of(self, name: str):
        return find_subscriber(self.watcher.subscribers, name).matcher

    def groups_text(self) -> str:
        return (self.base / "groups.txt").read_text(encoding="utf-8")

    def saved_subscribers(self) -> dict:
        return json.loads((self.base / "subscribers.json").read_text(encoding="utf-8"))

    def run_command(self, text: str, author_id: str = "42") -> str:
        reply, _ = self.control.handle(text, author_id)
        return reply


# ---------------------------------------------------------------------------
class TestSingleUserMode(ControlTestCase):
    """No subscribers.json: one implicit admin off config.json + keywords.txt."""

    def test_one_implicit_admin(self):
        self.assertEqual(len(self.watcher.subscribers), 1)
        sub = self.watcher.subscribers[0]
        self.assertTrue(sub.admin)
        self.assertEqual(sub.discord_webhook_url, WEBHOOK)
        self.assertEqual(len(sub.matcher.includes), 2)

    def test_anyone_in_the_channel_is_admin(self):
        self.assertIn("Admin", self.run_command("help", author_id="whoever"))

    def test_add_edits_the_shared_keywords_file(self):
        self.run_command("add enosobno + ljubljana")
        self.assertIn("enosobno + ljubljana", self.rules_of("me"))
        self.assertTrue(self.matcher_of("me").match("Oddam enosobno v Ljubljani").matched)


class TestKeywordCommands(ControlTestCase):
    def test_add_appends_and_applies_immediately(self):
        reply = self.run_command("add enosobno + ljubljana")
        self.assertIn("Added", reply)
        self.assertIn("enosobno + ljubljana", self.rules_of("me"))

    def test_add_rejects_a_broken_rule(self):
        reply = self.run_command("add re:[unclosed")
        self.assertIn("can't read", reply)
        self.assertNotIn("re:[unclosed", self.rules_of("me"))

    def test_add_is_idempotent(self):
        self.run_command("add garsonjera")
        self.assertEqual(self.rules_of("me").count("garsonjera"), 1)

    def test_remove_deletes_the_rule(self):
        self.run_command("remove garsonjera")
        self.assertNotIn("garsonjera", self.rules_of("me"))
        self.assertFalse(self.matcher_of("me").match("Oddam garsonjero").matched)

    def test_remove_reports_a_miss(self):
        self.assertIn("isn't on", self.run_command("remove nonexistent"))

    def test_remove_keeps_comments_intact(self):
        self.run_command("remove garsonjera")
        self.assertIn("# triggers", (self.base / "keywords.txt").read_text(encoding="utf-8"))

    def test_exclude_adds_a_bang_rule(self):
        self.run_command("exclude posrednik")
        self.assertIn("!posrednik", self.rules_of("me"))
        self.assertFalse(self.matcher_of("me").match("Oddam sobo, posrednik").matched)

    def test_exclude_accepts_an_explicit_bang(self):
        self.run_command("exclude !posrednik")
        self.assertIn("!posrednik", self.rules_of("me"))
        self.assertNotIn("!!posrednik", self.rules_of("me"))

    def test_test_command_explains_itself(self):
        self.assertIn("YES", self.run_command("test Oddam sobo v Ljubljani"))
        self.assertIn("No", self.run_command("test Prodam kolo"))

    def test_mine_lists_my_rules(self):
        reply = self.run_command("mine")
        self.assertIn("oddam + soba", reply)
        self.assertIn("!agencija", reply)


class TestGroupCommands(ControlTestCase):
    def test_add_group(self):
        reply = self.run_command("group add https://www.facebook.com/groups/777111 | Nova")
        self.assertIn("Nova", reply)
        self.assertIn("777111", self.groups_text())
        self.assertEqual(len(self.watcher.groups), 2)

    def test_add_group_rejects_junk(self):
        self.run_command("group add totally not a url")
        self.assertNotIn("totally", self.groups_text())

    def test_add_group_is_idempotent(self):
        self.assertIn("already", self.run_command("group add https://www.facebook.com/groups/555000"))

    def test_remove_group_by_name(self):
        self.run_command("group remove Test Group")
        self.assertNotIn("555000", self.groups_text())

    def test_remove_unknown_group(self):
        self.assertIn("No group matches", self.run_command("group remove nope"))


class TestWatcherControls(ControlTestCase):
    def test_pause_and_resume(self):
        self.assertIn("Paused", self.run_command("pause"))
        self.assertTrue(self.watcher.paused)
        self.assertIn("Already paused", self.run_command("pause"))
        self.assertIn("Resumed", self.run_command("resume"))
        self.assertFalse(self.watcher.paused)

    def test_check_sets_the_force_flag(self):
        _, flags = self.control.handle("check", "42")
        self.assertTrue(flags.get("force_check"))

    def test_interval_updates_the_config(self):
        self.run_command("interval 600")
        self.assertEqual(self.cfg.poll_interval_seconds, 600)

    def test_interval_refuses_to_hammer_facebook(self):
        self.run_command("interval 10")
        self.assertEqual(self.cfg.poll_interval_seconds, 300)

    def test_status_reports_the_current_state(self):
        reply = self.run_command("status")
        self.assertIn("watching", reply)
        self.assertIn("Subscribers", reply)

    def test_list_shows_groups_and_people(self):
        reply = self.run_command("list")
        self.assertIn("Test Group", reply)
        self.assertIn("**me**", reply)

    def test_help_and_empty_command(self):
        self.assertIn("your trigger words", self.run_command("help"))
        self.assertIn("your trigger words", self.run_command(""))

    def test_unknown_command_is_friendly(self):
        self.assertIn("Unknown command", self.run_command("frobnicate"))


# ---------------------------------------------------------------------------
class MultiUserTestCase(ControlTestCase):
    subscribers = {
        "domin": {
            "admin": True,
            "keywords_file": "keywords.txt",
            "discord_webhook_url": WEBHOOK,
            "discord_user_id": "42",
        },
        "ana": {
            "keywords_file": "keywords/ana.txt",
            "discord_webhook_url": WEBHOOK2,
            "discord_user_id": "77",
        },
    }

    def setUp(self):
        super().setUp()
        (self.base / "keywords").mkdir(exist_ok=True)
        (self.base / "keywords" / "ana.txt").write_text("garsonjera\n", encoding="utf-8")
        self.watcher.reload_inputs()


class TestPerUserRules(MultiUserTestCase):
    def test_each_person_has_their_own_rules(self):
        self.assertEqual(len(self.matcher_of("domin").includes), 2)
        self.assertEqual(len(self.matcher_of("ana").includes), 1)

    def test_a_user_edits_only_their_own_list(self):
        self.run_command("add dvosobno", author_id="77")  # ana
        self.assertIn("dvosobno", self.rules_of("ana"))
        self.assertNotIn("dvosobno", self.rules_of("domin"))

    def test_reply_names_the_person(self):
        self.assertIn("**ana**", self.run_command("add dvosobno", author_id="77"))

    def test_test_command_is_per_user(self):
        # "Oddam sobo" matches domin's rules but not ana's.
        self.assertIn("YES", self.run_command("test Oddam sobo v Ljubljani", author_id="42"))
        self.assertIn("No", self.run_command("test Oddam sobo v Ljubljani", author_id="77"))

    def test_mine_shows_only_my_rules(self):
        reply = self.run_command("mine", author_id="77")
        self.assertIn("garsonjera", reply)
        self.assertNotIn("oddam + soba", reply)

    def test_unknown_discord_user_is_told_how_to_join(self):
        self.assertIn("don't have a subscription", self.run_command("add soba", author_id="999"))


class TestAdminPermissions(MultiUserTestCase):
    def test_non_admin_cannot_manage_people(self):
        self.assertIn("admin-only", self.run_command("user add bojan", author_id="77"))

    def test_non_admin_cannot_add_groups(self):
        reply = self.run_command("group add https://www.facebook.com/groups/888", author_id="77")
        self.assertIn("admin-only", reply)
        self.assertNotIn("888", self.groups_text())

    def test_non_admin_cannot_pause(self):
        self.run_command("pause", author_id="77")
        self.assertFalse(self.watcher.paused)

    def test_admin_can(self):
        self.run_command("pause", author_id="42")
        self.assertTrue(self.watcher.paused)

    def test_non_admin_help_hides_admin_section(self):
        self.assertNotIn("Admin", self.run_command("help", author_id="77"))

    def test_admin_help_shows_it(self):
        self.assertIn("Admin", self.run_command("help", author_id="42"))


class TestAdminLockoutIsAvoided(ControlTestCase):
    """Linking a normal user must not strip admin from everybody else.

    Admin rights key off whether an *admin* has linked a Discord account. If
    they keyed off any linked user, adding Ana first would lock the owner out
    of their own watcher, fixable only by editing subscribers.json by hand.
    """

    subscribers = {
        "domin": {
            "admin": True,
            "keywords_file": "keywords.txt",
            "discord_webhook_url": WEBHOOK,
            # deliberately NOT linked
        },
        "ana": {
            "keywords_file": "keywords/ana.txt",
            "discord_webhook_url": WEBHOOK2,
            "discord_user_id": "77",
        },
    }

    def setUp(self):
        super().setUp()
        (self.base / "keywords").mkdir(exist_ok=True)
        (self.base / "keywords" / "ana.txt").write_text("garsonjera\n", encoding="utf-8")
        self.watcher.reload_inputs()

    def test_the_unlinked_owner_keeps_admin(self):
        self.run_command("pause", author_id="42")
        self.assertTrue(self.watcher.paused)

    def test_admin_commands_still_reachable(self):
        self.assertIn("Subscribers", self.run_command("users", author_id="42"))

    def test_a_linked_user_still_resolves_to_their_own_rules(self):
        self.run_command("add dvosobno", author_id="77")
        self.assertIn("dvosobno", self.rules_of("ana"))
        self.assertNotIn("dvosobno", self.rules_of("domin"))

    def test_linking_an_admin_switches_to_strict_matching(self):
        self.run_command("user set domin discord 42")
        # Ana is linked and not an admin, so she loses admin rights.
        self.assertIn("admin-only", self.run_command("pause", author_id="77"))
        # A stranger now has no subscription at all.
        self.assertIn("don't have a subscription", self.run_command("add soba", author_id="999"))
        # The owner still administers.
        self.run_command("pause", author_id="42")
        self.assertTrue(self.watcher.paused)


class TestUserManagement(MultiUserTestCase):
    def test_add_a_person(self):
        reply = self.run_command("user add bojan")
        self.assertIn("bojan", reply)
        self.assertIn("bojan", self.saved_subscribers())
        self.assertTrue((self.base / "keywords" / "bojan.txt").exists())

    def test_a_new_person_receives_nothing_until_set_up(self):
        self.run_command("user add bojan")
        bojan = find_subscriber(self.watcher.subscribers, "bojan")
        self.assertFalse(bojan.deliverable)
        self.assertIn("no Discord webhook", bojan.why_idle())

    def test_set_webhook_then_rules_makes_them_live(self):
        self.run_command("user add bojan")
        self.run_command(f"user set bojan webhook {WEBHOOK2}")
        self.run_command("for bojan add garsonjera")
        bojan = find_subscriber(self.watcher.subscribers, "bojan")
        self.assertTrue(bojan.deliverable)

    def test_set_rejects_a_bad_webhook(self):
        self.assertIn("doesn't look like", self.run_command("user set ana webhook http://evil"))

    def test_set_telegram_chat_id(self):
        self.run_command("user set ana telegram 123456789")
        self.assertEqual(self.saved_subscribers()["ana"]["telegram_chat_id"], "123456789")

    def test_set_telegram_rejects_non_numeric(self):
        self.assertIn("is a number", self.run_command("user set ana telegram @ana"))

    def test_disable_and_enable(self):
        self.run_command("user disable ana")
        self.assertFalse(find_subscriber(self.watcher.subscribers, "ana").enabled)
        self.run_command("user enable ana")
        self.assertTrue(find_subscriber(self.watcher.subscribers, "ana").enabled)

    def test_remove_a_person(self):
        self.run_command("user remove ana")
        self.assertNotIn("ana", self.saved_subscribers())

    def test_cannot_remove_the_last_admin(self):
        reply = self.run_command("user remove domin")
        self.assertIn("only admin", reply)
        self.assertIn("domin", self.saved_subscribers())

    def test_admin_edits_someone_elses_rules_with_for(self):
        self.run_command("for ana add dvosobno")
        self.assertIn("dvosobno", self.rules_of("ana"))

    def test_for_an_unknown_person(self):
        self.assertIn("No subscriber", self.run_command("for nobody add soba"))

    def test_users_listing_flags_idle_people(self):
        self.run_command("user add bojan")
        reply = self.run_command("users")
        self.assertIn("bojan", reply)
        self.assertIn("no Discord webhook", reply)


class TestPolling(ControlTestCase):
    def test_a_command_in_the_channel_is_executed(self):
        self.session.inbox = [[message("!fbw add dvosobno")]]
        self.control.poll()
        self.assertIn("dvosobno", self.rules_of("me"))
        self.assertTrue(any("Added" in s for s in self.session.sent))

    def test_messages_without_the_prefix_are_ignored(self):
        self.session.inbox = [[message("just chatting about apartments")]]
        self.control.poll()
        self.assertEqual(self.session.sent, [])

    def test_bot_messages_are_ignored(self):
        # Our own notifications land in the channel; they must not loop.
        self.session.inbox = [[message("!fbw pause", bot=True)]]
        self.control.poll()
        self.assertFalse(self.watcher.paused)

    def test_prefix_is_case_insensitive(self):
        self.session.inbox = [[message("!FBW pause")]]
        self.control.poll()
        self.assertTrue(self.watcher.paused)

    def test_commands_run_oldest_first(self):
        # Discord returns newest-first; pause then resume must end up resumed.
        self.session.inbox = [
            [message("!fbw resume", msg_id="2"), message("!fbw pause", msg_id="1")]
        ]
        self.control.poll()
        self.assertFalse(self.watcher.paused)

    def test_flags_propagate_out_of_poll(self):
        self.session.inbox = [[message("!fbw check")]]
        self.assertTrue(self.control.poll().get("force_check"))

    def test_a_failing_command_does_not_raise(self):
        self.session.inbox = [[message("!fbw interval banana")]]
        self.control.poll()
        self.assertTrue(any("Usage" in s for s in self.session.sent))

    def test_hard_allowlist_blocks_other_users(self):
        self.cfg.control_allowed_user_ids = ["42"]
        control = DiscordControl(self.cfg, self.watcher, session=self.session)
        self.session.inbox = [[message("!fbw pause", author_id="999")]]
        control.poll()
        self.assertFalse(self.watcher.paused)
        self.assertTrue(any("not allowed" in s for s in self.session.sent))


class TestPausedWatcher(ControlTestCase):
    def test_paused_records_posts_but_sends_nothing(self):
        group = make_group()
        self.cfg.notify_on_first_run = True
        self.watcher.paused = True

        scraper = StubScraper([make_post("1", "Oddam sobo v Ljubljani", group)])
        stats = self.watcher.check_group(scraper, group)

        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(self.inbox["me"], [])

        # Resuming must not replay it.
        self.watcher.paused = False
        stats = self.watcher.check_group(scraper, group)
        self.assertEqual(stats["new"], 0)
        self.assertEqual(self.inbox["me"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
