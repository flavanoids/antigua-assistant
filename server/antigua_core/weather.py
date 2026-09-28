"""Weather skill: Open-Meteo forecast + deterministic spoken answers.

Three orthogonal resolvers turn a transcript into an answer:

    intent   — what she asked (rain / temperature / general / umbrella / …)
    timeframe — when (now / soon / tonight / tomorrow / this Thursday / weekend / range)
    location  — where (home / a city / a region or country)

Each `phrase_*()` function is pure: forecast data + timeframe in, one spoken
sentence out. No LLM, so a weather answer cannot hallucinate, ramble, or stall.
The network lives entirely in WeatherProvider / GeocodeCache; everything below
that is unit-testable with a synthetic Forecast.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import settings
from .classify import resolve_day_offset

log = logging.getLogger("antigua_core")

_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HORIZON_DAYS = 7


# ── WMO weather codes ────────────────────────────────────────────────────────
# (spoken phrase, is_precip) — phrases are already TTS-friendly.
_WMO = {
    0: ("clear", False), 1: ("mostly clear", False), 2: ("partly cloudy", False),
    3: ("cloudy", False), 45: ("foggy", False), 48: ("foggy", False),
    51: ("drizzly", True), 53: ("drizzly", True), 55: ("drizzly", True),
    56: ("freezing drizzle", True), 57: ("freezing drizzle", True),
    61: ("light rain", True), 63: ("rain", True), 65: ("heavy rain", True),
    66: ("freezing rain", True), 67: ("freezing rain", True),
    71: ("light snow", True), 73: ("snow", True), 75: ("heavy snow", True),
    77: ("snow", True), 80: ("rain showers", True), 81: ("rain showers", True),
    82: ("heavy showers", True), 85: ("snow showers", True), 86: ("snow showers", True),
    95: ("thunderstorms", True), 96: ("thunderstorms", True), 99: ("thunderstorms", True),
}


def _wmo_desc(code) -> str:
    return _WMO.get(int(code or 0), ("unsettled", False))[0]


def _wmo_is_precip(code) -> bool:
    return _WMO.get(int(code or 0), ("", False))[1]


def _precip_noun(code) -> str:
    c = int(code or 0)
    if c in (71, 73, 75, 77, 85, 86):
        return "snow"
    if c in (95, 96, 99):
        return "storms"
    return "rain"


# ── US state → representative city (Open-Meteo geocoding has no reliable ADM1
# entries for US states, so "weather in Hawaii" needs a lookup table). ────────
_US_STATES = {
    "alabama": ("Birmingham", 33.52, -86.81), "alaska": ("Anchorage", 61.22, -149.90),
    "arizona": ("Phoenix", 33.45, -112.07), "arkansas": ("Little Rock", 34.75, -92.29),
    "california": ("Los Angeles", 34.05, -118.24), "colorado": ("Denver", 39.74, -104.98),
    "connecticut": ("Hartford", 41.76, -72.69), "delaware": ("Wilmington", 39.75, -75.55),
    "florida": ("Miami", 25.76, -80.19), "georgia": ("Atlanta", 33.75, -84.39),
    "hawaii": ("Honolulu", 21.31, -157.86), "idaho": ("Boise", 43.62, -116.20),
    "illinois": ("Chicago", 41.85, -87.65), "indiana": ("Indianapolis", 39.77, -86.16),
    "iowa": ("Des Moines", 41.60, -93.61), "kansas": ("Wichita", 37.69, -97.34),
    "kentucky": ("Louisville", 38.25, -85.76), "louisiana": ("New Orleans", 29.95, -90.07),
    "maine": ("Portland", 43.66, -70.26), "maryland": ("Baltimore", 39.29, -76.61),
    "massachusetts": ("Boston", 42.36, -71.06), "michigan": ("Detroit", 42.33, -83.05),
    "minnesota": ("Minneapolis", 44.98, -93.27), "mississippi": ("Jackson", 32.30, -90.18),
    "missouri": ("Kansas City", 39.10, -94.58), "montana": ("Billings", 45.78, -108.50),
    "nebraska": ("Omaha", 41.26, -95.93), "nevada": ("Las Vegas", 36.17, -115.14),
    "new hampshire": ("Manchester", 42.99, -71.46), "new jersey": ("Newark", 40.74, -74.17),
    "new mexico": ("Albuquerque", 35.08, -106.65), "new york": ("New York City", 40.71, -74.01),
    "north carolina": ("Charlotte", 35.23, -80.84), "north dakota": ("Fargo", 46.88, -96.79),
    "ohio": ("Columbus", 39.96, -83.00), "oklahoma": ("Oklahoma City", 35.47, -97.52),
    "oregon": ("Portland", 45.52, -122.68), "pennsylvania": ("Philadelphia", 39.95, -75.16),
    "rhode island": ("Providence", 41.82, -71.41), "south carolina": ("Columbia", 34.00, -81.03),
    "south dakota": ("Sioux Falls", 43.55, -96.70), "tennessee": ("Nashville", 36.16, -86.78),
    "texas": ("Houston", 29.76, -95.37), "utah": ("Salt Lake City", 40.76, -111.89),
    "vermont": ("Burlington", 44.48, -73.21), "virginia": ("Virginia Beach", 36.85, -75.98),
    "washington": ("Seattle", 47.61, -122.33), "west virginia": ("Charleston", 38.35, -81.63),
    "wisconsin": ("Milwaukee", 43.04, -87.91), "wyoming": ("Cheyenne", 41.14, -104.82),
}


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class Location:
    lat: float
    lon: float
    tz: str = "America/Chicago"
    spoken: str = ""       # how the answer names the place ("Denver", "Guatemala City")
    kind: str = "home"     # home | city | region
    is_us: bool = True


@dataclass
class Conditions:
    temp_f: int
    feels_f: int
    humidity: int
    wind_mph: int
    code: int


@dataclass
class Day:
    d: date
    high_f: int
    low_f: int
    code: int
    rain_pct: int
    sunrise: str = ""   # "6:48 AM"
    sunset: str = ""
    uv_max: float = 0.0


@dataclass
class Hour:
    dt: datetime        # location-local, naive
    temp_f: int
    rain_pct: int
    code: int


@dataclass
class Forecast:
    location: Location
    tz: str
    current: Conditions
    hourly: list[Hour]
    days: list[Day]                       # today first, HORIZON_DAYS long
    yesterday: Day | None = None
    fetched_at: float = field(default_factory=time.time)
    stale: bool = False

    def day_for(self, d: date) -> Day | None:
        return next((x for x in self.days if x.d == d), None)


# ── Timeframe resolver ──────────────────────────────────────────────────────


@dataclass
class Timeframe:
    kind: str            # current | hourly | day | range | beyond
    label: str           # spoken ("this afternoon", "Thursday", "this weekend")
    start: datetime | None = None    # for hourly
    end: datetime | None = None
    day: date | None = None          # for day
    days: list[date] = field(default_factory=list)   # for range
    days_out: int = 0                 # max horizon distance, drives hedging


_PART_HOURS = {
    "morning": (6, 11), "afternoon": (12, 17), "evening": (18, 21),
    "night": (19, 23), "tonight": (19, 23),
}
_SOON_RE = re.compile(
    r"\b(right now|in a bit|in a moment|soon|shortly|"
    r"in (?:the )?next (?:hour|couple hours|few hours)|later today|"
    r"rest of (?:the|today)|this hour)\b", re.IGNORECASE)
_WEEKEND_RE = re.compile(r"\b(this |next |the )?weekend\b", re.IGNORECASE)
_WEEK_RE = re.compile(r"\b(this week|rest of the week|next few days|coming days|"
                      r"next couple(?: of)? days|next (\d+) days)\b", re.IGNORECASE)
_IN_N_DAYS_RE = re.compile(r"\bin (\d+) days?\b", re.IGNORECASE)
_DOW = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DOW_RE = re.compile(r"\b(?:this |next |on |coming )?(" + "|".join(_DOW) + r")\b", re.IGNORECASE)


_MONTH_RE = re.compile(
    r"\b(?:in|for|during)\s+(january|february|march|april|may|june|july|"
    r"august|september|october|november|december)\b|\b(next|this)\s+month\b",
    re.IGNORECASE)
_SEASON_RE = re.compile(r"\b(?:this |next |in (?:the )?)(spring|summer|fall|autumn|winter)\b",
                        re.IGNORECASE)


def resolve_timeframe(text: str, now: datetime) -> Timeframe:
    """Map a transcript + current local time to a concrete window."""
    t = text.lower()
    today = now.date()

    # Seasonal / month-out questions are past the forecast horizon.
    m = _MONTH_RE.search(t) or _SEASON_RE.search(t)
    if m:
        return Timeframe("beyond", next((g for g in m.groups() if g), "then"), days_out=99)

    # "in N days" / "in 3 days"
    m = _IN_N_DAYS_RE.search(t)
    if m:
        n = int(m.group(1))
        if n > HORIZON_DAYS:
            return Timeframe("beyond", f"in {n} days", days_out=n)
        return Timeframe("day", _day_label(today, today + timedelta(days=n), now),
                         day=today + timedelta(days=n), days_out=n)

    # weekend
    m = _WEEKEND_RE.search(t)
    if m:
        which = (m.group(1) or "this").strip().lower()
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        if today.weekday() >= 5 and which != "next":   # already the weekend
            sat = today - timedelta(days=today.weekday() - 5)
        if which == "next":
            sat = sat + timedelta(days=7)
        days = [sat, sat + timedelta(days=1)]
        days_out = (days[-1] - today).days
        if days_out > HORIZON_DAYS:
            return Timeframe("beyond", f"{which} weekend", days_out=days_out)
        return Timeframe("range", f"{which} weekend" if which != "this" else "this weekend",
                         days=days, days_out=days_out)

    # multi-day ranges
    m = _WEEK_RE.search(t)
    if m:
        n = int(m.group(2)) if m.group(2) else (
            (6 - today.weekday()) if "week" in m.group(1) else 3)
        n = max(1, min(n, HORIZON_DAYS))
        days = [today + timedelta(days=i) for i in range(n + 1)]
        return Timeframe("range", _range_label(m.group(1)), days=days, days_out=n)

    # named weekday
    m = _DOW_RE.search(t)
    if m:
        offset = resolve_day_offset(m.group(1), now)
        if offset is not None:
            # "next Thursday" pushes a week out only when that stays in range.
            if re.search(r"\bnext\b", t[max(0, m.start() - 6):m.start()]) \
                    and offset + 7 <= HORIZON_DAYS:
                offset += 7
            d = today + timedelta(days=offset)
            if offset > HORIZON_DAYS:
                return Timeframe("beyond", m.group(1).title(), days_out=offset)
            part = _part_in(t)
            if part and offset <= 1:
                return _hourly_tf(now, d, part, offset)
            return Timeframe("day", _day_label(today, d, now), day=d, days_out=offset)

    # tomorrow (+ optional part of day)
    if "tomorrow" in t:
        d = today + timedelta(days=1)
        part = _part_in(t)
        if part:
            return _hourly_tf(now, d, part, 1)
        return Timeframe("day", "tomorrow", day=d, days_out=1)

    # tonight / this evening / this afternoon / this morning
    part = _part_in(t)
    if part:
        return _hourly_tf(now, today, part, 0)

    # soon / later today / rest of the day
    if _SOON_RE.search(t):
        if re.search(r"\b(later today|rest of (?:the|today))\b", t):
            end = now.replace(hour=23, minute=59, second=0, microsecond=0)
            return Timeframe("hourly", "later today", start=now, end=end, days_out=0)
        return Timeframe("hourly", "in the next few hours", start=now,
                         end=now + timedelta(hours=3), days_out=0)

    # explicit "today"
    if re.search(r"\btoday\b", t):
        return Timeframe("day", "today", day=today, days_out=0)

    return Timeframe("current", "right now", days_out=0)


def _part_in(t: str) -> str | None:
    for part in ("morning", "afternoon", "evening", "tonight", "night"):
        if re.search(rf"\b{part}\b", t):
            return "night" if part in ("tonight", "night") else part
    return None


def _hourly_tf(now: datetime, d: date, part: str, days_out: int) -> Timeframe:
    lo, hi = _PART_HOURS[part]
    start = datetime.combine(d, datetime.min.time()).replace(hour=lo)
    end = datetime.combine(d, datetime.min.time()).replace(hour=hi)
    if days_out == 0 and now.hour > lo:
        start = now.replace(minute=0, second=0, microsecond=0)
    label = ("tonight" if part == "night" and days_out == 0 else
             f"{'tomorrow ' if days_out == 1 else 'this '}{part}"
             if part != "night" else
             ("tomorrow night" if days_out == 1 else "tonight"))
    return Timeframe("hourly", label, start=start, end=end, days_out=days_out)


def _day_label(today: date, d: date, now: datetime) -> str:
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return d.strftime("%A")


def _range_label(g: str) -> str:
    g = (g or "").strip().lower()
    if "rest of the week" in g:
        return "the rest of the week"
    if g in ("this week",):
        return "this week"
    return "the next few days"


# ── Phrasing helpers ────────────────────────────────────────────────────────


def _round5(n) -> int:
    return int(round(float(n) / 5.0) * 5)


def _hedge(days_out: int) -> str:
    """Confidence-graded lead-in. Flat for 0-2 days, softened beyond."""
    if days_out <= 3:
        return ""
    if days_out <= 5:
        return "it looks like "
    return "early signs point to "


def _band(feels_f: int) -> str:
    if feels_f >= 95:
        return "sweltering"
    if feels_f >= 85:
        return "hot"
    if feels_f >= 72:
        return "warm"
    if feels_f >= 60:
        return "mild"
    if feels_f >= 45:
        return "chilly"
    if feels_f >= 32:
        return "cold"
    return "freezing"


def _hours_in(fc: Forecast, tf: Timeframe) -> list[Hour]:
    if tf.start and tf.end:
        return [h for h in fc.hourly if tf.start <= h.dt <= tf.end]
    if tf.day:
        return [h for h in fc.hourly if h.dt.date() == tf.day]
    return []


def _rain_verdict(pct: int) -> str:
    likely = settings.WEATHER_RAIN_LIKELY_PCT
    slight = settings.WEATHER_RAIN_SLIGHT_PCT
    if pct >= likely:
        return "likely"
    if pct >= slight:
        return "possible"
    return "unlikely"


def _fmt_hour(dt: datetime) -> str:
    if dt.hour == 12 and dt.minute == 0:
        return "noon"
    if dt.hour == 0 and dt.minute == 0:
        return "midnight"
    return dt.strftime("%-I %p")


# ── phrase_* : forecast + timeframe -> one spoken sentence ───────────────────


def phrase_current(fc: Forecast, tf: Timeframe, now: datetime) -> str:
    c = fc.current
    today = fc.days[0] if fc.days else None
    desc = _wmo_desc(c.code)
    s = f"It's {c.temp_f} and {desc} right now"
    if today and now.hour < 15:
        s += f", heading for a high near {_round5(today.high_f)}"
    elif today:
        s += f", with a low tonight around {_round5(today.low_f)}"
    return s + "."


def phrase_rain(fc: Forecast, tf: Timeframe, now: datetime) -> str:
    if tf.kind == "current":
        if _wmo_is_precip(fc.current.code):
            return "Yes, it's coming down right now."
        near = [h for h in fc.hourly if 0 <= (h.dt - now).total_seconds() <= 7200]
        rest = [h for h in fc.hourly if now <= h.dt and h.dt.date() == now.date()]
        near_pct = max((h.rain_pct for h in near), default=0)
        if _rain_verdict(near_pct) != "unlikely":
            lead = "Yes" if _rain_verdict(near_pct) == "likely" else "Maybe"
            return f"{lead} — rain's {_rain_verdict(near_pct)} within the hour, around {near_pct} percent."
        later = max(rest, key=lambda h: h.rain_pct, default=None)
        if later and _rain_verdict(later.rain_pct) != "unlikely":
            return (f"Dry right now, but rain's {_rain_verdict(later.rain_pct)} later, "
                    f"around {_fmt_hour(later.dt)}, about {later.rain_pct} percent.")
        return "No, it looks dry for the rest of the day."

    if tf.kind == "range":
        wet = _wettest_day(fc, tf.days)
        h = _hedge(tf.days_out)
        if wet is None or wet[1] < settings.WEATHER_RAIN_SLIGHT_PCT:
            return f"{h.capitalize()}{tf.label} looks dry.".replace("  ", " ")
        day, pct = wet
        name = "today" if day.d == now.date() else day.d.strftime("%A")
        noun = _precip_noun(day.code)
        if pct >= settings.WEATHER_RAIN_LIKELY_PCT:
            return (f"{h.capitalize()}{name} is the wet one, around {pct} percent. "
                    f"The other days look drier.").replace("  ", " ")
        return (f"{h.capitalize()}mostly dry, with the best chance of {noun} {name}, "
                f"about {pct} percent.").replace("  ", " ")

    if tf.kind == "hourly":
        hrs = _hours_in(fc, tf)
        if hrs:
            peak = max(hrs, key=lambda h: h.rain_pct)
            v = _rain_verdict(peak.rain_pct)
            if v == "unlikely":
                return f"Probably not — under {settings.WEATHER_RAIN_SLIGHT_PCT} percent through {tf.label}."
            when = _fmt_hour(peak.dt)
            lead = "Yes" if v == "likely" else "Maybe"
            return f"{lead} — the best chance {tf.label} is around {when}, about {peak.rain_pct} percent."
        # fall through to the day summary

    # day
    day = fc.day_for(tf.day) if tf.day else (fc.days[0] if fc.days else None)
    if day is None:
        return "I don't have the forecast for that day."
    v = _rain_verdict(day.rain_pct)
    h = _hedge(tf.days_out)
    noun = _precip_noun(day.code)
    if v == "unlikely":
        return f"{h.capitalize()}{tf.label} looks dry, only about {day.rain_pct} percent.".replace("  ", " ")
    lead = "Yes" if v == "likely" else "Maybe"
    return f"{lead}, {h}{tf.label} has about a {day.rain_pct} percent chance of {noun}.".replace("  ", " ")


def phrase_temp(fc: Forecast, tf: Timeframe, now: datetime) -> str:
    if tf.kind == "current":
        c = fc.current
        extra = f", feels like {c.feels_f}" if abs(c.feels_f - c.temp_f) >= 4 else ""
        return f"It's {c.temp_f} out{extra}, so {_band(c.feels_f)}."
    if tf.kind == "hourly":
        hrs = _hours_in(fc, tf)
        if hrs:
            lo = min(h.temp_f for h in hrs)
            hi = max(h.temp_f for h in hrs)
            rng = f"{lo} to {hi}" if hi - lo >= 4 else f"{hi}"
            return f"Around {rng} {tf.label}."
    day = fc.day_for(tf.day) if tf.day else (fc.days[0] if fc.days else None)
    if day is None:
        return "I don't have that day's forecast."
    h = _hedge(tf.days_out)
    return (f"{h.capitalize()}{tf.label} tops out near {_round5(day.high_f)}, "
            f"down to about {_round5(day.low_f)}.").replace("  ", " ")


def phrase_general(fc: Forecast, tf: Timeframe, now: datetime) -> str:
    if tf.kind == "current":
        return phrase_current(fc, tf, now)
    if tf.kind == "range":
        return phrase_range(fc, tf, now)
    if tf.kind == "hourly":
        hrs = _hours_in(fc, tf)
        if hrs:
            codes = [h.code for h in hrs]
            desc = _wmo_desc(max(set(codes), key=codes.count))
            lo = min(h.temp_f for h in hrs)
            hi = max(h.temp_f for h in hrs)
            rng = f"{lo} to {hi}" if hi - lo >= 4 else f"{hi}"
            return f"{tf.label.capitalize()}: {desc}, around {rng}."
    day = fc.day_for(tf.day) if tf.day else (fc.days[0] if fc.days else None)
    if day is None:
        return "I don't have that day's forecast yet."
    h = _hedge(tf.days_out)
    rain = ""
    if day.rain_pct >= settings.WEATHER_RAIN_SLIGHT_PCT:
        rain = f", {day.rain_pct} percent chance of rain"
    return (f"{h.capitalize()}{tf.label}: {_wmo_desc(day.code)}, high near "
            f"{_round5(day.high_f)}, low around {_round5(day.low_f)}{rain}.").replace("  ", " ")


def phrase_range(fc: Forecast, tf: Timeframe, now: datetime) -> str:
    days = [fc.day_for(d) for d in tf.days]
    days = [d for d in days if d]
    if not days:
        return "I don't have the forecast that far out."
    if len(days) <= 2:
        parts = []
        for d in days:
            name = "today" if d.d == now.date() else (
                "tomorrow" if d.d == now.date() + timedelta(days=1) else d.d.strftime("%A"))
            parts.append(f"{name} {_wmo_desc(d.code)}, near {_round5(d.high_f)}")
        return _sentence_join(parts) + "."
    highs = [d.high_f for d in days]
    wet = _wettest_day(fc, tf.days)
    warm_trend = "warming up" if highs[-1] - highs[0] >= 8 else (
        "cooling off" if highs[0] - highs[-1] >= 8 else "steady")
    h = _hedge(tf.days_out)
    base = f"{h.capitalize()}{tf.label}: {warm_trend}, highs in the {_round5(sum(highs)//len(highs))}s".replace("  ", " ")
    if wet and wet[1] >= settings.WEATHER_RAIN_SLIGHT_PCT:
        wname = "today" if wet[0].d == now.date() else wet[0].d.strftime("%A")
        return base + f", with rain most likely {wname}."
    return base + ", staying mostly dry."


def phrase_umbrella(fc: Forecast, tf: Timeframe, now: datetime) -> str:
    # "Do I need an umbrella?" with no timeframe means today, not this instant.
    label = "today" if tf.kind == "current" else tf.label
    day = _reference_day(fc, tf, now)
    pct = day.rain_pct if day else 0
    if tf.kind == "hourly":
        hrs = _hours_in(fc, tf)
        if hrs:
            pct = max(h.rain_pct for h in hrs)
    if pct >= settings.WEATHER_RAIN_LIKELY_PCT:
        return f"Yes, I'd take one — rain's likely {label}, around {pct} percent."
    if pct >= settings.WEATHER_RAIN_SLIGHT_PCT:
        return f"Maybe keep one handy — about a {pct} percent chance {label}."
    return f"No, you should be fine {label}."


def phrase_jacket(fc: Forecast, tf: Timeframe, now: datetime) -> str:
    if tf.kind == "current":
        feels = fc.current.feels_f
    else:
        day = _reference_day(fc, tf, now)
        feels = day.low_f if day else fc.current.feels_f
    thresh = settings.WEATHER_JACKET_BELOW_F
    when = "right now" if tf.kind == "current" else tf.label
    if feels < thresh - 12:
        return f"Yes, bundle up — it's {_band(feels)} {when}, around {feels}."
    if feels < thresh:
        return f"A light one — it's about {feels} {when}."
    return f"No, it's {_band(feels)} {when}, around {feels}."


def phrase_sunset(fc: Forecast, tf: Timeframe, now: datetime) -> str:
    day = _reference_day(fc, tf, now) or (fc.days[0] if fc.days else None)
    if not day or not day.sunset:
        return "I don't have the sunset time right now."
    when = "tonight" if day.d == now.date() else tf.label
    return f"Sunset {when} is at {day.sunset}."


def phrase_sunrise(fc: Forecast, tf: Timeframe, now: datetime) -> str:
    day = _reference_day(fc, tf, now) or (fc.days[0] if fc.days else None)
    if not day or not day.sunrise:
        return "I don't have the sunrise time right now."
    return f"Sunrise is at {day.sunrise}."


def phrase_compare_yesterday(fc: Forecast, tf: Timeframe, now: datetime) -> str:
    y = fc.yesterday
    today = fc.days[0] if fc.days else None
    if not y or not today:
        return "I don't have yesterday's numbers to compare."
    diff = today.high_f - y.high_f
    if abs(diff) <= 2:
        return f"About the same as yesterday, near {_round5(today.high_f)}."
    word = "warmer" if diff > 0 else "cooler"
    return f"About {abs(diff)} degrees {word} than yesterday, near {_round5(today.high_f)}."


def phrase_wind(fc: Forecast, tf: Timeframe, now: datetime) -> str:
    w = fc.current.wind_mph
    if w >= settings.WEATHER_NOTABLE_WIND_MPH:
        return f"Breezy — wind's around {w} miles an hour."
    return f"Light wind, about {w} miles an hour."


# ── phrasing utilities ──────────────────────────────────────────────────────


def _reference_day(fc: Forecast, tf: Timeframe, now: datetime) -> Day | None:
    if tf.day:
        return fc.day_for(tf.day)
    if tf.days:
        return fc.day_for(tf.days[0])
    if tf.start:
        return fc.day_for(tf.start.date())
    return fc.days[0] if fc.days else None


def _wettest_day(fc: Forecast, days: list[date]):
    cand = [(fc.day_for(d)) for d in days]
    cand = [d for d in cand if d]
    if not cand:
        return None
    top = max(cand, key=lambda d: d.rain_pct)
    return (top, top.rain_pct)


def _sentence_join(parts: list[str]) -> str:
    return ", ".join(parts[:-1]) + (f", and {parts[-1]}" if len(parts) > 1 else parts[0])


# ── Intent classifier ───────────────────────────────────────────────────────

_INTENT_PATTERNS = [
    ("compare", re.compile(r"\b(warmer|colder|cooler|hotter|hot|cold)\s+"
                           r"(?:today\s+)?than\s+(yesterday|it was)", re.IGNORECASE)),
    ("umbrella", re.compile(r"\b(umbrella|rain\s*coat|rain\s*jacket)\b", re.IGNORECASE)),
    ("jacket", re.compile(r"\b(need a (?:jacket|coat|sweater)|bundle up|dress warm|"
                          r"how cold is it|is it cold|is it chilly|is it freezing|"
                          r"warm enough|shorts weather|wear shorts)\b", re.IGNORECASE)),
    ("rain", re.compile(r"\b(rain|raining|rainy|drizzl\w*|pouring|showers?|"
                        r"thunder\w*|thunderstorm\w*|hail|snow\w*|sleet|"
                        r"chance of (?:rain|showers|storms)|wet outside)\b", re.IGNORECASE)),
    ("sunset", re.compile(r"\bsun\s*set\b|\bget\s+dark\b|\bwhen.*(dusk|nightfall)\b",
                          re.IGNORECASE)),
    ("sunrise", re.compile(r"\bsun\s*rise\b|\bwhen.*(sunup|get\s+light)\b", re.IGNORECASE)),
    ("wind", re.compile(r"\b(wind|windy|winds|breezy|breeze|gusty|gusts)\b", re.IGNORECASE)),
    ("temp", re.compile(r"\b(temperature|how hot|how warm|how cold|hot out|"
                        r"high (?:today|tomorrow)|how many degrees|"
                        r"what.*(?:the )?(?:temp|degrees)|feels? like out)\b", re.IGNORECASE)),
    ("general", re.compile(r"\b(weather|forecast|nice out|nice outside|"
                           r"how.?s it (?:look|feel)ing? outside)\b", re.IGNORECASE)),
]


def classify_weather_intent(text: str) -> str | None:
    """Return the weather intent for a transcript, or None if it isn't weather.

    Selects which phrase_*() function answers. classify._WEATHER_ROUTE_RE is the
    coarser gate that decides the turn is weather at all — keep the two in sync.
    """
    for name, pat in _INTENT_PATTERNS:
        if pat.search(text):
            return name
    return None


_INTENT_FUNCS = {
    "compare": phrase_compare_yesterday,
    "umbrella": phrase_umbrella,
    "jacket": phrase_jacket,
    "rain": phrase_rain,
    "sunset": phrase_sunset,
    "sunrise": phrase_sunrise,
    "wind": phrase_wind,
    "temp": phrase_temp,
    "general": phrase_general,
}


# ── Location extraction ─────────────────────────────────────────────────────

_LOC_RE = re.compile(
    r"\b(?:in|for|at|around|near|over)\s+"
    r"(?:the\s+(?:city\s+of\s+|country\s+of\s+|state\s+of\s+)?)?"
    r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,3})"
    r"(?:\s*,?\s*([A-Z][A-Za-z]+))?",
)
_LOC_STOPWORDS = {
    "here", "outside", "town", "my area", "the morning", "the afternoon",
    "the evening", "a bit", "a moment", "the next", "a few", "the week",
    "the weekend", "the day", "fahrenheit", "celsius", "the sun", "the shade",
    "general", "particular", "fact", "case", "advance",
}
_MONTHS = {"january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"}


def extract_location_query(text: str) -> tuple[str, str | None] | None:
    """Return (place, admin_hint) from 'weather in Denver' / 'rain in Portland, Maine'."""
    for m in _LOC_RE.finditer(text):
        place = m.group(1).strip()
        low = place.lower()
        if low in _LOC_STOPWORDS or low in _MONTHS or low in _DOW:
            continue
        if low in ("today", "tomorrow", "tonight", "the weekend", "a week"):
            continue
        return place, (m.group(2).strip() if m.group(2) else None)
    return None


# ── Geocoding ───────────────────────────────────────────────────────────────


class GeocodeCache:
    """Name -> Location, with persistent cache and user corrections."""

    def __init__(self, path=None):
        self._path = path or (settings.DATA_DIR / "geocode_cache.json")
        self._lock = threading.Lock()
        self._data = {"places": {}, "corrections": {}}
        self._load()

    def _load(self):
        try:
            with open(self._path) as f:
                self._data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except OSError as e:
            log.warning("geocode cache save failed: %s", e)

    def remember_correction(self, query: str, loc: Location):
        with self._lock:
            self._data["corrections"][query.lower().strip()] = _loc_to_dict(loc)
            self._save()

    def resolve(self, place: str, admin_hint: str | None = None):
        """Return (Location, ambiguous) — ambiguous is a (name_a, name_b) tuple
        when two candidates are close and no hint disambiguates."""
        key = place.lower().strip()
        with self._lock:
            if key in self._data["corrections"]:
                return _loc_from_dict(self._data["corrections"][key]), None
            if not admin_hint and key in self._data["places"]:
                return _loc_from_dict(self._data["places"][key]), None

        # US state shortcut
        if key in _US_STATES:
            city, lat, lon = _US_STATES[key]
            loc = Location(lat, lon, _tz_guess(lon), city, "city", True)
            self._store(key, loc)
            return loc, None

        results = self._geo_lookup(place, admin_hint)
        if not results:
            return None, None

        if admin_hint:
            h = admin_hint.lower()
            results = [r for r in results
                       if h in (r.get("admin1", "") or "").lower()
                       or h in (r.get("country", "") or "").lower()] or results

        top = results[0]
        # near-tie? second candidate with comparable population in another region
        if (len(results) > 1 and not admin_hint
                and (results[1].get("population") or 0) > 0.5 * (top.get("population") or 1)
                and results[1].get("admin1") != top.get("admin1")):
            return None, (_cand_name(top), _cand_name(results[1]))

        loc = _result_to_location(top)
        self._store(key, loc)
        return loc, None

    def _store(self, key, loc: Location):
        with self._lock:
            self._data["places"][key] = _loc_to_dict(loc)
            self._save()

    def _geo_lookup(self, place, admin_hint):
        q = urllib.parse.urlencode(
            {"name": place, "count": 5, "language": "en", "format": "json"})
        try:
            with urllib.request.urlopen(f"{_GEO_URL}?{q}",
                                        timeout=settings.WEATHER_TIMEOUT) as r:
                return json.loads(r.read()).get("results", []) or []
        except Exception as e:
            log.warning("geocoding failed for %r: %s", place, e)
            return []


def _cand_name(r: dict) -> str:
    a = r.get("admin1")
    return f"{r['name']}, {a}" if a else r["name"]


def _result_to_location(r: dict) -> Location:
    fc = r.get("feature_code", "")
    is_us = (r.get("country_code") == "US") or (r.get("country") == "United States")
    kind = "region" if fc.startswith(("PCLI", "ADM1", "ADM2")) else "city"
    return Location(r["latitude"], r["longitude"],
                    r.get("timezone") or _tz_guess(r["longitude"]),
                    r["name"], kind, is_us)


def _tz_guess(lon: float) -> str:
    # Rough US fallback when the API omits a timezone.
    if lon < -115:
        return "America/Los_Angeles"
    if lon < -100:
        return "America/Denver"
    if lon < -87:
        return "America/Chicago"
    return "America/New_York"


def _loc_to_dict(loc: Location) -> dict:
    return {"lat": loc.lat, "lon": loc.lon, "tz": loc.tz,
            "spoken": loc.spoken, "kind": loc.kind, "is_us": loc.is_us}


def _loc_from_dict(d: dict) -> Location:
    return Location(d["lat"], d["lon"], d.get("tz", "America/Chicago"),
                    d.get("spoken", ""), d.get("kind", "city"), d.get("is_us", True))


# ── Weather provider (forecast fetch + cache) ───────────────────────────────


class WeatherProvider:
    def __init__(self, home: Location, geocode: GeocodeCache | None = None):
        self.home = home
        self.geocode = geocode or GeocodeCache()
        self._cache: dict[tuple, tuple[Forecast, float]] = {}
        self._lock = threading.Lock()
        self._ttl = settings.WEATHER_TTL

    @staticmethod
    def _key(loc: Location) -> tuple:
        return (round(loc.lat, 2), round(loc.lon, 2))

    def get(self, loc: Location | None = None) -> Forecast | None:
        loc = loc or self.home
        k = self._key(loc)
        now = time.time()
        with self._lock:
            hit = self._cache.get(k)
        if hit:
            fc, at = hit
            if now - at >= self._ttl:
                threading.Thread(target=self._refresh, args=(loc,), daemon=True).start()
                fc.stale = now - at >= self._ttl * 4
            return fc
        return self._refresh(loc)

    _FAIL_TTL = 90

    def _refresh(self, loc: Location) -> Forecast | None:
        k = self._key(loc)
        try:
            fc = self._fetch(loc)
            with self._lock:
                self._cache[k] = (fc, time.time())
            log.info("weather fetched: %s %d°F", loc.spoken or "home", fc.current.temp_f)
            return fc
        except Exception as e:
            log.warning("weather fetch failed for %s: %s", loc.spoken or "home", e)
            with self._lock:
                if k in self._cache:
                    fc = self._cache[k][0]
                    fc.stale = True
                    return fc
                self._cache[k] = (None, time.time() - self._ttl + self._FAIL_TTL)
            return None

    def prewarm(self, city_names: list[str]):
        threading.Thread(target=self.get, daemon=True).start()
        for name in city_names or []:
            loc, _ = self.geocode.resolve(name)
            if loc:
                threading.Thread(target=self.get, args=(loc,), daemon=True).start()

    def _fetch(self, loc: Location) -> Forecast:
        params = {
            "latitude": loc.lat, "longitude": loc.lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "weather_code,wind_speed_10m",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,sunrise,sunset,uv_index_max",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "precipitation_unit": "inch", "timezone": "auto",
            "forecast_days": HORIZON_DAYS, "forecast_hours": 48, "past_days": 1,
        }
        url = f"{_FORECAST_URL}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=settings.WEATHER_TIMEOUT) as r:
            raw = json.loads(r.read())
        return _parse_forecast(raw, loc)


def _parse_forecast(raw: dict, loc: Location) -> Forecast:
    tz = raw.get("timezone", loc.tz)
    cur = raw["current"]
    current = Conditions(
        temp_f=round(cur["temperature_2m"]),
        feels_f=round(cur["apparent_temperature"]),
        humidity=round(cur["relative_humidity_2m"]),
        wind_mph=round(cur["wind_speed_10m"]),
        code=int(cur["weather_code"]),
    )
    hrs = raw.get("hourly", {})
    hourly = []
    for i, ts in enumerate(hrs.get("time", [])):
        hourly.append(Hour(
            dt=datetime.fromisoformat(ts),
            temp_f=round(hrs["temperature_2m"][i]),
            rain_pct=int(hrs["precipitation_probability"][i] or 0),
            code=int(hrs["weather_code"][i]),
        ))
    dl = raw.get("daily", {})
    parsed_days = []
    for i, ds in enumerate(dl.get("time", [])):
        parsed_days.append(Day(
            d=date.fromisoformat(ds),
            high_f=round(dl["temperature_2m_max"][i]),
            low_f=round(dl["temperature_2m_min"][i]),
            code=int(dl["weather_code"][i]),
            rain_pct=int(dl["precipitation_probability_max"][i] or 0),
            sunrise=_iso_to_clock(dl["sunrise"][i]),
            sunset=_iso_to_clock(dl["sunset"][i]),
            uv_max=float(dl["uv_index_max"][i] or 0),
        ))
    today = datetime.now(ZoneInfo(tz)).date()
    yesterday = next((d for d in parsed_days if d.d == today - timedelta(days=1)), None)
    days = [d for d in parsed_days if d.d >= today]
    return Forecast(location=loc, tz=tz, current=current, hourly=hourly,
                    days=days, yesterday=yesterday)


def _iso_to_clock(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%-I:%M %p")
    except ValueError:
        return ""


# ── Entry point ─────────────────────────────────────────────────────────────


def _now_local(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz)).replace(tzinfo=None)


def _loc_prefix(loc: Location, home: Location) -> str:
    if loc is home or loc.kind == "home":
        return ""
    if loc.kind == "region":
        return f"Across {loc.spoken}, "
    return f"In {loc.spoken}, "


def _cap_sentence(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s[:1].upper() + s[1:] if s else s


def answer(text: str, provider: WeatherProvider, *, now: datetime | None = None) -> str | None:
    """Full deterministic weather answer, or None if this isn't a weather turn."""
    intent = classify_weather_intent(text)
    if intent is None:
        return None

    home = provider.home
    loc = home
    lq = extract_location_query(text)
    if lq:
        resolved, ambiguous = provider.geocode.resolve(lq[0], lq[1])
        if ambiguous:
            return f"Do you mean {ambiguous[0]} or {ambiguous[1]}?"
        if resolved:
            loc = resolved
        else:
            return f"I couldn't find {lq[0]}."

    fc = provider.get(loc)
    if fc is None:
        where = "" if loc is home else f" for {loc.spoken}"
        return (f"I can't reach the weather service{where} right now. "
                f"Try again in a few minutes.")

    now = now or _now_local(fc.tz)
    tf = resolve_timeframe(text, now)
    if tf.kind == "beyond":
        last = fc.days[-1].d.strftime("%A") if fc.days else "about a week out"
        return f"I can only see about a week ahead — want the forecast through {last}?"

    body = _INTENT_FUNCS[intent](fc, tf, now)
    prefix = _loc_prefix(loc, home)
    if prefix:
        body = body[:1].lower() + body[1:]
    stale_note = " (my data's a little old right now)" if fc.stale else ""
    return _cap_sentence(prefix + body + stale_note)


# ── LLM-prompt context ──────────────────────────────────────────────────────


def context_for_prompt(provider: WeatherProvider, transcript: str,
                       now: datetime | None = None) -> str:
    """Compact weather block for the LLM system prompt — only the location and
    window the turn is about, so there's less for a small model to stray over.
    Falls back to a plain 'unavailable' line the caller's claim guard tolerates.
    """
    loc = provider.home
    lq = extract_location_query(transcript)
    if lq:
        resolved, _ = provider.geocode.resolve(lq[0], lq[1])
        if resolved:
            loc = resolved
    fc = provider.get(loc)
    if fc is None:
        return "Current weather: unavailable."

    now = now or _now_local(fc.tz)
    tf = resolve_timeframe(transcript, now)
    where = "" if loc is provider.home else f" in {loc.spoken}"
    c = fc.current
    lines = [(f"Weather{where} now: {c.temp_f}°F (feels {c.feels_f}°F), "
              f"{_wmo_desc(c.code)}, wind {c.wind_mph} mph.")]

    want_days = tf.days or ([tf.day] if tf.day else [])
    if tf.kind in ("current", "hourly") or not want_days:
        want_days = [now.date()]
    for d in want_days[:3]:
        day = fc.day_for(d)
        if day:
            lbl = _day_label(now.date(), d, now)
            lines.append(f"{lbl.capitalize()}: {_wmo_desc(day.code)}, high "
                         f"{day.high_f}°F, low {day.low_f}°F, {day.rain_pct}% rain. "
                         f"Sunrise {day.sunrise}, sunset {day.sunset}.")
    if tf.kind == "hourly":
        hrs = _hours_in(fc, tf)
        if hrs:
            peak = max(hrs, key=lambda h: h.rain_pct)
            lines.append(f"{tf.label.capitalize()}: "
                         f"{min(h.temp_f for h in hrs)}-{max(h.temp_f for h in hrs)}°F, "
                         f"rain peaks {peak.rain_pct}% around {_fmt_hour(peak.dt)}.")
    if fc.yesterday:
        lines.append(f"Yesterday's high was {fc.yesterday.high_f}°F.")
    return " ".join(lines)
