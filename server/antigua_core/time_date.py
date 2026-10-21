"""Time & date skill — deterministic, LLM-free.

Same class as calc / weather: Python answers, the 4b model never sees it.
Everything is computed in the home timezone (settings.WEATHER_HOME_TZ), not
the server's system clock.

  * now          — "what time is it", "what's today", "what year is it",
                   "what week of the year is it", "time and date"
  * other days   — "what's the date tomorrow", "what day was yesterday",
                   "what's the date in 3 weeks", "what date is 90 days from now"
  * weekday of   — "what day is Christmas", "what day of the week is Nov 3rd"
  * when is      — "when is Thanksgiving", "when is Easter"
  * countdowns   — "how many days until Christmas / Friday / the weekend",
                   "how many weeks till New Year's", "days left in the year"
  * since        — "how many days since July 4th", "how long ago was Easter"
  * clock gap    — "how long until 5 pm", "how many hours until midnight"
  * yes / no     — "is it Friday", "is today the 15th", "is it the weekend"
  * holidays     — "is today a holiday", "is Monday a holiday"
  * world clock  — "what time is it in Tokyo" (geocoded, cached)
  * time zone    — "what time zone are we in"
  * clocks       — "when do the clocks change", "when do we fall back"

parse() is pure and network-free so classify() can route on it; answer() does
the one network-touching thing (geocoding a world-clock city). Sunrise and
sunset belong to the weather skill, birthdays to memory.
"""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import settings
from .intents.time_date import _DATE_QUERY_RE, _TIME_QUERY_RE
from .intents.timers import _replace_word_numbers, _resolve_clock_time

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(_MONTHS)} | {"sept": 9}
_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30,
}


def _ordinals_to_digits(text: str) -> str:
    """'twenty-third' -> '23rd', 'fourth' -> '4th' (before word numbers run)."""
    def compound(m):
        n = (20 if m.group(1).lower() == "twenty" else 30) + _ORDINAL_WORDS[m.group(2).lower()]
        return f"{n}th"
    text = re.sub(r"\b(twenty|thirty)[\s-]+(first|second|third|fourth|fifth|sixth|"
                  r"seventh|eighth|ninth)\b", compound, text, flags=re.I)
    return re.sub(r"\b(" + "|".join(_ORDINAL_WORDS) + r")\b",
                  lambda m: f"{_ORDINAL_WORDS[m.group(1).lower()]}th", text, flags=re.I)


# ── holidays ────────────────────────────────────────────────────────────────


def _nth_weekday(year, month, weekday, n):
    """n-th `weekday` (0=Mon) of the month; n=-1 for the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    """Western Easter Sunday (anonymous Gregorian computus)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month = (h + el - 7 * m + 114) // 31
    return date(year, month, (h + el - 7 * m + 114) % 31 + 1)


def _thanksgiving(y):
    return _nth_weekday(y, 11, 3, 4)


# (spoken name, regex alternatives, year -> date). The spoken name is used in
# replies, so it carries its own article where English wants one.
_HOLIDAYS = [
    ("New Year's Eve", r"new\s+year'?s\s+eve", lambda y: date(y, 12, 31)),
    ("New Year's Day", r"new\s+year'?s(?:\s+day)?|the\s+new\s+year|new\s+year",
     lambda y: date(y, 1, 1)),
    ("Martin Luther King Day", r"(?:martin\s+luther\s+king|mlk)(?:\s+jr\.?)?(?:'?s)?(?:\s+day)?",
     lambda y: _nth_weekday(y, 1, 0, 3)),
    ("Groundhog Day", r"groundhog'?s?\s+day", lambda y: date(y, 2, 2)),
    ("Valentine's Day", r"valentine'?s(?:\s+day)?", lambda y: date(y, 2, 14)),
    ("Presidents' Day", r"president'?s'?\s+day", lambda y: _nth_weekday(y, 2, 0, 3)),
    ("Mardi Gras", r"mardi\s+gras|fat\s+tuesday", lambda y: _easter(y) - timedelta(days=47)),
    ("Ash Wednesday", r"ash\s+wednesday", lambda y: _easter(y) - timedelta(days=46)),
    ("St. Patrick's Day", r"(?:saint|st\.?)\s+patrick'?s(?:\s+day)?|st\.?\s+patty'?s(?:\s+day)?",
     lambda y: date(y, 3, 17)),
    ("April Fools' Day", r"april\s+fool'?s'?(?:\s+day)?", lambda y: date(y, 4, 1)),
    ("Palm Sunday", r"palm\s+sunday", lambda y: _easter(y) - timedelta(days=7)),
    ("Good Friday", r"good\s+friday", lambda y: _easter(y) - timedelta(days=2)),
    ("Easter", r"easter(?:\s+sunday)?", _easter),
    ("Earth Day", r"earth\s+day", lambda y: date(y, 4, 22)),
    ("Cinco de Mayo", r"cinco\s+de\s+mayo", lambda y: date(y, 5, 5)),
    ("Mother's Day", r"mother'?s'?\s+day", lambda y: _nth_weekday(y, 5, 6, 2)),
    ("Memorial Day", r"memorial\s+day", lambda y: _nth_weekday(y, 5, 0, -1)),
    ("Father's Day", r"father'?s'?\s+day", lambda y: _nth_weekday(y, 6, 6, 3)),
    ("Juneteenth", r"juneteenth", lambda y: date(y, 6, 19)),
    ("the Fourth of July", r"(?:the\s+)?(?:fourth|4th)\s+of\s+july|independence\s+day|july\s+(?:fourth|4th)",
     lambda y: date(y, 7, 4)),
    ("Labor Day", r"labou?r\s+day", lambda y: _nth_weekday(y, 9, 0, 1)),
    ("Columbus Day", r"columbus\s+day|indigenous\s+peoples'?\s+day",
     lambda y: _nth_weekday(y, 10, 0, 2)),
    ("Halloween", r"hallowe'?en", lambda y: date(y, 10, 31)),
    ("Día de los Muertos", r"d[ií]a\s+de\s+(?:los\s+)?muertos|day\s+of\s+the\s+dead",
     lambda y: date(y, 11, 2)),
    ("Veterans Day", r"veteran'?s'?\s+day", lambda y: date(y, 11, 11)),
    ("Thanksgiving", r"thanksgiving(?:\s+day)?", _thanksgiving),
    ("Black Friday", r"black\s+friday", lambda y: _thanksgiving(y) + timedelta(days=1)),
    ("Christmas Eve", r"christmas\s+eve|xmas\s+eve", lambda y: date(y, 12, 24)),
    ("Christmas", r"christmas(?:\s+day)?|xmas", lambda y: date(y, 12, 25)),
]
_HOLIDAY_RES = [(name, re.compile(r"\b(?:" + pat + r")\b", re.I), fn)
                for name, pat, fn in _HOLIDAYS]


def holidays_on(d: date) -> list[str]:
    return [name for name, _, fn in _HOLIDAYS if fn(d.year) == d]


def _match_holiday(text):
    for name, rx, fn in _HOLIDAY_RES:
        if rx.search(text):
            return name, fn
    return None


# ── date phrases ────────────────────────────────────────────────────────────

_WD = "(" + "|".join(_WEEKDAYS) + ")"
_MON = r"(" + "|".join(_MONTHS) + "|" + "|".join(sorted(_MONTH_ABBR, key=len, reverse=True)) + r")\.?"
_DAYNUM = r"(\d{1,2})(?:st|nd|rd|th)?"
_MONTH_DAY_RE = re.compile(
    rf"\b(?:{_MON}\s+(?:the\s+)?{_DAYNUM}|(?:the\s+)?{_DAYNUM}\s+of\s+{_MON})(?:,?\s+(\d{{4}}))?\b", re.I)
_BARE_ORDINAL_RE = re.compile(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b", re.I)
_REL_WD_RE = re.compile(rf"\b(this|next|last|this\s+coming|this\s+past)?\s*{_WD}\b", re.I)
_OFFSET_RE = re.compile(
    r"\b(?:in\s+)?(an?|\d+)\s+(day|week|month|year)s?\b(?:\s+(from\s+(?:now|today)|ago|before|after))?",
    re.I)


def _month_num(tok):
    tok = tok.lower().rstrip(".")
    return _MONTHS.index(tok) + 1 if tok in _MONTHS else _MONTH_ABBR.get(tok[:4] if tok.startswith("sept") else tok[:3])


def _add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue


def _upcoming(fn, today, *, allow_today=True):
    """Next occurrence of a yearly date (this year, or next if it's passed)."""
    d = fn(today.year)
    if d < today or (d == today and not allow_today):
        d = fn(today.year + 1)
    return d


def _recent(fn, today):
    d = fn(today.year)
    return d if d <= today else fn(today.year - 1)


def parse_date_phrase(text, today, *, past=False):
    """Resolve the day `text` names. Returns (date, label, explicit_year) or None.

    `past` picks the previous occurrence for yearly dates and bare weekdays
    ("how many days since Christmas"). label is how the reply names the day
    ("Christmas", "Friday", "tomorrow") or None to use the date itself.
    """
    t = text.lower()
    if re.search(r"\bday\s+after\s+tomorrow\b", t):
        return today + timedelta(days=2), "the day after tomorrow", False
    if re.search(r"\bday\s+before\s+yesterday\b", t):
        return today - timedelta(days=2), "the day before yesterday", False
    for word, off in (("tomorrow", 1), ("yesterday", -1), ("today", 0), ("tonight", 0)):
        if re.search(rf"\b{word}'?s?\b", t) and not _OFFSET_RE.search(t):
            return today + timedelta(days=off), word, False

    m = _OFFSET_RE.search(t)
    if m and (m.group(3) or re.search(r"\bin\s+(?:an?|\d+)\s+(?:day|week|month|year)", t)):
        n = 1 if m.group(1) in ("a", "an") else int(m.group(1))
        unit, direction = m.group(2), (m.group(3) or "from now").lower()
        sign = -1 if direction in ("ago", "before") else 1
        if unit == "day":
            d = today + timedelta(days=sign * n)
        elif unit == "week":
            d = today + timedelta(weeks=sign * n)
        elif unit == "month":
            d = _add_months(today, sign * n)
        else:
            d = _add_months(today, sign * 12 * n)
        span = f"{m.group(1)} {unit}" + ("" if m.group(1) in ("a", "an", "1") else "s")
        tail = "ago" if sign < 0 else ("from today" if "today" in direction else "from now")
        return d, f"{span} {tail}", d.year != today.year

    hol = _match_holiday(t)
    if hol:
        name, fn = hol
        y = re.search(r"\b(?:in\s+)?(20\d\d)\b", t)
        if y:
            return fn(int(y.group(1))), name, True
        if re.search(r"\blast\s+year\b", t):
            return fn(today.year - 1), name, True
        if re.search(r"\bnext\s+year\b", t):
            return fn(today.year + 1), name, True
        if re.search(r"\bthis\s+year\b", t):
            return fn(today.year), name, False
        return (_recent(fn, today) if past else _upcoming(fn, today)), name, False

    m = _MONTH_DAY_RE.search(t)
    if m:
        mon = _month_num(m.group(1) or m.group(4))
        day = int(m.group(2) or m.group(3))
        try:
            if m.group(5):
                return date(int(m.group(5)), mon, day), None, True
            fn = lambda y: date(y, mon, day)   # noqa: E731
            return (_recent(fn, today) if past else _upcoming(fn, today)), None, False
        except ValueError:
            return None
    m = _BARE_ORDINAL_RE.search(t)
    if m and 1 <= int(m.group(1)) <= 31:
        day = int(m.group(1))
        d = today
        for _ in range(3):              # this month, else the next that has the day
            try:
                cand = d.replace(day=day)
                if (cand >= today) != past or cand == today:
                    return cand, None, False
            except ValueError:
                pass
            d = _add_months(d.replace(day=1), -1 if past else 1)
        return None

    if re.search(r"\bend\s+of\s+the\s+year\b|\bnew\s+year'?s\s+eve\b", t):
        return date(today.year, 12, 31), "the end of the year", False
    if re.search(r"\bend\s+of\s+(?:the|this)\s+month\b", t):
        return _add_months(today.replace(day=1), 1) - timedelta(days=1), "the end of the month", False

    m = _REL_WD_RE.search(t)
    if m:
        mod = (m.group(1) or "").lower().split()[0] if m.group(1) else ""
        if " past" in (m.group(1) or "").lower():
            mod = "last"
        wd = _WEEKDAYS.index(m.group(2).lower())
        if mod == "last" or (past and not mod):
            off = -((today.weekday() - wd) % 7 or 7)
        else:
            off = (wd - today.weekday()) % 7
            if mod == "next" and off == 0:
                off = 7
        return today + timedelta(days=off), m.group(2).capitalize() if off else "today", False
    return None


# ── spoken formatting ───────────────────────────────────────────────────────


def _clock(dt):
    if dt.hour == 12 and dt.minute == 0:
        return "noon"
    if dt.hour == 0 and dt.minute == 0:
        return "midnight"
    return dt.strftime("%-I:%M %p")


def _clock_round(dt):
    """'5 PM' rather than '5:00 PM' for an on-the-hour target."""
    return _clock(dt) if dt.minute or dt.hour in (0, 12) else dt.strftime("%-I %p")


def _date(d, today, *, weekday=True, force_year=False):
    s = d.strftime("%A, %B %-d" if weekday else "%B %-d")
    if force_year or d.year != today.year:
        s += f", {d.year}"
    return s


def _plural(n, unit):
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def _span_days(days, *, weeks=False):
    if weeks and days >= 7:
        w, r = divmod(days, 7)
        return _plural(w, "week") + (f" and {_plural(r, 'day')}" if r else "")
    return _plural(days, "day")


def _span_minutes(mins):
    h, m = divmod(mins, 60)
    if h and m:
        return f"{_plural(h, 'hour')} and {_plural(m, 'minute')}"
    return _plural(h, "hour") if h else _plural(m, "minute")


def _cap(s):
    return s[:1].upper() + s[1:]


def _tz_spoken(tz, at):
    abbr = at.astimezone(ZoneInfo(tz)).tzname() or tz
    names = {"CDT": "Central Daylight Time", "CST": "Central Standard Time",
             "EDT": "Eastern Daylight Time", "EST": "Eastern Standard Time",
             "MDT": "Mountain Daylight Time", "MST": "Mountain Standard Time",
             "PDT": "Pacific Daylight Time", "PST": "Pacific Standard Time"}
    return names.get(abbr, tz.split("/")[-1].replace("_", " ") + " time")


def next_clock_change(tz, now):
    """(local datetime just after the change, delta hours) or None if no DST."""
    zi = ZoneInfo(tz)
    cur = now.astimezone(zi)
    off = cur.utcoffset()
    for h in range(0, 24 * 400, 1):
        t = (cur + timedelta(hours=h)).astimezone(zi)
        if t.utcoffset() != off:
            # Walk back to the exact hour boundary in UTC.
            return t, (t.utcoffset() - off).total_seconds() / 3600
    return None


# ── parse ───────────────────────────────────────────────────────────────────

_FILLER_RE = re.compile(
    r"^(?:(?:hey|ok|okay|so|um|uh)\s+)*(?:antigua[,\s]+)?(?:(?:please|can\s+you|could\s+you|"
    r"would\s+you|do\s+you\s+know|tell\s+me|i\s+wonder|any\s+idea)\s+)*", re.I)

_NOW_TIME_RE = re.compile(
    r"^(?:what(?:'s|\s+is)\s+the\s+time|what\s+time(?:'s|\s+is)\s+it|the\s+time|"
    r"time(?:\s+(?:please|check|now|right\s+now))?|got\s+the\s+time|"
    r"(?:do\s+you\s+)?have\s+the\s+time|what\s+time\s+(?:do\s+you\s+have|have\s+you\s+got)|"
    r"what\s+time\s+it\s+is|how\s+late\s+is\s+it)"
    r"(?:\s+(?:right\s+)?now)?\??$", re.I)
_NOW_DATE_RE = re.compile(
    r"^(?:what(?:'s|\s+is)\s+today|what\s+day\s+it\s+is|the\s+date|today'?s\s+date|"
    r"what(?:'s|\s+is)\s+the\s+date|what\s+date\s+it\s+is|what\s+day\s+is\s+(?:it|today))"
    r"(?:\s+today)?\??$", re.I)
_YEAR_RE = re.compile(r"\bwhat\s+year\s+is\s+(?:it|this)\b|\bwhat(?:'s|\s+is)\s+the\s+year\b", re.I)
_MONTH_NOW_RE = re.compile(r"\bwhat\s+month\s+is\s+(?:it|this)\b|\bwhat(?:'s|\s+is)\s+the\s+month\b", re.I)
_WEEKNUM_RE = re.compile(r"\b(?:what(?:'s|\s+is)\s+the\s+week\s+number|what\s+week\s+(?:of\s+the\s+year\s+)?is\s+(?:it|this))\b", re.I)
_DAY_OF_YEAR_RE = re.compile(r"\bwhat\s+day\s+of\s+the\s+year\b", re.I)
_DAYS_LEFT_RE = re.compile(
    r"\bhow\s+many\s+(days|weeks)\s+(?:are\s+)?(?:left|remaining)\s+(?:in|this)\s+(?:the\s+)?(year|month)\b", re.I)

_WORLD_RE = re.compile(
    r"\b(?:what(?:'s|\s+is)\s+the\s+time|what\s+time\s+is\s+it|what\s+time\s+it\s+is|"
    r"the\s+time|what\s+day\s+is\s+it|what(?:'s|\s+is)\s+the\s+date)\s+(?:right\s+now\s+)?(?:in|over\s+in)\s+"
    r"(.+?)(?:\s+right\s+now|\s+now)?\??$", re.I)
# "the date in 2 weeks", "the time in the kitchen" — not a world-clock city.
_NOT_A_PLACE_RE = re.compile(
    r"^(?:(?:an?|\d+)\s+(?:day|week|month|year)s?\b|here$|"
    r"(?:the|this|my|our)\s+(?:house|home|kitchen|living\s*room|bedroom|office|garage|"
    r"morning|afternoon|evening|night|day)\b)", re.I)
_TZ_HOME_RE =re.compile(r"\bwhat\s+time\s*zone\s+(?:are\s+we|am\s+i|is\s+this|is\s+it)\b|"
                         r"\bwhat(?:'s|\s+is)\s+(?:our|my|the)\s+time\s*zone\b", re.I)
_DST_RE = re.compile(
    r"\b(?:daylight\s+sav(?:ings?|e)(?:\s+time)?|(?:the\s+)?clocks?\s+(?:change|go\s+(?:back|forward))|"
    r"time\s+change|change\s+the\s+clocks?|fall\s+back|spring\s+forward|"
    r"(?:do|does)\s+(?:the\s+)?time\s+change)\b", re.I)

_UNTIL_RE = re.compile(
    r"\bhow\s+(?:many\s+(days|weeks|sleeps|hours|minutes)|long|much\s+longer|much\s+time)\s+"
    r"(?:is\s+it\s+|are\s+there\s+|do\s+we\s+have\s+|left\s+|have\s+we\s+got\s+)?"
    r"(?:un)?til+\s+(.+?)\??$|"
    r"\bhow\s+(?:many\s+(days|weeks)|long)\s+(?:is\s+it\s+)?(?:away\s+is|before|to)\s+(.+?)(?:\s+is\s+here|\s+gets\s+here)?\??$|"
    r"\bhow\s+(?:many\s+(days|weeks)|long)\s+(?:away\s+)?is\s+(.+?)\s+away\??$|"
    r"\bhow\s+(?:far\s+away|far\s+off|soon)\s+is\s+(.+?)\??$",
    re.I)
_SINCE_RE = re.compile(
    r"\bhow\s+(?:many\s+(days|weeks)|long)\s+(?:has\s+it\s+been\s+|is\s+it\s+|)since\s+(.+?)\??$|"
    r"\bhow\s+(?:many\s+(days|weeks)|long)\s+ago\s+(?:was|is)\s+(.+?)\??$",
    re.I)
_WHEN_IS_RE = re.compile(
    r"^(?:when(?:'s|\s+is|\s+was|\s+will\s+be)|what\s+(?:day|date)\s+is|what(?:'s|\s+is)\s+the\s+date\s+(?:of|for))\s+"
    r"(.+?)(?:\s+(?:this|next|last)\s+year|\s+in\s+20\d\d)?(?:\s+on)?(?:\s+this\s+year)?\??$", re.I)
_DOW_OF_RE = re.compile(
    r"\bwhat\s+day(?:\s+of\s+the\s+week)?\s+(?:is|was|will\s+be|does|did|falls?)\s+(.+?)"
    r"(?:\s+(?:fall|land|be))?(?:\s+on)?\??$", re.I)
_DATE_OF_RE = re.compile(
    r"\bwhat(?:'s|\s+is|\s+was|\s+will\s+be)?\s+(?:the\s+)?(?:date|day)\s+"
    r"(?:(?:is\s+it\s+|will\s+it\s+be\s+|was\s+it\s+)?(.+?)|(?:is|was)\s+(.+?))\??$|"
    r"\bwhat\s+(?:date|day)\s+(?:is|was|will\s+it\s+be|will\s+be)\s+(.+?)\??$", re.I)
_YESNO_RE = re.compile(
    r"^(?:is|isn't|is\s+not)\s+(it|today|tomorrow|yesterday)\s+(?:actually\s+|really\s+)?(.+?)(?:\s+today)?\??$", re.I)
_HOLIDAY_Q_RE = re.compile(
    r"\b(?:is\s+(?:it\s+|there\s+)?(?:a\s+)?holiday|is\s+(.+?)\s+a\s+holiday|what\s+holiday\s+is|"
    r"any\s+holidays?\s+(?:today|tomorrow)|what(?:'s|\s+is)\s+the\s+(?:next|upcoming)\s+holiday)\b(.*)$", re.I)
_CLOCK_TARGET_RE = re.compile(
    r"^(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|o'?clock)?|noon|midnight|"
    r"\d{1,2}(?::\d{2})?\s*(?:tonight|this\s+(?:morning|afternoon|evening)|in\s+the\s+(?:morning|afternoon|evening)))$",
    re.I)


def _norm(text):
    t = text.strip().rstrip(".!?").strip()
    t = _FILLER_RE.sub("", t).strip()
    t = re.sub(r"[,\s]+(?:please|for\s+me|antigua|real\s+quick)$", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t)
    t = _ordinals_to_digits(t)
    return _replace_word_numbers(t)


def parse(text, now=None):
    """Classify a time/date question. Returns (kind, payload) or None.

    Pure and network-free. now is a home-tz-aware datetime (defaults to now).
    """
    if not text:
        return None
    now = now or _now()
    today = now.date()
    t = _norm(text)
    low = t.lower()

    # Anything about timers / alarms / reminders / the weather / the sun is
    # someone else's: "how long until my alarm", "what day is trash pickup".
    if re.search(r"\b(?:timer|alarm|reminder|weather|forecast|rain|snow|sun\s*(?:set|rise)|"
                 r"dark|light\s+out|game|match|show|episode|appointment|my\s+birthday|"
                 r"pickup|trash|garbage|recycling|open|close|delivery|flight|bus|train)s?\b", low):
        return None

    m = _WORLD_RE.search(t)
    if m:
        place = m.group(1).strip()
        if not _NOT_A_PLACE_RE.match(place):
            return "world", re.sub(r"^the\s+", "", place, flags=re.I)
    if _TZ_HOME_RE.search(low):
        return "tz_home", None
    if _DST_RE.search(low):
        return "dst", None

    if _NOW_TIME_RE.match(t):
        return "now_time", None
    if _NOW_DATE_RE.match(t):
        return "now_date", None
    if re.search(r"\b(?:time\s+and\s+(?:the\s+)?(?:date|day)|(?:date|day)\s+and\s+(?:the\s+)?time)\b", low):
        return "now_both", None
    if _YEAR_RE.search(low):
        return "now_year", None
    if _MONTH_NOW_RE.search(low):
        return "now_month", None
    if _WEEKNUM_RE.search(low):
        return "now_week", None
    if _DAY_OF_YEAR_RE.search(low):
        return "day_of_year", None
    m = _DAYS_LEFT_RE.search(low)
    if m:
        return "left_in", (m.group(2), m.group(1) == "weeks")

    m = _HOLIDAY_Q_RE.search(low)
    if m:
        if re.search(r"\b(?:next|upcoming)\s+holiday\b", low):
            return "next_holiday", None
        tail = (m.group(1) or "") + " " + (m.group(2) or "")
        pd = parse_date_phrase(tail, today) if tail.strip() else None
        return "holiday_on", (pd[0], pd[1]) if pd else (today, "today")

    m = _SINCE_RE.search(t)
    if m:
        target = (m.group(2) or m.group(4)).strip()
        weeks = (m.group(1) or m.group(3) or "").lower() == "weeks"
        pd = parse_date_phrase(target, today, past=True)
        if pd and pd[0] <= today:
            return "since", (pd[0], pd[1], weeks)
        return None

    m = _UNTIL_RE.search(t)
    if m:
        unit = (m.group(1) or m.group(3) or m.group(5) or "").lower()
        target = (m.group(2) or m.group(4) or m.group(6) or m.group(7)).strip()
        if unit in ("hours", "minutes") or _CLOCK_TARGET_RE.match(target):
            if _CLOCK_TARGET_RE.match(target):
                return "until_clock", target
            return None
        if re.fullmatch(r"(?:the\s+)?weekend", target, re.I):
            return "until_weekend", None
        pd = parse_date_phrase(target, today)
        if pd and (pd[1] or pd[0] != today or re.search(r"\d", target)):
            return "until", (pd[0], pd[1], unit == "weeks", unit == "sleeps")
        return None

    m = _YESNO_RE.match(t)
    if m:
        subj, pred = m.group(1).lower(), m.group(2).strip()
        day = today + timedelta(days={"it": 0, "today": 0, "tomorrow": 1, "yesterday": -1}[subj])
        plow = pred.lower()
        if re.fullmatch(r"(?:the\s+)?weekend(?:\s+yet)?", plow):
            return "is_weekend", (day, subj)
        if re.fullmatch(rf"(?:a\s+)?{_WD}", plow):
            return "is_weekday", (day, subj, _WEEKDAYS.index(plow.split()[-1]))
        if plow.rstrip(".") in _MONTHS:
            return "is_month", (day, subj, _MONTHS.index(plow) + 1)
        if _match_holiday(plow) or _MONTH_DAY_RE.fullmatch(plow) or _BARE_ORDINAL_RE.fullmatch(plow) \
                or re.fullmatch(r"the\s+\d{1,2}(?:st|nd|rd|th)?", plow):
            target = parse_date_phrase(plow if not re.fullmatch(r"the\s+\d{1,2}", plow) else plow + "th", day)
            if target:
                return "is_date", (day, subj, target[0], target[1])
        return None

    m = _DOW_OF_RE.search(t)
    if m:
        target = m.group(1).strip()
        if re.fullmatch(r"(?:it|today|it\s+today)", target, re.I):
            return "now_date", None
        pd = parse_date_phrase(target, today)
        if pd:
            return "date_of", (pd[0], pd[1], pd[2])
    m = _WHEN_IS_RE.match(t)
    if m:
        hol = _match_holiday(t)
        if hol and _match_holiday(m.group(1)):
            pd = parse_date_phrase(t, today)
            return "when_is", (pd[0], pd[1], pd[2])
        if m.group(0).lower().startswith("what"):
            pd = parse_date_phrase(m.group(1), today)
            if pd and pd[1] not in ("today",):
                return "date_of", (pd[0], pd[1], pd[2])
    m = _DATE_OF_RE.search(t)
    if m:
        target = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if re.fullmatch(r"(?:it|today|it\s+today|it\s+now|of\s+the\s+week|of\s+the\s+week\s+is\s+it)?", target, re.I):
            return "now_date", None
        pd = parse_date_phrase(target, today)
        if pd and pd[1] != "today":
            return "date_of", (pd[0], pd[1], pd[2])
        if pd:
            return "now_date", None
    m = re.match(r"^(?:what(?:'s|\s+is|\s+was)|what\s+day\s+is)\s+(tomorrow|yesterday)(?:'?s\s+date)?\??$", low)
    if m:
        pd = parse_date_phrase(m.group(1), today)
        return "date_of", (pd[0], pd[1], False)

    # The original catch-alls: still claim what they always did.
    is_time = bool(_TIME_QUERY_RE.search(t))
    is_date = bool(_DATE_QUERY_RE.search(t))
    if is_time and is_date:
        return "now_both", None
    if is_time:
        return "now_time", None
    if is_date:
        return "now_date", None
    return None


# ── answer ──────────────────────────────────────────────────────────────────


def _now():
    return datetime.now(ZoneInfo(settings.WEATHER_HOME_TZ))


_SUBJ = {"it": "it's", "today": "today is", "tomorrow": "tomorrow is", "yesterday": "yesterday was"}


def _is_label(label):
    """True for a name worth saying (Christmas, Friday) rather than a relative
    day (tomorrow, 3 weeks from now) that's better said as the date."""
    return bool(label) and not label.endswith(("from now", "from today", "ago")) \
        and label not in ("today", "tomorrow", "yesterday", "tonight",
                          "the day after tomorrow", "the day before yesterday")


def answer(text, *, now=None, geocode=None):
    """Spoken reply for a time/date question, or None if it isn't one."""
    now = now or _now()
    today = now.date()
    p = parse(text, now)
    if not p:
        return None
    kind, arg = p

    if kind == "now_time":
        return f"It's {_clock(now)}."
    if kind == "now_date":
        return f"Today is {_date(today, today)}."
    if kind == "now_both":
        return f"It's {_clock(now)} on {_date(today, today)}."
    if kind == "now_year":
        return f"It's {today.year}."
    if kind == "now_month":
        return f"It's {today:%B}."
    if kind == "now_week":
        return f"It's week {today.isocalendar()[1]} of {today.year}."
    if kind == "day_of_year":
        total = 366 if date(today.year, 12, 31).timetuple().tm_yday == 366 else 365
        return f"Today is day {today.timetuple().tm_yday} of {total}."
    if kind == "left_in":
        span, weeks = arg
        end = date(today.year, 12, 31) if span == "year" else \
            _add_months(today.replace(day=1), 1) - timedelta(days=1)
        n = (end - today).days
        if n == 0:
            return f"Today is the last day of the {span}."
        return f"There {'is' if n == 1 else 'are'} {_span_days(n, weeks=weeks)} left in the {span}, not counting today."

    if kind == "date_of":
        d, label, explicit = arg
        verb = "was" if d < today else "is"
        if label and not _is_label(label):          # tomorrow, 3 weeks from now
            return f"{_cap(label)} {verb} {_date(d, today)}."
        if label and label.lower() in _WEEKDAYS:
            return f"{'Last ' if d < today else ''}{label} {verb} {_date(d, today, weekday=False)}."
        if label:
            return _holiday_sentence(label, d, today, explicit)
        return f"{_cap(_date(d, today, weekday=False, force_year=explicit))} {verb} a {d:%A}."

    if kind == "when_is":
        d, label, explicit = arg
        return _holiday_sentence(label, d, today, explicit)

    if kind == "until":
        d, label, weeks, sleeps = arg
        n = (d - today).days
        name = label if _is_label(label) else _date(d, today, weekday=False)
        if n < 0:
            return f"{_cap(name)} was {_span_days(-n, weeks=weeks)} ago."
        if n == 0:
            return f"{_cap(name)} is today!" if _is_label(label) else "That's today."
        if n == 1:
            return f"{_cap(name)} is tomorrow." if label != "tomorrow" else "Tomorrow is one day away."
        if sleeps:
            return f"{_plural(n, 'more sleep')} until {name}."
        tail = "" if not _is_label(label) else f", on {_date(d, today)}"
        if label and label in [w.capitalize() for w in _WEEKDAYS]:
            tail = ""
        return f"{_cap(name)} is {_span_days(n, weeks=weeks)} away{tail}."

    if kind == "until_weekend":
        wd = today.weekday()
        if wd >= 5:
            return "It's the weekend now!"
        n = 5 - wd
        return "The weekend starts tomorrow." if n == 1 else f"The weekend starts in {_plural(n, 'day')}, on Saturday."

    if kind == "since":
        d, label, weeks = arg
        n = (today - d).days
        name = label if _is_label(label) else _date(d, today, weekday=False)
        if n == 0:
            return f"{_cap(name)} is today."
        return f"{_cap(name)} was {_span_days(n, weeks=weeks)} ago."

    if kind == "until_clock":
        naive = now.replace(tzinfo=None)
        hit = _resolve_clock_time(arg, naive, allow_daypart=True)
        if not hit:
            return None
        target = hit[0]
        # "until 5" with no am/pm means whichever 5 comes first, not the alarm
        # parser's wake-up-hours guess.
        if not re.search(r"\b(?:[ap]\.?m|morning|afternoon|evening|tonight|noon|midnight)\b", arg, re.I):
            sooner = target - timedelta(hours=12)
            if sooner > naive:
                target = sooner
        mins = max(1, round((target - naive).total_seconds() / 60))
        return f"{_cap(_span_minutes(mins))} until {_clock_round(target)}."

    if kind == "is_weekday":
        day, subj, wd = arg
        return f"{'Yes' if day.weekday() == wd else 'No'}, {_SUBJ[subj]} {day:%A}."
    if kind == "is_weekend":
        day, subj = arg
        if day.weekday() >= 5:
            return f"Yes, {_SUBJ[subj]} {day:%A}."
        if subj in ("it", "today"):
            n = 5 - day.weekday()
            starts = "tomorrow" if n == 1 else f"in {_plural(n, 'day')}"
            return f"Not yet, it's {day:%A}. The weekend starts {starts}."
        return f"No, {_SUBJ[subj]} {day:%A}."
    if kind == "is_month":
        day, subj, mon = arg
        return f"{'Yes' if day.month == mon else 'No'}, it's {day:%B}."
    if kind == "is_date":
        day, subj, target, label = arg
        if day == target:
            return f"Yes, {_SUBJ[subj]} {label}, {_date(day, today)}." if _is_label(label) \
                else f"Yes, {_SUBJ[subj]} {_date(day, today)}."
        n = (target - today).days
        if _is_label(label) and n > 0:
            return f"No, {label} is {_span_days(n)} away, on {_date(target, today)}."
        return f"No, {_SUBJ[subj]} {_date(day, today)}."

    if kind == "holiday_on":
        d, label = arg
        names = holidays_on(d)
        when = label if label in ("today", "tomorrow", "yesterday") else _date(d, today)
        verb = "was" if d < today else "is"
        if names:
            return f"Yes, {when} {verb} {' and '.join(names)}."
        nxt_d, nxt = _next_holiday(today if d <= today else d)
        return (f"No holiday {when}. The next one is {nxt}, "
                f"{'in ' + _span_days((nxt_d - today).days) if nxt_d > today else 'today'}.") \
            if label in ("today", "tomorrow") else f"No, {when} {verb} not a holiday."
    if kind == "next_holiday":
        d, name = _next_holiday(today + timedelta(days=1))
        return f"The next holiday is {name}, on {_date(d, today)}. That's {_span_days((d - today).days)} away."

    if kind == "tz_home":
        return f"We're on {_tz_spoken(settings.WEATHER_HOME_TZ, now)}."

    if kind == "dst":
        hit = next_clock_change(settings.WEATHER_HOME_TZ, now)
        if not hit:
            return "We don't change the clocks here."
        when, delta = hit
        moved = "back" if delta < 0 else "forward"
        what = "Daylight saving time ends" if delta < 0 else "Daylight saving time starts"
        n = (when.date() - today).days
        away = "tonight" if n == 0 else "tomorrow morning" if n == 1 else f"in {_span_days(n)}"
        return (f"{what} on {_date(when.date(), today)}, {away}. "
                f"At 2 AM the clocks go {moved} an hour.")

    if kind == "world":
        return _world_clock(arg, now, geocode)
    return None


def _holiday_sentence(label, d, today, explicit):
    n = (d - today).days
    if n == 0:
        return f"{_cap(label)} is today!"
    if n == 1:
        return f"{_cap(label)} is tomorrow, {_date(d, today)}."
    if n < 0:
        return f"{_cap(label)} was on {_date(d, today, force_year=explicit and d.year != today.year)}."
    when = f"{_span_days(n)} from now" if n <= 60 else f"{_span_days(n, weeks=True)} away" if n <= 120 else ""
    return f"{_cap(label)} is {_date(d, today, force_year=explicit and d.year != today.year)}" + \
        (f", {when}." if when else ".")


def _next_holiday(start):
    best = None
    for name, _, fn in _HOLIDAYS:
        d = _upcoming(fn, start)
        if best is None or d < best[0]:
            best = (d, name)
    return best


def _world_clock(place, now, geocode):
    if geocode is None:
        return None
    try:
        loc, ambiguous = geocode.resolve(place)
    except Exception:
        loc, ambiguous = None, None
    if ambiguous:
        return f"Did you mean {ambiguous[0]} or {ambiguous[1]}?"
    if not loc:
        return f"I couldn't find {place}."
    there = now.astimezone(ZoneInfo(loc.tz))
    name = loc.spoken or place
    if loc.tz == settings.WEATHER_HOME_TZ:
        return f"It's {_clock(there)} in {name}, same as here."
    diff = (there.utcoffset() - now.utcoffset()).total_seconds() / 3600
    hrs = abs(diff)
    hrs_s = str(int(hrs)) if hrs == int(hrs) else f"{hrs:g}"
    rel = "" if diff == 0 else f", {hrs_s} {'hour' if hrs == 1 else 'hours'} {'ahead of' if diff > 0 else 'behind'} us"
    day = "" if there.date() == now.date() else f" {there:%A}"
    return f"It's {_clock(there)}{day} in {name}{rel}."
