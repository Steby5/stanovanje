"""Text normalisation shared by the keyword matcher and the extractors.

Slovenian is heavily inflected and people type without diacritics, so every
comparison happens on a folded form: lowercase, no diacritics, collapsed
whitespace.  That way `stanovanj` matches "Stanovanje", "stanovanja" and
"STANOVANJU", and `garsonjera` matches "garsonjera" as well as "garsonjera".
"""

from __future__ import annotations

import re
import unicodedata

# Characters that NFKD does not decompose into base + combining mark.
_TRANSLIT = {
    "đ": "d",  # d with stroke
    "Đ": "D",
    "ð": "d",
    "Ð": "D",
    "ł": "l",  # l with stroke
    "Ł": "L",
    "ø": "o",
    "Ø": "O",
    "ß": "ss",
    "æ": "ae",
    "Æ": "AE",
    "œ": "oe",
    "Œ": "OE",
    "þ": "th",
    "Þ": "TH",
}

# Invisible / look-alike characters that show up when text is copied out of
# Facebook's DOM and would otherwise break a literal match.
_CLEANUP = {
    " ": " ",  # nbsp
    " ": " ",
    " ": " ",
    "​": "",  # zero width space
    "‌": "",
    "‍": "",
    "‎": "",  # LTR/RTL marks
    "‏": "",
    "﻿": "",
    "“": '"',
    "”": '"',
    "„": '"',
    "‘": "'",
    "’": "'",
    "–": "-",
    "—": "-",
    "−": "-",
}

_WS_RE = re.compile(r"\s+")


def clean(text: str | None) -> str:
    """Replace exotic whitespace/quotes and collapse runs of whitespace."""
    if not text:
        return ""
    for src, dst in _CLEANUP.items():
        if src in text:
            text = text.replace(src, dst)
    return _WS_RE.sub(" ", text).strip()


def strip_diacritics(text: str) -> str:
    for src, dst in _TRANSLIT.items():
        if src in text:
            text = text.replace(src, dst)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str | None) -> str:
    """Fold text for matching: lowercase, diacritic-free, single-spaced."""
    return strip_diacritics(clean(text)).lower()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """Cut `text` to `limit` characters, preferring a paragraph/word boundary."""
    if len(text) <= limit:
        return text
    hard = limit - len(suffix)
    window = text[:hard]
    for sep in ("\n\n", "\n", ". ", " "):
        idx = window.rfind(sep)
        if idx > hard * 0.6:
            window = window[:idx]
            break
    return window.rstrip() + suffix
