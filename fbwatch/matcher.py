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

    kind: str  # substring | phrase | word | regex | any
    raw: str
    needle: str = ""
    pattern: re.Pattern[str] | None = None
    # For an alias: any one of these satisfies the atom.  This is the only
    # place OR lives - a Rule is still a plain AND over its atoms.
    options: tuple = ()

    def matches(self, normalized_text: str) -> bool:
        if self.options:
            return any(option.matches(normalized_text) for option in self.options)
        if self.pattern is not None:
            return self.pattern.search(normalized_text) is not None
        return self.needle in normalized_text

    def hit(self, normalized_text: str) -> str | None:
        """Which term actually matched, for explaining a match back to the user."""
        if self.options:
            for option in self.options:
                found = option.hit(normalized_text)
                if found is not None:
                    return found
            return None
        return self.raw if self.matches(normalized_text) else None


@dataclass(frozen=True)
class Rule:
    """A whole line: all atoms must match (AND)."""

    raw: str
    atoms: tuple[Atom, ...]
    exclude: bool = False
    lineno: int = 0

    def matches(self, normalized_text: str) -> bool:
        return bool(self.atoms) and all(a.matches(normalized_text) for a in self.atoms)

    def explain(self, normalized_text: str) -> str:
        """The rule as written, with each alias showing the term that hit.

        `oddam + soba + @lj` becomes `oddam + soba + @lj→bezigrad`, so a reply
        says *why* a post matched rather than leaving the alias opaque.  Rules
        without aliases come back verbatim, so nothing downstream changes.
        """
        if not any(a.options for a in self.atoms):
            return self.raw
        parts = []
        for atom in self.atoms:
            found = atom.hit(normalized_text)
            parts.append(f"{atom.raw}→{found}" if atom.options and found else atom.raw)
        return " + ".join(parts)

    def missing(self, normalized_text: str) -> tuple:
        """The atoms that did not match - used to spot rules that are too tight."""
        return tuple(a for a in self.atoms if not a.matches(normalized_text))


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


ALIAS_NAME_RE = re.compile(r"^@[a-z0-9_-]{1,32}$", re.I)
_ALIAS_DEF_RE = re.compile(r"^(@[a-z0-9_-]{1,32})\s*=\s*(.+)$", re.I)


def parse_alias(line: str) -> tuple[str, str] | None:
    """Recognise `@name = a, b, c`.  Returns (name, the raw member list)."""
    match = _ALIAS_DEF_RE.match(line.strip())
    if not match:
        return None
    name, members = match.group(1).lower(), match.group(2).strip()
    if not members:
        raise KeywordSyntaxError(f"{name} is defined with nothing in it")
    return name, members


def _split_members(text: str) -> list[str]:
    """Split on commas that are not inside quotes."""
    members, current, quote = [], [], ""
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char == ",":
            members.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    members.append("".join(current).strip())
    return [m for m in members if m]


def build_alias(name: str, members: str) -> Atom:
    """Turn a definition into one atom that any member satisfies."""
    parts = _split_members(members)
    if not parts:
        raise KeywordSyntaxError(f"{name} is defined with nothing in it")
    options = []
    for part in parts:
        if part.startswith("@"):
            raise KeywordSyntaxError(
                f"{name} refers to another alias ({part}); list the terms directly"
            )
        options.append(_parse_atom(part))
    return Atom(kind="any", raw=name, options=tuple(options))


def _parse_atom(token: str, aliases: dict | None = None) -> Atom:
    token = token.strip()
    if not token:
        raise KeywordSyntaxError("empty term")

    if token.startswith("@"):
        if not ALIAS_NAME_RE.match(token):
            raise KeywordSyntaxError(f"{token!r} is not a usable alias name")
        alias = (aliases or {}).get(token.lower())
        if alias is None:
            raise KeywordSyntaxError(
                f"no alias called {token} - define it with "
                f"'{token.lower()} = ljubljana, bezigrad, vic'"
            )
        return alias

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


def parse_rule(line: str, lineno: int = 0, aliases: dict | None = None) -> Rule | None:
    """Parse one keywords.txt line.  Returns None for blanks, comments, aliases."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if _ALIAS_DEF_RE.match(line):
        return None  # a definition, not a rule; collected in the first pass

    exclude = False
    if line.startswith("!"):
        exclude = True
        line = line[1:].strip()
        if not line:
            raise KeywordSyntaxError("'!' with nothing to exclude")

    atoms = tuple(
        _parse_atom(tok, aliases) for tok in _SPLIT_AND_RE.split(line) if tok.strip()
    )
    if not atoms:
        raise KeywordSyntaxError(f"no usable terms in {line!r}")
    return Rule(raw=line, atoms=atoms, exclude=exclude, lineno=lineno)


class KeywordMatcher:
    """Holds the parsed rules and tests posts against them."""

    def __init__(self, rules: list[Rule], aliases: dict | None = None):
        self.includes = [r for r in rules if not r.exclude]
        self.excludes = [r for r in rules if r.exclude]
        self.aliases = aliases or {}

    @property
    def match_everything(self) -> bool:
        """True when no include rules exist - every post is interesting."""
        return not self.includes

    def __len__(self) -> int:
        return len(self.includes) + len(self.excludes)

    @classmethod
    def from_lines(cls, lines) -> "KeywordMatcher":
        """Two passes: collect the aliases, then parse the rules against them.

        Order in the file does not matter, so the alias block can sit at the
        bottom where it does not get in the way of the rules.
        """
        lines = list(lines)

        aliases: dict[str, Atom] = {}
        for lineno, raw in enumerate(lines, 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                found = parse_alias(stripped)
                if found is None:
                    continue
                name, members = found
                if name in aliases:
                    raise KeywordSyntaxError(f"{name} is defined more than once")
                aliases[name] = build_alias(name, members)
            except KeywordSyntaxError as exc:
                raise KeywordSyntaxError(f"line {lineno}: {exc}") from exc

        rules = []
        for lineno, raw in enumerate(lines, 1):
            try:
                rule = parse_rule(raw, lineno, aliases)
            except KeywordSyntaxError as exc:
                raise KeywordSyntaxError(f"line {lineno}: {exc}") from exc
            if rule:
                rules.append(rule)
        return cls(rules, aliases)

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

        hits = [
            rule.explain(normalized) for rule in self.includes if rule.matches(normalized)
        ]
        return MatchResult(matched=bool(hits), matched_rules=hits)

    def near_misses(self, text: str) -> list[tuple[Rule, Atom]]:
        """Rules that failed on exactly one term.

        An over-tight rule fails silently - you never learn what you missed.
        This is what makes that visible: `oddam + garsonjera + ljubljana` on a
        post saying "Bezigrad" comes back as (rule, the `ljubljana` atom).
        """
        normalized = normalize(text)
        for rule in self.excludes:
            if rule.matches(normalized):
                return []

        out = []
        for rule in self.includes:
            if len(rule.atoms) < 2 or rule.matches(normalized):
                continue
            missing = rule.missing(normalized)
            if len(missing) == 1:
                out.append((rule, missing[0]))
        return out
