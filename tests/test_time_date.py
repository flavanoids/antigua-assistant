#!/usr/bin/env python3
"""Snapshot tests for the time & date skill (antigua_core/time_date.py).

No network: the world clock gets a stub geocoder. The clock is pinned to
Thursday, October 15, 2026, 1:56 PM Central unless a case says otherwise.
Every reply is also pushed through clean_for_tts() and must come out with no
surviving digit.

Run: python3 tests/test_time_date.py   (also works under pytest)
"""

import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

settings.configure({})
settings.WEATHER_HOME_TZ = "America/Chicago"

from antigua_core import time_date  # noqa: E402
from antigua_core.classify import classify  # noqa: E402
from antigua_core.tts_text import clean_for_tts  # noqa: E402
from antigua_core.weather import Location  # noqa: E402

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 15, 13, 56, tzinfo=CT)


class _Geo:
    _PLACES = {
        "tokyo": Location(35.68, 139.69, "Asia/Tokyo", "Tokyo", "city", False),
        "london": Location(51.5, -0.12, "Europe/London", "London", "city", False),
        "dallas": Location(32.8, -96.8, "America/Chicago", "Dallas", "city", True),
        "mumbai": Location(19.1, 72.9, "Asia/Kolkata", "Mumbai", "city", False),
    }

    def resolve(self, place, admin_hint=None):
        return self._PLACES.get(place.lower()), None


CASES = [
    # now
    ("what time is it", "It's 1:56 PM."),
    ("what's the time", "It's 1:56 PM."),
    ("tell me the time", "It's 1:56 PM."),
    ("do you know what time it is", "It's 1:56 PM."),
    ("can you tell me the time please", "It's 1:56 PM."),
    ("what time's it", "It's 1:56 PM."),
    ("the time", "It's 1:56 PM."),
    ("what's the date", "Today is Thursday, October 15."),
    ("what's today", "Today is Thursday, October 15."),
    ("what day is it", "Today is Thursday, October 15."),
    ("what day of the week is it", "Today is Thursday, October 15."),
    ("what's the time and date", "It's 1:56 PM on Thursday, October 15."),
    ("what year is it", "It's 2026."),
    ("what month is it", "It's October."),
    ("what week of the year is it", "It's week 42 of 2026."),
    ("what day of the year is it", "Today is day 288 of 365."),
    ("how many days are left in the year", "There are 77 days left in the year, not counting today."),
    ("how many weeks left in the year", "There are 11 weeks left in the year, not counting today."),
    ("how many days left in the month", "There are 16 days left in the month, not counting today."),
    # other days
    ("what's the date tomorrow", "Tomorrow is Friday, October 16."),
    ("what's tomorrow's date", "Tomorrow is Friday, October 16."),
    ("what day is tomorrow", "Tomorrow is Friday, October 16."),
    ("what was yesterday's date", "Yesterday was Wednesday, October 14."),
    ("what day was yesterday", "Yesterday was Wednesday, October 14."),
    ("what's the date in two weeks", "2 weeks from now is Thursday, October 29."),
    ("what date is three weeks from today", "3 weeks from today is Thursday, November 5."),
    ("what's the date a week from today", "A week from today is Thursday, October 22."),
    ("what date is 90 days from now", "90 days from now is Wednesday, January 13, 2027."),
    ("what was the date 2 weeks ago", "2 weeks ago was Thursday, October 1."),
    ("what's the date next Friday", "Friday is October 16."),
    ("what was the date last Monday", "Last Monday was October 12."),
    # weekday of
    ("what day of the week is November 3rd", "November 3 is a Tuesday."),
    ("what day is the 31st", "October 31 is a Saturday."),
    ("what day is the fourth of July next year", "The Fourth of July is Sunday, July 4, 2027."),
    ("what day is Christmas on this year", "Christmas is Friday, December 25, 10 weeks and 1 day away."),
    ("what day does Thanksgiving fall on", "Thanksgiving is Thursday, November 26, 42 days from now."),
    ("what day is Christmas 2027", "Christmas is Saturday, December 25, 2027."),
    # when is
    ("when is Thanksgiving", "Thanksgiving is Thursday, November 26, 42 days from now."),
    ("when is Easter", "Easter is Sunday, March 28, 2027."),
    ("when is Mother's Day", "Mother's Day is Sunday, May 9, 2027."),
    ("when is Halloween", "Halloween is Saturday, October 31, 16 days from now."),
    ("when is Labor Day", "Labor Day is Monday, September 6, 2027."),
    ("when is Christmas next year", "Christmas is Saturday, December 25, 2027."),
    # countdowns
    ("how many days until Christmas", "Christmas is 71 days away, on Friday, December 25."),
    ("how long until Christmas", "Christmas is 71 days away, on Friday, December 25."),
    ("how many weeks until Christmas", "Christmas is 10 weeks and 1 day away, on Friday, December 25."),
    ("how many sleeps until Christmas", "71 more sleeps until Christmas."),
    ("how many days till Halloween", "Halloween is 16 days away, on Saturday, October 31."),
    ("how many days until December 25th", "December 25 is 71 days away."),
    ("how many days until new year's", "New Year's Day is 78 days away, on Friday, January 1, 2027."),
    ("how many days until Friday", "Friday is tomorrow."),
    ("how many days until Monday", "Monday is 4 days away."),
    ("how many days until the weekend", "The weekend starts in 2 days, on Saturday."),
    ("how many days until the 1st", "November 1 is 17 days away."),
    # since
    ("how many days since the fourth of July", "The Fourth of July was 103 days ago."),
    ("how many days ago was September 1st", "September 1 was 44 days ago."),
    ("how long ago was Easter", "Easter was 193 days ago."),
    ("how many weeks since Labor Day", "Labor Day was 5 weeks and 3 days ago."),
    # clock gaps
    ("how long until 5 pm", "3 hours and 4 minutes until 5 PM."),
    ("how many hours until 5", "3 hours and 4 minutes until 5 PM."),
    ("how long until midnight", "10 hours and 4 minutes until midnight."),
    ("how long until 2:30", "34 minutes until 2:30 PM."),
    # yes / no
    ("is it Friday", "No, it's Thursday."),
    ("is today Thursday", "Yes, today is Thursday."),
    ("is tomorrow Saturday", "No, tomorrow is Friday."),
    ("is it the weekend", "Not yet, it's Thursday. The weekend starts in 2 days."),
    ("is today the 15th", "Yes, today is Thursday, October 15."),
    ("is it October", "Yes, it's October."),
    ("is it Halloween", "No, Halloween is 16 days away, on Saturday, October 31."),
    # holidays
    ("is it a holiday today", "No holiday today. The next one is Halloween, in 16 days."),
    ("is Monday a holiday", "No, Monday, October 19 is not a holiday."),
    ("what's the next holiday", "The next holiday is Halloween, on Saturday, October 31. That's 16 days away."),
    # time zone and clocks
    ("what time zone are we in", "We're on Central Daylight Time."),
    ("when do the clocks change",
     "Daylight saving time ends on Sunday, November 1, in 17 days. At 2 AM the clocks go back an hour."),
    ("when do we fall back",
     "Daylight saving time ends on Sunday, November 1, in 17 days. At 2 AM the clocks go back an hour."),
    # world clock
    ("what time is it in Tokyo", "It's 3:56 AM Friday in Tokyo, 14 hours ahead of us."),
    ("what's the time in London", "It's 7:56 PM in London, 6 hours ahead of us."),
    ("what time is it in Dallas", "It's 1:56 PM in Dallas, same as here."),
    ("what time is it in Mumbai", "It's 12:26 AM Friday in Mumbai, 10.5 hours ahead of us."),
    ("what time is it in Atlantis", "I couldn't find Atlantis."),
]

# (now, question, expected) — cases that need a different clock.
DATED = [
    (datetime(2026, 9, 13, 12, 0, tzinfo=CT), "what time is it", "It's noon."),
    (datetime(2026, 9, 13, 0, 0, tzinfo=CT), "what time is it", "It's midnight."),
    (datetime(2026, 9, 13, 12, 30, tzinfo=CT), "what time is it", "It's 12:30 PM."),
    (datetime(2026, 10, 17, 10, 0, tzinfo=CT), "is it the weekend", "Yes, it's Saturday."),
    (datetime(2026, 10, 17, 10, 0, tzinfo=CT), "how many days until the weekend", "It's the weekend now!"),
    (datetime(2026, 11, 26, 9, 0, tzinfo=CT), "is it a holiday today", "Yes, today is Thanksgiving."),
    (datetime(2026, 12, 25, 9, 0, tzinfo=CT), "how many days until Christmas", "Christmas is today!"),
    (datetime(2026, 12, 31, 9, 0, tzinfo=CT), "how many days left in the year", "Today is the last day of the year."),
    (datetime(2027, 2, 20, 9, 0, tzinfo=CT), "when does daylight saving time start",
     "Daylight saving time starts on Sunday, March 14, in 22 days. At 2 AM the clocks go forward an hour."),
    (datetime(2028, 2, 29, 9, 0, tzinfo=CT), "what day of the year is it", "Today is day 60 of 366."),
]

# Questions that belong to another skill — the time skill must not claim them.
NOT_MINE = [
    ("how long until my timer is done", "timer_status"),
    ("what time is my alarm", "timer_status"),
    ("what time is sunset", "weather"),
    ("when does it get dark", "weather"),
    ("what's the weather today", "weather"),
    ("what time does Target close", None),
    ("when is the next Rockets game", None),
    ("when is my birthday", None),
    ("set an alarm for 7 tomorrow", "alarm_set"),
    ("remind me on Friday to call mom", "reminder_set"),
]


def _spoken_ok(reply):
    spoken = clean_for_tts(reply)
    return not re.search(r"\d", spoken), spoken


def run():
    failed = 0
    geo = _Geo()
    for text, expected in CASES:
        got = time_date.answer(text, now=NOW, geocode=geo)
        ok, spoken = _spoken_ok(got or "")
        if got != expected:
            failed += 1
            print(f"[FAIL] {text!r}\n   got:      {got!r}\n   expected: {expected!r}")
        elif not ok:
            failed += 1
            print(f"[FAIL] {text!r} speaks a digit: {spoken!r}")
        else:
            print(f"[ok] {text!r} -> {got}")
        if classify(text) != "time_date":
            failed += 1
            print(f"[FAIL] {text!r} routed {classify(text)!r}, expected 'time_date'")

    for now, text, expected in DATED:
        got = time_date.answer(text, now=now, geocode=geo)
        if got != expected:
            failed += 1
            print(f"[FAIL] {now:%Y-%m-%d %H:%M} {text!r}\n   got:      {got!r}\n   expected: {expected!r}")
        else:
            print(f"[ok] {now:%Y-%m-%d %H:%M} {text!r} -> {got}")

    for text, route in NOT_MINE:
        r = classify(text)
        if time_date.parse(text, NOW) is not None or r == "time_date" or (route and r != route):
            failed += 1
            print(f"[FAIL] {text!r} routed {r!r}, expected {route or 'not time_date'!r}")
        else:
            print(f"[ok] not mine ({r}): {text!r}")

    # No geocoder (the fallback server): a world-clock question falls through.
    if time_date.answer("what time is it in Tokyo", now=NOW) is not None:
        failed += 1
        print("[FAIL] world clock without a geocoder should decline")

    if failed:
        print(f"\n{failed} check(s) failed")
        sys.exit(1)
    print(f"\nAll {len(CASES)} snapshots + {len(DATED)} dated + {len(NOT_MINE)} routing checks passed")


if __name__ == "__main__":
    run()
