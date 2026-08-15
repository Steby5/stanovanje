"""Tests for the parts that run without a browser: matching, parsing, state."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fbwatch.matcher import KeywordMatcher, KeywordSyntaxError, stem  # noqa: E402
from fbwatch.models import make_post_id, parse_group_line  # noqa: E402
from fbwatch.store import SeenStore  # noqa: E402
from fbwatch.textutil import normalize, truncate  # noqa: E402


def matcher(*lines: str) -> KeywordMatcher:
    return KeywordMatcher.from_lines(list(lines))


class TestNormalize(unittest.TestCase):
    def test_folds_case_and_slovenian_diacritics(self):
        self.assertEqual(normalize("Želim ČIST Šport"), "zelim cist sport")

    def test_folds_characters_nfkd_leaves_alone(self):
        self.assertEqual(normalize("Đorđe"), "dorde")

    def test_collapses_whitespace_and_nbsp(self):
        self.assertEqual(normalize("a  \n b\t\tc"), "a b c")

    def test_handles_none_and_empty(self):
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize(""), "")


class TestSubstringRules(unittest.TestCase):
    def test_matches_inflected_forms(self):
        m = matcher("stanovanj")
        for text in ("Oddam stanovanje", "iščem STANOVANJA", "v stanovanju"):
            self.assertTrue(m.match(text).matched, text)

    def test_matches_without_diacritics_in_either_direction(self):
        self.assertTrue(matcher("garsonjera").match("GARSONJERA v centru").matched)
        self.assertTrue(matcher("iščem").match("iscem sobo").matched)
        self.assertTrue(matcher("iscem").match("Iščem sobo").matched)

    def test_reports_which_rule_matched(self):
        result = matcher("garsonjera", "soba").match("Oddam garsonjera blizu FRI")
        self.assertEqual(result.matched_rules, ["garsonjera"])


class TestStemming(unittest.TestCase):
    """Slovenian changes the ending, so plain terms match declined forms."""

    def test_long_terms_lose_the_inflectional_vowel(self):
        self.assertEqual(stem("ljubljana"), "ljubljan")
        self.assertEqual(stem("garsonjera"), "garsonjer")
        self.assertEqual(stem("stanovanje"), "stanovanj")

    def test_short_terms_are_not_stemmed_into_a_substring(self):
        # "sob" as a bare substring would hit "sobota"; it gets the anchored
        # vowel-ending treatment instead (see below).
        self.assertEqual(stem("soba"), "soba")
        self.assertEqual(stem("eur"), "eur")

    def test_short_words_match_their_case_endings_only(self):
        m = matcher("soba")
        for text in ("Oddam sobo", "prosta soba", "v sobi", "dve sobe", "5 sob"):
            self.assertTrue(m.match(text).matched, text)
        for text in ("danes je sobota", "gremo v soboto", "osoba"):
            self.assertFalse(m.match(text).matched, text)

    def test_double_vowel_endings_stay_literal(self):
        self.assertEqual(stem("avenue"), "avenue")

    def test_dictionary_form_matches_declined_text(self):
        self.assertTrue(matcher("ljubljana").match("Oddam sobo v Ljubljani").matched)
        self.assertTrue(matcher("garsonjera").match("Oddam garsonjero").matched)
        self.assertTrue(matcher("stanovanje").match("Lepa stanovanja na voljo").matched)

    def test_stemming_does_not_swallow_unrelated_words(self):
        self.assertFalse(matcher("soba").match("danes je sobota").matched)
        self.assertFalse(matcher("ljubljana").match("Oddam sobo v Mariboru").matched)

    def test_only_the_last_word_of_a_term_is_stemmed(self):
        self.assertTrue(matcher("mesto ljubljana").match("mesto ljubljani").matched)
        self.assertFalse(matcher("mesto ljubljana").match("mesta ljubljani").matched)


class TestAndRules(unittest.TestCase):
    def test_requires_every_part(self):
        m = matcher("oddam + ljubljana")
        self.assertTrue(m.match("Oddam lepo sobo v Ljubljani").matched)
        self.assertFalse(m.match("Oddam sobo v Mariboru").matched)
        self.assertFalse(m.match("Iščem stanovanje v Ljubljani").matched)

    def test_order_does_not_matter(self):
        self.assertTrue(matcher("a + b").match("something b then a").matched)

    def test_three_parts(self):
        m = matcher("oddam + soba + ljubljana")
        self.assertTrue(m.match("oddam soba, Ljubljana center").matched)
        self.assertFalse(m.match("oddam soba, Kranj").matched)


class TestPhraseRules(unittest.TestCase):
    def test_matches_exact_phrase_across_odd_whitespace(self):
        m = matcher('"oddam stanovanje"')
        self.assertTrue(m.match("Nujno:  oddam   stanovanje\nod julija").matched)

    def test_rejects_words_apart(self):
        self.assertFalse(matcher('"oddam stanovanje"').match("oddam lepo stanovanje").matched)


class TestWordRules(unittest.TestCase):
    def test_whole_word_only(self):
        m = matcher("=soba")
        self.assertTrue(m.match("Prosta soba od 1.9.").matched)
        self.assertTrue(m.match("(soba)").matched)
        self.assertFalse(m.match("posoda za pomivanje").matched)
        self.assertFalse(m.match("sobarica").matched)


class TestRegexRules(unittest.TestCase):
    def test_price_pattern(self):
        m = matcher(r"re:\d{3,4}\s?(eur|€)")
        self.assertTrue(m.match("Cena 450 EUR na mesec").matched)
        self.assertTrue(m.match("500€ + stroski").matched)
        self.assertFalse(m.match("Cena po dogovoru").matched)

    def test_invalid_regex_is_reported_with_line_number(self):
        with self.assertRaises(KeywordSyntaxError) as ctx:
            matcher("ok", "re:[unclosed")
        self.assertIn("line 2", str(ctx.exception))


class TestExclusions(unittest.TestCase):
    def test_exclusion_beats_a_match(self):
        m = matcher("stanovanj", "!agencija")
        self.assertTrue(m.match("Oddam stanovanje").matched)
        result = m.match("Oddam stanovanje preko agencija Nepremicnine")
        self.assertFalse(result.matched)
        self.assertEqual(result.excluded_by, "agencija")

    def test_exclusion_ignores_diacritics_too(self):
        m = matcher("soba", "!iščem")
        self.assertFalse(m.match("Iscem sobo v Ljubljani").matched)


class TestFileLevelBehaviour(unittest.TestCase):
    def test_comments_and_blank_lines_are_skipped(self):
        m = matcher("# a comment", "", "   ", "soba")
        self.assertEqual(len(m.includes), 1)

    def test_empty_file_matches_everything(self):
        m = matcher("# only comments")
        self.assertTrue(m.match_everything)
        self.assertTrue(m.match("literally anything").matched)

    def test_exclusions_still_apply_when_no_triggers(self):
        m = matcher("!spam")
        self.assertTrue(m.match("normal post").matched)
        self.assertFalse(m.match("this is spam").matched)

    def test_no_match_is_not_notified(self):
        self.assertFalse(matcher("stanovanj").match("Prodam kolo").matched)


class TestGroupParsing(unittest.TestCase):
    def test_full_url(self):
        g = parse_group_line("https://www.facebook.com/groups/123456789")
        self.assertEqual(g.slug, "123456789")
        self.assertEqual(g.url, "https://www.facebook.com/groups/123456789")
        self.assertIn("CHRONOLOGICAL", g.feed_url)

    def test_url_with_trailing_path_and_query(self):
        g = parse_group_line("https://www.facebook.com/groups/my-group/?ref=bookmarks")
        self.assertEqual(g.slug, "my-group")

    def test_bare_id_and_alias(self):
        g = parse_group_line("123456789 | Stanovanja LJ")
        self.assertEqual(g.slug, "123456789")
        self.assertEqual(g.name, "Stanovanja LJ")

    def test_name_defaults_to_slug(self):
        self.assertEqual(parse_group_line("mygroup").name, "mygroup")

    def test_comments_and_blanks(self):
        self.assertIsNone(parse_group_line("# comment"))
        self.assertIsNone(parse_group_line("   "))

    def test_trailing_comment_stripped(self):
        self.assertEqual(parse_group_line("mygroup   # watch this one").slug, "mygroup")

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_group_line("not a url at all!!")


class TestPostId(unittest.TestCase):
    def test_id_from_permalink(self):
        url = "https://www.facebook.com/groups/123/posts/999888777/?ref=x"
        self.assertEqual(make_post_id(url, "A", "text"), "999888777")

    def test_id_from_multi_permalink_query(self):
        url = "https://www.facebook.com/groups/123/?multi_permalink_id=555"
        self.assertEqual(make_post_id(url, "A", "text"), "555")

    def test_falls_back_to_content_hash_and_is_stable(self):
        a = make_post_id("", "Ana", "Oddam sobo")
        b = make_post_id("", "Ana", "Oddam  sobo")  # whitespace normalised
        self.assertTrue(a.startswith("fp_"))
        self.assertEqual(a, b)

    def test_different_posts_get_different_ids(self):
        self.assertNotEqual(
            make_post_id("", "Ana", "Oddam sobo"),
            make_post_id("", "Ana", "Oddam stanovanje"),
        )


class TestSeenStore(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_remembers_across_reload(self):
        store = SeenStore(self.path)
        self.assertFalse(store.knows_group("me", "g1"))
        store.add("me", "g1", "p1")
        store.save()

        reloaded = SeenStore(self.path)
        self.assertTrue(reloaded.has("me", "g1", "p1"))
        self.assertFalse(reloaded.has("me", "g1", "p2"))
        self.assertTrue(reloaded.knows_group("me", "g1"))

    def test_prune_drops_old_entries(self):
        import time

        store = SeenStore(self.path, retention_days=7)
        store.add("me", "g1", "old", when=time.time() - 40 * 86400)
        store.add("me", "g1", "new")
        self.assertEqual(store.prune(), 1)
        self.assertFalse(store.has("me", "g1", "old"))
        self.assertTrue(store.has("me", "g1", "new"))

    def test_corrupt_file_does_not_crash(self):
        self.path.write_text("{not json", encoding="utf-8")
        store = SeenStore(self.path)
        self.assertEqual(store.count(), 0)
        store.add("me", "g1", "p1")
        store.save()
        self.assertTrue(SeenStore(self.path).has("me", "g1", "p1"))


class TestTruncate(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(truncate("hello", 50), "hello")

    def test_long_text_fits_limit(self):
        text = "word " * 200
        out = truncate(text, 50)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.endswith("…"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
