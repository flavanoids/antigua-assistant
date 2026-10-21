"""Store hours: "what time does H-E-B close", "is Target open". The place
comes from the drive-time skill's resolver (commute.Resolver), so "the
Lowe's on Ella" means the same store in both; its hours come from that
store's OpenStreetMap opening_hours tag, read from the OSM API.

parse_opening_hours() covers the common shapes ("Mo-Fr 07:00-22:00;
Sa-Su 08:00-22:00", "24/7", "08:00-22:00", "Su off", "PH closed"). A tag
it can't read, or a store with none, returns None, and the question goes
to web search as before.
"""

import logging
import re
import time
from datetime import date, datetime, timedelta

import requests

from . import settings
from .commute import Place

log = logging.getLogger("antigua_core")

_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_RANGE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})\+?$")


def _day_set(sel: str) -> set[int] | None:
    out = set()
    for part in sel.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a not in _DAYS or b not in _DAYS:
                return None
            i, j = _DAYS.index(a), _DAYS.index(b)
            out |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
        elif part in _DAYS:
            out.add(_DAYS.index(part))
        else:
            return None
    return out


def parse_opening_hours(tag: str) -> dict[int, list[tuple[int, int]]] | None:
    """weekday (0 = Monday) → [(open, close)] in minutes from midnight; a
    close past midnight is over 1440. None when the tag isn't understood."""
    tag = (tag or "").strip()
    if not tag:
        return None
    if tag == "24/7":
        return {d: [(0, 1440)] for d in range(7)}
    week: dict[int, list[tuple[int, int]]] = {}
    for rule in re.split(r"\s*;\s*|\s*\|\|\s*", tag):
        rule = re.sub(r'\s*(?:open\s*)?"[^"]*"', "", rule).strip()
        if not rule:
            continue
        if re.match(r"^(?:PH|SH)\b", rule):
            continue                          # holidays: not modelled
        if _day_set(rule) is not None:
            continue                          # a bare day list left after a comment
        m = re.match(r"^([A-Za-z,\-]+(?:\s*,\s*[A-Za-z\-]+)*)\s+(.+)$", rule)
        if m and not re.match(r"^\d", rule):
            days, times = _day_set(m.group(1).replace(" ", "")), m.group(2).strip()
            if days is None:
                return None
        else:
            days, times = set(range(7)), rule
        if times.lower() in ("off", "closed"):
            for d in days:
                week[d] = []
            continue
        if times == "00:00-24:00" or times == "24/7":
            spans = [(0, 1440)]
        else:
            spans = []
            for r in times.split(","):
                rm = _RANGE.match(r.strip())
                if not rm:
                    return None
                a = int(rm.group(1)) * 60 + int(rm.group(2))
                b = int(rm.group(3)) * 60 + int(rm.group(4))
                spans.append((a, b if b > a else b + 1440))
        for d in days:
            week[d] = spans                   # a later rule overrides an earlier one
    return week if week else None


# ── fetching ──

_tags: dict[str, tuple[float, str | None]] = {}
_TTL = 7 * 86400


def opening_hours(place: Place) -> str | None:
    """The OSM opening_hours tag for `place`, cached a week."""
    if not place.osm:
        return None
    hit = _tags.get(place.osm)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        r = requests.get(f"{settings.HOURS_OSM_API}/{place.osm}.json", timeout=settings.HOURS_TIMEOUT,
                         headers={"User-Agent": "Antigua-home-assistant/1.0"})
        r.raise_for_status()
        tag = ((r.json().get("elements") or [{}])[0].get("tags") or {}).get("opening_hours")
    except (requests.RequestException, ValueError, IndexError) as e:
        log.warning("Hours: %s: %s", place.osm, e)
        return None
    _tags[place.osm] = (time.time(), tag)
    return tag


# ── phrasing ──

def _clock(minutes: int) -> str:
    m = minutes % 1440
    if m == 0:
        return "midnight"
    if m == 720:
        return "noon"
    t = datetime(2000, 1, 1, m // 60, m % 60)
    return t.strftime("%-I %p") if not t.minute else t.strftime("%-I:%M %p")


def _day_word(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return f"on {d:%A}"


def _spans_text(spans) -> str:
    if spans == [(0, 1440)]:
        return "open twenty-four hours"
    return " and ".join(f"open {_clock(a)} to {_clock(b)}" for a, b in spans)


def _open_span(week: dict, now: datetime) -> tuple[int, int] | None:
    """The span open at `now` (today's, or last night's past midnight), in
    today's minutes."""
    mins = now.hour * 60 + now.minute
    for a, b in week.get(now.weekday()) or []:
        if a <= mins < b:
            return a, b
    for a, b in week.get((now.weekday() - 1) % 7) or []:
        if b > 1440 and mins < b - 1440:
            return a - 1440, b - 1440
    return None


def _next_open(week: dict, now: datetime) -> tuple[int, date] | None:
    mins = now.hour * 60 + now.minute
    for k in range(8):
        d = now.date() + timedelta(days=k)
        for a, _ in week.get(d.weekday()) or []:
            if k or a > mins:
                return a, d
    return None


def answer(kind: str, label: str, week: dict, day: date | None = None,
           now: datetime | None = None) -> str:
    """kind: "open_now" | "close" | "open" | "hours"; `day` for a named day."""
    now = now or datetime.now()
    today = now.date()
    name = label[:1].upper() + label[1:]
    if all(week.get(d) == [(0, 1440)] for d in range(7)):
        return f"{name} is open twenty-four hours."

    if day is not None and day != today:
        spans, when = week.get(day.weekday()), _day_word(day, today)
        if spans is None:
            return f"I don't know {label}'s hours {when}."
        if not spans:
            return f"{name} is closed {when}."
        if kind == "open":
            return f"{name} opens at {_clock(spans[0][0])} {when}."
        if kind == "close":
            return f"{name} closes at {_clock(spans[-1][1])} {when}."
        return f"{when[:1].upper()}{when[1:]}, {label} is {_spans_text(spans)}."

    spans = week.get(today.weekday())
    if spans is None:
        return f"I don't know {label}'s hours today."
    cur = _open_span(week, now)
    nxt = _next_open(week, now)
    reopens = ""
    if nxt:
        a, d = nxt
        reopens = f" It opens at {_clock(a)}{'' if d == today else ' ' + _day_word(d, today)}."
    if kind == "hours":
        if not spans:
            return f"{name} is closed today.{reopens}"
        return f"Today, {label} is {_spans_text(spans)}." + ("" if cur else " It's closed right now.")
    if cur:
        when = "" if cur[1] <= 6 * 60 else " tonight" if cur[1] >= 17 * 60 else " today"
        until = f"{_clock(cur[1])}{when}"
        if kind == "open_now":
            return f"Yes, {label} is open until {until}."
        if kind == "close":
            return f"{name} closes at {until}."
        return f"{name} is open now, until {until}."
    mins = now.hour * 60 + now.minute
    if kind == "close" and spans and mins >= spans[-1][1] % 1440:
        return f"{name} already closed today, at {_clock(spans[-1][1])}.{reopens}"
    if kind == "close" and spans:
        return f"{name} isn't open yet. Today it's {_spans_text(spans)}."
    if kind == "open" and nxt:
        return f"{name} opens at {_clock(nxt[0])} {_day_word(nxt[1], today)}."
    lead = "No, " if kind == "open_now" else ""
    return f"{lead}{label if lead else name} is closed right now.{reopens}"
