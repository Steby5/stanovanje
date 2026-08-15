"""Trigger-word matching for `keywords.txt`.

Every line is one rule.  A post is notified when it matches at least one rule
and no exclusion rule.  An empty keywords file means "notify on everything".

    stanovanje              substring (matches stanovanja, stanovanju, ...)
    soba + ljubljana        AND: both parts must appear somewhere in the post
    "oddam garsonjero"      exact phrase
    =soba                   whole word only (won't match "posoda")
    re:\\d{3,4}\\s?(eur|€)   regular expression
    !agencija               exclusion: any post containing it is skipped

Matching is case-insensitive and diacritic-insensitive, so `zelim` matches
"Želim".  Plain terms also match declined forms - `ljubljana` matches
"v Ljubljani" and `garsonjera` matches "garsonjero" - by dropping a trailing
vowel to leave the stem.  Use `=word` or a `"quoted phrase"` when you need the
exact form instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .textutil import normalize

_SPLIT_AND_RE = re.compile(r"\s*\+\s*")

# Slovenian inflects the ending rather than appending to it: "Ljubljana" becomes
# "Ljubljani", "soba" becomes "sobo".  A plain substring search for the
# dictionary form would miss all of those, so a trailing vowel is dropped to
# leave the stem.
#
# How far the stem is then allowed to spread depends on how distinctive it is.
# A long stem is matched as a plain substring, which also picks up derived
# adjectives ("ljubljansko").  A short stem like "sob" is too ambiguous for
# that - it would hit "sobota" - so it is anchored between word boundaries and
# may only be followed by vowels, which still covers sobo/sobe/sobi/sob.
_VOWELS = "aeiou"
MIN_LENGTH_TO_STEM = 5
MIN_STEM_LENGTH = 4
MIN_SHORT_STEM_LENGTH = 3


def stem(term: str) -> str:
    """Drop one inflectional vowel, when a distinctive stem survives."""
    if len(term) >= MIN_LENGTH_TO_STEM and term[-1] in _VOWELS:
        candidate = term[:-1]
        if len(candidate) >= MIN_STEM_LENGTH and candidate[-1] not in _VOWELS:
            return candidate
    return term


def _substring_atom(token: str, needle: str) -> Atom:
    """Build a substring atom that tolerates Slovenian noun endings.

    Only the final word is relaxed; earlier words keep their exact form so
    "oddam sobo" does not quietly widen at both ends.
    """
    head, sep, tail = needle.rpartition(" ")
    stemmed = stem(tail)
    if stemmed != tail:
        return Atom(kind="substring", raw=token, needle=head + sep + stemmed)

    if (
        len(tail) == MIN_SHORT_STEM_LENGTH + 1
        and tail[-1] in _VOWELS
        and tail[-2] not in _VOWELS
    ):
        prefix = re.escape(head + sep + tail[:-1])
        pattern = re.compile(rf"(?<!\w){prefix}[{_VOWELS}]{{0,2}}(?!\w)")
        return Atom(kind="substring", raw=token, needle=needle, pattern=pattern)

    return Atom(kind="substring", raw=token, needle=needle)


@dataclass(frozen=True)
class Atom:
    """One condition inside a rule."""

    kind: str  # substring | phrase | word | regex
    raw: str
    needle: str = ""
    pattern: re.Pattern[str] | None = None

    def matches(self, normalized_text: str) -> bool:
        if self.pattern is not None:
            return self.pattern.search(normalized_text) is not None
        return self.needle in normalized_text


@dataclass(frozen=True)
class Rule:
    """A whole line: all atoms must match (AND)."""

    raw: str
    atoms: tuple[Atom, ...]
    exclude: bool = False
    lineno: int = 0

    def matches(self, normalized_text: str) -> bool:
        return bool(self.atoms) and all(a.matches(normalized_text) for a in self.atoms)


@dataclass
class MatchResult:
    matched: bool
    matched_rules: list[str] = field(default_factory=list)
    excluded_by: str | None = None

    @property
    def reason(self) -> str:
        if self.excluded_by:
            return f"excluded by !{self.excluded_by}"
        if not self.matched_rules:
            return "no keywords configured" if self.matched else "no keyword matched"
        return ", ".join(self.matched_rules)


class KeywordSyntaxError(ValueError):
    pass


def _parse_atom(token: str) -> Atom:
    token = token.strip()
    if not token:
        raise KeywordSyntaxError("empty term")

    if token.lower().startswith("re:"):
        expr = token[3:].strip()
        if not expr:
            raise KeywordSyntaxError("empty regular expression after 're:'")
        try:
            # The text is normalized before matching, so compile against the
            # folded form; IGNORECASE keeps hand-written [A-Z] classes working.
            pattern = re.compile(expr, re.IGNORECASE)
        except re.error as exc:
            raise KeywordSyntaxError(f"invalid regular expression {expr!r}: {exc}") from exc
        return Atom(kind="regex", raw=token, pattern=pattern)

    if token.startswith("=") and len(token) > 1:
        word = normalize(token[1:])
        if not word:
            raise KeywordSyntaxError(f"empty word in {token!r}")
        pattern = re.compile(rf"(?<!\w){re.escape(word)}(?!\w)")
        return Atom(kind="word", raw=token, needle=word, pattern=pattern)

    if len(token) > 1 and token[0] == token[-1] and token[0] in "\"'":
        phrase = normalize(token[1:-1])
        if not phrase:
            raise KeywordSyntaxError(f"empty phrase in {token!r}")
        return Atom(kind="phrase", raw=token, needle=phrase)

    needle = normalize(token)
    if not needle:
        raise KeywordSyntaxError(f"term normalises to nothing: {token!r}")
    return _substring_atom(token, needle)


def parse_rule(line: str, lineno: int = 0) -> Rule | None:
    """Parse one keywords.txt line.  Returns None for blanks and comments."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    exclude = False
    if line.startswith("!"):
        exclude = True
        line = line[1:].strip()
        if not line:
            raise KeywordSyntaxError("'!' with nothing to exclude")

    atoms = tuple(_parse_atom(tok) for tok in _SPLIT_AND_RE.split(line) if tok.strip())
    if not atoms:
        raise KeywordSyntaxError(f"no usable terms in {line!r}")
    return Rule(raw=line, atoms=atoms, exclude=exclude, lineno=lineno)


class KeywordMatcher:
    """Holds the parsed rules and tests posts against them."""

    def __init__(self, rules: list[Rule]):
        self.includes = [r for r in rules if not r.exclude]
        self.excludes = [r for r in rules if r.exclude]

    @property
    def match_everything(self) -> bool:
        """True when no include rules exist - every post is interesting."""
        return not self.includes

    def __len__(self) -> int:
        return len(self.includes) + len(self.excludes)

    @classmethod
    def from_lines(cls, lines) -> "KeywordMatcher":
        rules = []
        for lineno, raw in enumerate(lines, 1):
            try:
                rule = parse_rule(raw, lineno)
            except KeywordSyntaxError as exc:
                raise KeywordSyntaxError(f"line {lineno}: {exc}") from exc
            if rule:
                rules.append(rule)
        return cls(rules)

    @classmethod
    def from_file(cls, path: Path) -> "KeywordMatcher":
        if not path.exists():
            raise FileNotFoundError(f"keywords file not found: {path}")
        try:
            return cls.from_lines(path.read_text(encoding="utf-8").splitlines())
        except KeywordSyntaxError as exc:
            raise KeywordSyntaxError(f"{path}: {exc}") from exc

    def match(self, text: str) -> MatchResult:
        """Decide whether a post is worth notifying about.

        Exclusions win: a post containing an excluded term is dropped even if
        it also matches an include rule.
        """
        normalized = normalize(text)

        for rule in self.excludes:
            if rule.matches(normalized):
                return MatchResult(matched=False, excluded_by=rule.raw)

        if self.match_everything:
            return MatchResult(matched=True)

        hits = [rule.raw for rule in self.includes if rule.matches(normalized)]
        return MatchResult(matched=bool(hits), matched_rules=hits)
