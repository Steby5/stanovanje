"""Turning Facebook's post times into something you can compare.

Facebook writes the age two ways: relative for recent posts ("3 days ago",
"45 mins ago") and absolute once they get older ("July 22 at 3:41 PM").  Both
have to become a real time, because "is this listing still worth chasing" is
the question the whole tool exists to answer.

Deliberately hand-rolled rather than handed to `dateutil.parser` with
`fuzzy=True`, which is confidently wrong on every relative form - it reads the
leading number as a day or a year:

    "3 days ago"   -> the 3rd of this month
    "45 mins ago"  -> the year 2045
    "1 h"          -> 01:00 today

For an age filter that is the worst possible failure: it would silently drop
fresh listings and let stale ones through.  Anything unrecognised returns None,
and an unknown age is never treated as fresh.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .textutil import normalize

# Month names are matched directly rather than via strptime's %B, which depends
# on the process locale - the account's language decides what Facebook renders,
# and it need not match the machine's.
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    # Slovenian, in the nominative and the genitive Facebook uses after a day
    "januar": 1, "januarja": 1, "februar": 2, "februarja": 2,
    "marec": 3, "marca": 3, "april": 4, "aprila": 4, "maj": 5, "maja": 5,
    "junij": 6, "junija": 6, "julij": 7, "julija": 7, "avgust": 8, "avgusta": 8,
    "oktober": 10, "oktobra": 10, "november": 11, "novembra": 11,
    "december": 12, "decembra": 12,
}

# How long one unit is.  Months and years are approximate on purpose: nothing
# this old survives the age filter anyway, and being a day out does not matter.
_UNITS = {
    "s": "seconds", "sec": "seconds", "secs": "seconds", "second": "seconds",
    "seconds": "seconds",
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes",
    "minutes": "minutes", "minuto": "minutes", "minutami": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "u": "hours", "uro": "hours", "urami": "hours", "ura": "hours", "uri": "hours",
    "ure": "hours",
    "d": "days", "day": "days", "days": "days",
    "dan": "days", "dni": "days", "dnevi": "days", "dnem": "days",
    "w": "weeks", "wk": "weeks", "week": "weeks", "weeks": "weeks",
    "t": "weeks", "teden": "weeks", "tedni": "weeks", "tednom": "weeks",
    "mo": "months", "month": "months", "months": "months",
    "mesec": "months", "meseci": "months", "mesecem": "months",
    "y": "years", "yr": "years", "year": "years", "years": "years",
    "l": "years", "leto": "years", "leti": "years", "let": "years",
}

_APPROX_DAYS = {"months": 30, "years": 365}

# "3 days ago", "45 mins ago", "2h", "1 h", "pred 3 dnevi"
_RELATIVE_RE = re.compile(
    r"(?:^|\bpred\s+)(\d{1,3})\s*([a-z]{1,8})\b(?:\s+ago)?\s*$", re.I
)
# "July 22 at 3:41 PM", "December 31, 2025 at 11:59 PM", "August 3 at 09:15"
_ABSOLUTE_RE = re.compile(
    r"^([a-z]{3,12})\s+(\d{1,2})(?:,\s*(\d{4}))?"
    r"(?:\s+(?:at|ob)\s+|\s+)(\d{1,2}):(\d{2})\s*(am|pm)?\s*$",
    re.I,
)
# "22. julij ob 15:41" - Slovenian puts the day first
_ABSOLUTE_DAY_FIRST_RE = re.compile(
    r"^(\d{1,2})\.\s*([a-z]{3,12})(?:\s+(\d{4}))?"
    r"(?:\s+(?:ob|at)\s+|\s+)(\d{1,2}):(\d{2})\s*(am|pm)?\s*$",
    re.I,
)
# "Yesterday at 18:12"
_YESTERDAY_RE = re.compile(
    r"^(?:yesterday|vceraj)(?:\s+(?:at|ob)\s+(\d{1,2}):(\d{2}))?\s*$", re.I
)
_NOW_RE = re.compile(r"^(?:just now|now|pravkar|zdaj)\s*$", re.I)


def _with_time(base: datetime, hour: int, minute: int, ampm: str | None) -> datetime:
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time out of range")
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_posted_at(text: str, now: datetime | None = None) -> datetime | None:
    """When a post was made, or None when the text is not a time at all.

    Returning None matters: the same sprite mechanism that carries the time
    also carries things like "Learn More", and an unrecognised string must
    never be mistaken for a fresh post.
    """
    if not text:
        return None
    now = now or datetime.now()
    cleaned = normalize(text)  # lowercases, strips diacritics, tidies spaces

    if _NOW_RE.match(cleaned):
        return now

    match = _YESTERDAY_RE.match(cleaned)
    if match:
        base = now - timedelta(days=1)
        if match.group(1):
            try:
                return _with_time(base, int(match.group(1)), int(match.group(2)), None)
            except ValueError:
                return None
        return base

    match = _RELATIVE_RE.search(cleaned)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        name = _UNITS.get(unit)
        if name:
            if name in _APPROX_DAYS:
                return now - timedelta(days=amount * _APPROX_DAYS[name])
            return now - timedelta(**{name: amount})
        return None

    for pattern, day_first in ((_ABSOLUTE_RE, False), (_ABSOLUTE_DAY_FIRST_RE, True)):
        match = pattern.match(cleaned)
        if not match:
            continue
        first, second, year, hour, minute, ampm = match.groups()
        month_name, day = (second, first) if day_first else (first, second)
        month = _MONTHS.get(month_name.lower())
        if not month:
            return None
        try:
            base = datetime(int(year) if year else now.year, month, int(day))
            stamp = _with_time(base, int(hour), int(minute), ampm)
        except ValueError:
            return None
        # No year given and the date is ahead of us: it belongs to last year.
        if not year and stamp > now + timedelta(days=1):
            try:
                stamp = stamp.replace(year=stamp.year - 1)
            except ValueError:
                return None
        return stamp

    return None


def describe_age(posted_at: datetime | None, now: datetime | None = None) -> str:
    """A short human age, for the notification itself."""
    if posted_at is None:
        return ""
    now = now or datetime.now()
    seconds = (now - posted_at).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} min ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} h ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)} day{'s' if int(days) != 1 else ''} ago"
    weeks = days / 7
    if weeks < 5:
        return f"{int(weeks)} week{'s' if int(weeks) != 1 else ''} ago"
    # "%-d" is a glibc extension and raises on Windows, so strip the zero here.
    return f"{posted_at.day} {posted_at.strftime('%b')}"
