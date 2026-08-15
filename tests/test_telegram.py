"""Telegram delivery: message formatting, error handling, chat discovery."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import make_group, make_post  # noqa: E402

from fbwatch.config import Config  # noqa: E402
from fbwatch.delivery import Dispatcher  # noqa: E402
from fbwatch.matcher import KeywordMatcher, MatchResult  # noqa: E402
from fbwatch.subscribers import Subscriber  # noqa: E402
from fbwatch.telegram import TelegramNotifier, recent_chats  # noqa: E402

GROUP = make_group(name="Najem stanovanj LJ")
WEBHOOK = "https://discord.com/api/webhooks/1/abc"


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, status=200, payload=None):
        self.posts: list[dict] = []
        self.gets: list[str] = []
        self.status = status
        self.payload = payload if payload is not None else {"ok": True}

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "body": json})
        return FakeResponse(self.payload, self.status)

    def get(self, url, timeout=None):
        self.gets.append(url)
        return FakeResponse(self.payload, self.status)


def cfg_with_token(token="123:ABC"):
    cfg = Config()
    cfg.telegram_bot_token = token
    return cfg


class TestMessageBuilding(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_with_token()
        self.notifier = TelegramNotifier(self.cfg, "999")

    def test_message_has_group_author_text_and_link(self):
        post = make_post("42", "Oddam lepo sobo v Ljubljani, 400 EUR", GROUP)
        result = KeywordMatcher.from_lines(["oddam + soba"]).match(post.text)
        text = self.notifier.build_message(post, result)

        self.assertIn("Najem stanovanj LJ", text)
        self.assertIn("Ana Novak", text)
        self.assertIn("Oddam lepo sobo v Ljubljani", text)
        self.assertIn(post.url, text)
        self.assertIn("oddam + soba", text)

    def test_html_in_a_post_is_escaped(self):
        # A post containing markup must not break Telegram's HTML parse mode.
        post = make_post("42", "Oddam <b>sobo</b> & balkon", GROUP)
        text = self.notifier.build_message(post, MatchResult(matched=True))
        self.assertIn("&lt;b&gt;sobo&lt;/b&gt;", text)
        self.assertIn("&amp;", text)

    def test_long_post_is_truncated(self):
        post = make_post("42", "Oddam sobo. " + "beseda " * 3000, GROUP)
        text = self.notifier.build_message(post, MatchResult(matched=True))
        self.assertLess(len(text), 4096)

    def test_post_without_text_still_builds(self):
        post = make_post("42", "", GROUP)
        text = self.notifier.build_message(post, MatchResult(matched=True))
        self.assertIn("no text", text)
        self.assertIn(post.url, text)


class TestSending(unittest.TestCase):
    def test_sends_to_the_right_chat(self):
        session = FakeSession()
        notifier = TelegramNotifier(cfg_with_token(), "999", session=session)
        post = make_post("1", "Oddam sobo", GROUP)

        self.assertTrue(notifier.send_post(post, MatchResult(matched=True)))
        body = session.posts[0]["body"]
        self.assertEqual(body["chat_id"], "999")
        self.assertEqual(body["parse_mode"], "HTML")
        self.assertIn("/bot123:ABC/sendMessage", session.posts[0]["url"])

    def test_disabled_without_a_token(self):
        notifier = TelegramNotifier(Config(), "999")
        self.assertFalse(notifier.enabled)
        self.assertFalse(notifier.send_post(make_post("1", "x", GROUP), MatchResult(matched=True)))

    def test_disabled_without_a_chat_id(self):
        self.assertFalse(TelegramNotifier(cfg_with_token(), "").enabled)

    def test_client_error_is_not_retried(self):
        # A blocked bot or wrong chat id will never succeed - fail fast.
        session = FakeSession(status=403, payload={"ok": False, "description": "blocked"})
        notifier = TelegramNotifier(cfg_with_token(), "999", session=session)
        self.assertFalse(notifier.send_post(make_post("1", "x", GROUP), MatchResult(matched=True)))
        self.assertEqual(len(session.posts), 1)


class TestRecentChats(unittest.TestCase):
    def test_lists_unique_chats(self):
        payload = {
            "ok": True,
            "result": [
                {"message": {"chat": {"id": 111, "first_name": "Ana", "username": "ana_lj"}}},
                {"message": {"chat": {"id": 111, "first_name": "Ana", "username": "ana_lj"}}},
                {"message": {"chat": {"id": 222, "first_name": "Marko", "last_name": "K"}}},
            ],
        }
        chats = recent_chats(cfg_with_token(), session=FakeSession(payload=payload))
        self.assertEqual([c["chat_id"] for c in chats], ["111", "222"])
        self.assertEqual(chats[0]["username"], "ana_lj")
        self.assertEqual(chats[1]["name"], "Marko K")

    def test_no_token_yields_nothing(self):
        self.assertEqual(recent_chats(Config()), [])

    def test_api_error_yields_nothing(self):
        self.assertEqual(recent_chats(cfg_with_token(), session=FakeSession(status=401)), [])


class TestDispatcherRouting(unittest.TestCase):
    """A subscriber may have Discord, Telegram, or both."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_with_token()
        self.cfg.base_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def describe(self, sub, cfg=None):
        return Dispatcher(cfg or self.cfg, [sub]).describe(sub)

    def test_discord_only(self):
        self.assertEqual(self.describe(Subscriber(name="ana", discord_webhook_url=WEBHOOK)), "Discord")

    def test_telegram_only(self):
        self.assertEqual(self.describe(Subscriber(name="ana", telegram_chat_id="999")), "Telegram")

    def test_both(self):
        sub = Subscriber(name="ana", discord_webhook_url=WEBHOOK, telegram_chat_id="999")
        self.assertEqual(self.describe(sub), "Discord, Telegram")

    def test_neither(self):
        self.assertEqual(self.describe(Subscriber(name="ana")), "nowhere")

    def test_telegram_ignored_without_a_bot_token(self):
        cfg = Config()
        cfg.base_dir = Path(self.tmp.name)
        self.assertEqual(self.describe(Subscriber(name="ana", telegram_chat_id="999"), cfg), "nowhere")

    def test_describe_flags_a_shared_channel(self):
        subs = [
            Subscriber(name="ana", discord_webhook_url=WEBHOOK),
            Subscriber(name="bo", discord_webhook_url=WEBHOOK),
            Subscriber(name="cvet", discord_webhook_url="https://discord.com/api/webhooks/9/zzz"),
        ]
        dispatcher = Dispatcher(self.cfg, subs)
        self.assertIn("shared with 1", dispatcher.describe(subs[0]))
        self.assertEqual(dispatcher.describe(subs[2]), "Discord")

    def test_telegram_delivers_per_person(self):
        session = FakeSession()
        subs = [
            Subscriber(name="ana", telegram_chat_id="111"),
            Subscriber(name="bo", telegram_chat_id="222"),
        ]
        dispatcher = Dispatcher(self.cfg, subs, session=session)
        post = make_post("1", "Oddam sobo", GROUP)
        result = MatchResult(matched=True)

        delivered = dispatcher.deliver(post, [(subs[0], result), (subs[1], result)])
        self.assertEqual(delivered, {"ana", "bo"})
        self.assertEqual([p["body"]["chat_id"] for p in session.posts], ["111", "222"])

    def test_a_broken_channel_does_not_lose_the_other(self):
        class Broken:
            enabled = True

            def send_post(self, post, result):
                raise RuntimeError("boom")

        sub_tg = Subscriber(name="ana", telegram_chat_id="111")
        session = FakeSession()
        dispatcher = Dispatcher(self.cfg, [sub_tg], session=session)
        # Pretend Ana also has a Discord webhook whose notifier explodes.
        sub_tg.discord_webhook_url = WEBHOOK
        dispatcher._discord[WEBHOOK] = Broken()

        delivered = dispatcher.deliver(
            make_post("1", "x", GROUP), [(sub_tg, MatchResult(matched=True))]
        )
        self.assertEqual(delivered, {"ana"})  # Telegram still got through


if __name__ == "__main__":
    unittest.main(verbosity=2)
