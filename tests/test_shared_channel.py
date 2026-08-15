"""One channel, several readers: post once, @-mention whoever it matched.

Discord can't show a message to only some people in a channel, so a shared
channel means everyone sees every matched post and only the relevant people get
pinged.  What must hold: one message rather than one per person, mentions
limited to the people who actually matched, and no way for a post's own text to
ping the server.
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
from fbwatch.delivery import Dispatcher  # noqa: E402
from fbwatch.matcher import KeywordMatcher, MatchResult  # noqa: E402
from fbwatch.runner import Watcher  # noqa: E402
from fbwatch.subscribers import Subscriber  # noqa: E402

SHARED = "https://discord.com/api/webhooks/1/shared"
PRIVATE = "https://discord.com/api/webhooks/2/private"
GROUP = make_group()


class FakeResponse:
    status_code = 204
    text = ""

    def json(self):
        return {}


class RecordingSession:
    """Captures webhook payloads without any network."""

    def __init__(self):
        self.posts: list[dict] = []

    def post(self, url, json=None, **kwargs):
        self.posts.append({"url": url, "body": json, "headers": kwargs.get("headers") or {}})
        return FakeResponse()


def match(*rules):
    return MatchResult(matched=True, matched_rules=list(rules))


# ---------------------------------------------------------------------------
class TestGroupedDelivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config()
        self.cfg.base_dir = Path(self.tmp.name)
        self.session = RecordingSession()

        self.ana = Subscriber(name="ana", discord_webhook_url=SHARED, discord_user_id="111")
        self.bo = Subscriber(name="bo", discord_webhook_url=SHARED, discord_user_id="222")
        self.solo = Subscriber(name="solo", discord_webhook_url=PRIVATE, discord_user_id="333")
        self.dispatcher = Dispatcher(
            self.cfg, [self.ana, self.bo, self.solo], session=self.session
        )
        self.post = make_post("42", "Oddam sobo v Ljubljani, 400 EUR", GROUP)

    def tearDown(self):
        self.tmp.cleanup()

    def body(self, index=0):
        return self.session.posts[index]["body"]

    def test_people_sharing_a_channel_get_one_message(self):
        delivered = self.dispatcher.deliver(
            self.post, [(self.ana, match("soba")), (self.bo, match("oddam"))]
        )
        self.assertEqual(delivered, {"ana", "bo"})
        self.assertEqual(len(self.session.posts), 1)

    def test_that_message_mentions_both(self):
        self.dispatcher.deliver(self.post, [(self.ana, match("soba")), (self.bo, match("oddam"))])
        self.assertIn("<@111>", self.body()["content"])
        self.assertIn("<@222>", self.body()["content"])

    def test_only_matching_people_are_mentioned(self):
        self.dispatcher.deliver(self.post, [(self.ana, match("soba"))])
        content = self.body()["content"]
        self.assertIn("<@111>", content)
        self.assertNotIn("<@222>", content)

    def test_separate_channels_still_get_separate_messages(self):
        self.dispatcher.deliver(
            self.post, [(self.ana, match("soba")), (self.solo, match("soba"))]
        )
        self.assertEqual(len(self.session.posts), 2)
        urls = {p["url"] for p in self.session.posts}
        self.assertEqual(urls, {SHARED, PRIVATE})

    def test_the_post_link_survives_the_mentions(self):
        self.dispatcher.deliver(self.post, [(self.ana, match("soba"))])
        self.assertIn(self.post.url, self.body()["content"])


class TestMentionSafety(TestGroupedDelivery):
    def test_a_post_cannot_ping_the_whole_server(self):
        # The real hazard: someone reposts a listing containing "@everyone".
        shouty = make_post("9", "@everyone @here Oddam sobo! @Moderators", GROUP)
        self.dispatcher.deliver(shouty, [(self.ana, match("soba"))])

        allowed = self.body()["allowed_mentions"]
        self.assertEqual(allowed["parse"], [])       # no everyone/here/roles
        self.assertEqual(allowed["users"], ["111"])  # only the matched person

    def test_only_matched_ids_are_whitelisted(self):
        self.dispatcher.deliver(self.post, [(self.ana, match("soba"))])
        self.assertEqual(self.body()["allowed_mentions"]["users"], ["111"])

    def test_a_person_can_opt_out_of_being_pinged(self):
        self.bo.mention = False
        self.dispatcher.deliver(self.post, [(self.ana, match("soba")), (self.bo, match("oddam"))])
        content = self.body()["content"]
        self.assertIn("<@111>", content)
        self.assertNotIn("<@222>", content)
        # ...but they still receive the post
        self.assertEqual(self.body()["allowed_mentions"]["users"], ["111"])

    def test_unlinked_people_simply_are_not_mentioned(self):
        nolink = Subscriber(name="nolink", discord_webhook_url=SHARED)
        dispatcher = Dispatcher(self.cfg, [nolink], session=self.session)
        delivered = dispatcher.deliver(self.post, [(nolink, match("soba"))])
        self.assertEqual(delivered, {"nolink"})
        self.assertEqual(self.body()["allowed_mentions"]["users"], [])


class TestSharedEmbed(TestGroupedDelivery):
    def test_a_single_recipient_keeps_the_plain_embed(self):
        self.dispatcher.deliver(self.post, [(self.ana, match("oddam + soba"))])
        fields = {f["name"]: f["value"] for f in self.body()["embeds"][0]["fields"]}
        self.assertIn("oddam + soba", fields["Matched"])

    def test_several_recipients_get_a_breakdown(self):
        self.dispatcher.deliver(
            self.post, [(self.ana, match("garsonjera")), (self.bo, match("oddam + soba"))]
        )
        fields = {f["name"]: f["value"] for f in self.body()["embeds"][0]["fields"]}
        breakdown = fields["Matched for 2"]
        self.assertIn("<@111>", breakdown)
        self.assertIn("garsonjera", breakdown)
        self.assertIn("<@222>", breakdown)
        self.assertIn("oddam + soba", breakdown)

    def test_the_post_text_is_still_there(self):
        self.dispatcher.deliver(self.post, [(self.ana, match("a")), (self.bo, match("b"))])
        self.assertIn("Oddam sobo v Ljubljani", self.body()["embeds"][0]["description"])


# ---------------------------------------------------------------------------
class TestSharedChannelThroughTheWatcher(unittest.TestCase):
    """The same behaviour, driven end to end by a scrape cycle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "groups.txt").write_text(
            "https://www.facebook.com/groups/555000 | Test Group\n", encoding="utf-8"
        )
        (self.base / "keywords").mkdir()
        (self.base / "keywords" / "ana.txt").write_text("garsonjera\n", encoding="utf-8")
        (self.base / "keywords" / "bo.txt").write_text("oddam + soba\n", encoding="utf-8")
        (self.base / "subscribers.json").write_text(json.dumps({
            "ana": {"keywords_file": "keywords/ana.txt",
                    "discord_webhook_url": SHARED, "discord_user_id": "111"},
            "bo": {"keywords_file": "keywords/bo.txt",
                   "discord_webhook_url": SHARED, "discord_user_id": "222"},
        }), encoding="utf-8")

        self.cfg = Config()
        self.cfg.base_dir = self.base
        self.cfg.notify_on_first_run = True

        self.inbox: dict = {}
        self.batches: list = []
        self.watcher = Watcher(
            self.cfg,
            dispatcher_factory=stub_dispatcher(self.inbox, batches=self.batches),
        )
        self.watcher.reload_inputs()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_post_matching_both_is_sent_once(self):
        scraper = StubScraper([make_post("1", "Oddam sobo in garsonjera", GROUP)])
        stats = self.watcher.check_group(scraper, GROUP)

        self.assertEqual(len(self.batches), 1)
        self.assertEqual(sorted(self.batches[0][1]), ["ana", "bo"])
        self.assertEqual(stats["sent"], 2)  # two people served by one message

    def test_a_post_matching_one_person_mentions_only_them(self):
        scraper = StubScraper([make_post("1", "Oddam sobo v Ljubljani", GROUP)])
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(self.batches, [(SHARED, ["bo"])])

    def test_a_post_matching_nobody_is_not_posted_at_all(self):
        scraper = StubScraper([make_post("1", "Prodam rabljeno kolo", GROUP)])
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(self.batches, [])

    def test_nobody_is_notified_twice_across_cycles(self):
        scraper = StubScraper([make_post("1", "Oddam sobo in garsonjera", GROUP)])
        self.watcher.check_group(scraper, GROUP)
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(len(self.batches), 1)

    def test_a_failed_batch_is_retried_for_everyone_in_it(self):
        self.watcher._dispatcher_factory = stub_dispatcher(
            self.inbox, batches=self.batches, fail=("ana",)
        )
        self.watcher.reload_inputs()
        scraper = StubScraper([make_post("1", "Oddam sobo in garsonjera", GROUP)])
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(self.batches, [])

        # Delivery recovers: the post comes back for both, not lost.
        self.watcher._dispatcher_factory = stub_dispatcher(self.inbox, batches=self.batches)
        self.watcher.reload_inputs()
        self.watcher.check_group(scraper, GROUP)
        self.assertEqual(sorted(self.batches[0][1]), ["ana", "bo"])


class TestBotDelivery(unittest.TestCase):
    """Posting as the bot, instead of creating a webhook per channel."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config()
        self.cfg.base_dir = Path(self.tmp.name)
        self.cfg.discord_bot_token = "bot-token"
        self.session = RecordingSession()
        self.post = make_post("42", "Oddam sobo v Ljubljani", GROUP)

    def tearDown(self):
        self.tmp.cleanup()

    def dispatch(self, subs):
        return Dispatcher(self.cfg, subs, session=self.session)

    def test_a_channel_id_needs_no_webhook(self):
        ana = Subscriber(name="ana", discord_channel_id="555", discord_user_id="111")
        self.assertTrue(ana.has_destination)
        delivered = self.dispatch([ana]).deliver(self.post, [(ana, match("soba"))])

        self.assertEqual(delivered, {"ana"})
        sent = self.session.posts[0]
        self.assertTrue(sent["url"].endswith("/channels/555/messages"))
        self.assertEqual(sent["headers"]["Authorization"], "Bot bot-token")

    def test_the_bot_posts_under_its_own_name(self):
        # username/avatar are webhook-only fields; Discord rejects them here.
        ana = Subscriber(name="ana", discord_channel_id="555")
        self.dispatch([ana]).deliver(self.post, [(ana, match("soba"))])
        body = self.session.posts[0]["body"]
        self.assertNotIn("username", body)
        self.assertNotIn("avatar_url", body)

    def test_the_embed_and_mentions_are_the_same_as_a_webhook(self):
        ana = Subscriber(name="ana", discord_channel_id="555", discord_user_id="111")
        bo = Subscriber(name="bo", discord_channel_id="555", discord_user_id="222")
        self.dispatch([ana, bo]).deliver(
            self.post, [(ana, match("soba")), (bo, match("oddam"))]
        )
        body = self.session.posts[0]["body"]
        self.assertIn("<@111>", body["content"])
        self.assertIn("<@222>", body["content"])
        self.assertEqual(body["allowed_mentions"], {"parse": [], "users": ["111", "222"]})

    def test_people_in_the_same_channel_share_one_message(self):
        ana = Subscriber(name="ana", discord_channel_id="555")
        bo = Subscriber(name="bo", discord_channel_id="555")
        self.dispatch([ana, bo]).deliver(
            self.post, [(ana, match("soba")), (bo, match("oddam"))]
        )
        self.assertEqual(len(self.session.posts), 1)

    def test_different_channels_stay_separate(self):
        ana = Subscriber(name="ana", discord_channel_id="555")
        bo = Subscriber(name="bo", discord_channel_id="666")
        self.dispatch([ana, bo]).deliver(
            self.post, [(ana, match("soba")), (bo, match("oddam"))]
        )
        self.assertEqual(len(self.session.posts), 2)

    def test_a_webhook_and_a_channel_are_not_merged(self):
        # Same physical channel, but we cannot know that - two sends is correct.
        ana = Subscriber(name="ana", discord_webhook_url=SHARED)
        bo = Subscriber(name="bo", discord_channel_id="555")
        self.dispatch([ana, bo]).deliver(
            self.post, [(ana, match("soba")), (bo, match("oddam"))]
        )
        self.assertEqual(len(self.session.posts), 2)

    def test_a_webhook_wins_when_both_are_set(self):
        # The webhook works without the bot being present, so prefer it.
        ana = Subscriber(name="ana", discord_webhook_url=SHARED, discord_channel_id="555")
        dispatcher = self.dispatch([ana])
        self.assertEqual(dispatcher.target_of(ana), f"webhook:{SHARED}")
        dispatcher.deliver(self.post, [(ana, match("soba"))])
        self.assertEqual(self.session.posts[0]["url"], SHARED)

    def test_a_channel_id_without_a_bot_token_is_no_destination(self):
        cfg = Config()
        cfg.base_dir = Path(self.tmp.name)
        ana = Subscriber(name="ana", discord_channel_id="555")
        dispatcher = Dispatcher(cfg, [ana], session=self.session)
        self.assertIsNone(dispatcher.target_of(ana))
        self.assertEqual(dispatcher.describe(ana), "nowhere")
        self.assertEqual(dispatcher.deliver(self.post, [(ana, match("soba"))]), set())

    def test_describe_says_which_transport(self):
        ana = Subscriber(name="ana", discord_channel_id="555")
        self.assertEqual(self.dispatch([ana]).describe(ana), "Discord (bot)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
