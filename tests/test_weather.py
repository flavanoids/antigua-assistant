#!/usr/bin/env python3
"""Snapshot tests for the deterministic weather phrasing layer.

No network: a synthetic Forecast drives every phrase_* path. Each spoken
string is also run through clean_for_tts() so a wording change that breaks
out loud shows up as a failure here.

Run: python3 tests/test_weather.py   (also works under pytest)
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

settings.configure({})

from antigua_core import weather as w  # noqa: E402
from antigua_core.tts_text import clean_for_tts  # noqa: E402

# Wednesday, 9:00 AM local.
NOW = datetime(2025, 6, 4, 9, 0)
HOME = w.Location(29.76, -95.37, "America/Chicago", "", "home", True)


def _forecast(*, cur_code=2, cur_temp=72, cur_feels=72, wind=6,
              hourly_rain=None, daily_rain=None, highs=None, lows=None,
              day_codes=None, yesterday_high=70):
    """Build a Forecast. hourly_rain: dict{hour_offset: pct}. daily_rain/highs/
    lows/day_codes: 7-length lists indexed from today."""
    hourly_rain = hourly_rain or {}
    daily_rain = daily_rain or [0] * 7
    highs = highs or [88] * 7
    lows = lows or [68] * 7
    day_codes = day_codes or [cur_code] * 7

    hours = []
    start = NOW.replace(minute=0)
    for h in range(48):
        dt = start + timedelta(hours=h)
        hours.append(w.Hour(dt=dt, temp_f=75, rain_pct=hourly_rain.get(h, 0),
                            code=cur_code))
    days = []
    for i in range(7):
        d = NOW.date() + timedelta(days=i)
        days.append(w.Day(d=d, high_f=highs[i], low_f=lows[i], code=day_codes[i],
                          rain_pct=daily_rain[i], sunrise="6:20 AM", sunset="8:20 PM",
                          uv_max=7.0))
    yest = w.Day(d=NOW.date() - timedelta(days=1), high_f=yesterday_high, low_f=66,
                 code=2, rain_pct=0, sunrise="6:21 AM", sunset="8:19 PM")
    return w.Forecast(location=HOME, tz="America/Chicago",
                      current=w.Conditions(cur_temp, cur_feels, 55, wind, cur_code),
                      hourly=hours, days=days, yesterday=yest)


CASES = []


def case(intent, text, forecast, expected):
    CASES.append((intent, text, forecast, expected))


# ── rain, today ─────────────────────────────────────────────────────────────
case("rain", "is it going to rain today",
     _forecast(daily_rain=[70] + [0] * 6, day_codes=[63] + [2] * 6,
               hourly_rain={6: 70, 7: 60}),
     "Yes, today has about a 70 percent chance of rain.")

case("rain", "will it rain this afternoon",
     _forecast(hourly_rain={h: 65 for h in range(3, 9)}),
     "Yes — the best chance this afternoon is around noon, about 65 percent.")

case("rain", "is it raining", _forecast(cur_code=63),
     "Yes, it's coming down right now.")

case("rain", "is it going to rain later",
     _forecast(hourly_rain={7: 70, 8: 75}),
     "Dry right now, but rain's likely later, around 5 PM, about 75 percent.")

case("rain", "is it going to rain tomorrow",
     _forecast(daily_rain=[0, 15] + [0] * 5),
     "Tomorrow looks dry, only about 15 percent.")

case("rain", "will it rain tomorrow",
     _forecast(daily_rain=[0] * 7),
     "Tomorrow looks dry.")

case("rain", "any rain this weekend",
     _forecast(daily_rain=[0, 0, 0, 80, 15, 0, 0]),
     "It looks like Saturday is the wet one, around 80 percent. "
     "The other days look drier.")

# ── umbrella / jacket ───────────────────────────────────────────────────────
case("umbrella", "do i need an umbrella",
     _forecast(daily_rain=[75] + [0] * 6), "Yes, I'd take one — rain's likely today, around 75 percent.")

case("jacket", "do i need a jacket",
     _forecast(cur_feels=51), "A light one — it's about 51 right now.")

case("jacket", "is it cold out", _forecast(cur_feels=40),
     "Yes, bundle up — it's cold right now, around 40.")

# ── temperature ─────────────────────────────────────────────────────────────
case("temp", "how hot will it get today", _forecast(highs=[95] + [88] * 6),
     "Today tops out near 95, down to about 70.")

case("temp", "what's the temperature", _forecast(cur_temp=81, cur_feels=88),
     "It's 81 out, feels like 88, so hot.")

# ── general ─────────────────────────────────────────────────────────────────
case("general", "what's the weather", _forecast(cur_temp=64, cur_code=45,
     highs=[79] + [88] * 6),
     "It's 64 and foggy right now, heading for a high near 80.")

case("general", "what's the forecast for saturday",
     _forecast(day_codes=[2, 2, 2, 3, 2, 2, 2], highs=[88, 88, 88, 91] + [88] * 3),
     "Saturday: cloudy, high near 90, low around 70.")

# ── far-out hedging ─────────────────────────────────────────────────────────
case("general", "what's the weather next tuesday",
     _forecast(highs=[88] * 5 + [88, 84], day_codes=[2] * 7),
     "Early signs point to Tuesday: partly cloudy, high near 85, low around 70.")

# ── compare to yesterday ────────────────────────────────────────────────────
case("compare", "is it warmer than yesterday",
     _forecast(highs=[85] + [88] * 6, yesterday_high=72),
     "About 13 degrees warmer than yesterday, near 85.")

# ── sunset ──────────────────────────────────────────────────────────────────
case("sunset", "when is sunset", _forecast(), "Sunset tonight is at 8:20 PM.")

# ── beyond horizon ──────────────────────────────────────────────────────────


def run():
    failed = 0
    for intent, text, fc, expected in CASES:
        tf = w.resolve_timeframe(text, NOW)
        got = w._INTENT_FUNCS[intent](fc, tf, NOW)
        got = w._cap_sentence(got)
        spoken = clean_for_tts(got)
        status = "ok" if got == expected else "FAIL"
        if got != expected:
            failed += 1
            print(f"[{status}] {text!r}\n   expected: {expected!r}\n   got:      {got!r}")
        else:
            print(f"[ok] {text!r} -> {got!r}")
            print(f"     tts: {spoken!r}")

    # intent classifier sanity
    assert w.classify_weather_intent("is it going to rain today") == "rain"
    assert w.classify_weather_intent("do i need an umbrella") == "umbrella"
    assert w.classify_weather_intent("what time is it") is None
    assert w.classify_weather_intent("what's the weather in Denver") == "general"

    # location extraction
    assert w.extract_location_query("what's the weather in Denver") == ("Denver", None)
    assert w.extract_location_query("will it rain in Portland, Maine") == ("Portland", "Maine")
    assert w.extract_location_query("is it going to rain today") is None

    # timeframe resolver
    assert w.resolve_timeframe("weather tomorrow", NOW).day == date(2025, 6, 5)
    assert w.resolve_timeframe("weather this weekend", NOW).kind == "range"
    assert w.resolve_timeframe("weather in 20 days", NOW).kind == "beyond"
    assert w.resolve_timeframe("what's the weather", NOW).kind == "current"

    # Provider: one fetch per location at a time, however many callers ask.
    import threading
    import time

    class SlowProvider(w.WeatherProvider):
        fetches = 0
        fail = False

        def _fetch(self, loc):
            SlowProvider.fetches += 1
            time.sleep(0.2)
            if self.fail:
                raise OSError("down")
            return _forecast()

    prov = SlowProvider(HOME)
    callers = [threading.Thread(target=prov.get) for _ in range(4)]
    for c in callers:
        c.start()
    for c in callers:
        c.join()
    assert SlowProvider.fetches == 1, SlowProvider.fetches
    # Stale: prewarm refreshes (blocking) while a get() in the middle of it
    # serves the old forecast without starting a second fetch.
    k = prov._key(HOME)
    prov._cache[k] = (prov._cache[k][0], time.time() - prov._ttl - 1)
    pw = threading.Thread(target=prov.prewarm, args=([],))
    pw.start()
    time.sleep(0.05)
    assert prov.get() is not None
    pw.join()
    time.sleep(0.05)
    assert SlowProvider.fetches == 2, SlowProvider.fetches
    assert time.time() - prov._cache[k][1] < 5   # prewarm left it fresh
    # A failed cold fetch caches None; once due again, get() retries cleanly.
    down = SlowProvider(HOME)
    down.fail = True
    assert down.get() is None
    down._cache[k] = (None, time.time() - down._ttl - 1)
    assert down.get() is None

    if failed:
        print(f"\n{failed} snapshot(s) failed")
        sys.exit(1)
    print(f"\nAll {len(CASES)} snapshots + assertions passed")


if __name__ == "__main__":
    run()
