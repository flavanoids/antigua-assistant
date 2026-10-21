# Skill: Time & Date

**Status:** Active  
**Pipeline stage:** LLM bypass — Python answers, the LLM is never called

---

## What It Does

Answers clock and calendar questions deterministically in the home timezone
(`weather.home_tz` in server.yaml → `settings.WEATHER_HOME_TZ`), not the
server's system clock. The 4b model is bad at date arithmetic; nothing here
reaches it.

| Kind | Examples | Reply |
|---|---|---|
| Now | "what time is it", "tell me the time", "what's today", "what year is it", "what week of the year is it", "how many days left in the year" | "It's 1:56 PM." / "Today is Thursday, October 15." |
| Other days | "what's tomorrow's date", "what day was yesterday", "what's the date in two weeks", "what date is 90 days from now" | "2 weeks from now is Thursday, October 29." |
| Weekday of | "what day is Christmas", "what day of the week is November 3rd", "what day is the 31st" | "November 3 is a Tuesday." |
| When is | "when is Thanksgiving / Easter / Mother's Day" | "Thanksgiving is Thursday, November 26, 42 days from now." |
| Countdowns | "how many days until Christmas", "how many weeks till New Year's", "how many sleeps until Christmas", "how many days until the weekend" | "Christmas is 71 days away, on Friday, December 25." |
| Since | "how many days since the Fourth of July", "how long ago was Easter" | "The Fourth of July was 103 days ago." |
| Clock gap | "how long until 5 pm", "how many hours until midnight" | "3 hours and 4 minutes until 5 PM." |
| Yes / no | "is it Friday", "is today the 15th", "is it the weekend", "is it Halloween" | "No, it's Thursday." |
| Holidays | "is today a holiday", "is Monday a holiday", "what's the next holiday" | "No holiday today. The next one is Halloween, in 16 days." |
| World clock | "what time is it in Tokyo" | "It's 3:56 AM Friday in Tokyo, 14 hours ahead of us." |
| Time zone | "what time zone are we in" | "We're on Central Daylight Time." |
| Clocks | "when do the clocks change", "when do we fall back" | "Daylight saving time ends on Sunday, November 1, in 17 days. At 2 AM the clocks go back an hour." |

**Not ours** (the parser declines anything mentioning these): timers, alarms,
reminders, weather, sunrise/sunset (weather skill), games/shows (search),
store hours, trash pickup, "my birthday" (memory).

---

## Code Location

| What | Where |
|---|---|
| Parser + replies | `server/antigua_core/time_date.py` — `parse()` (pure, network-free), `answer()` |
| Holiday table | `_HOLIDAYS` in the same file (US federal + common observances; Easter computed) |
| Route guard | `is_time_date_query()` in `intents/time_date.py`, called from `classify()` |
| Handler | `_handle_time_date` in `pipeline.py` |
| Tests | `tests/test_time_date.py` (pinned clock, stub geocoder, TTS digit check) |

`answer()` returns None to fall through to the LLM — only when a world-clock
question arrives without a geocoder (the fallback server).

World-clock cities go through the weather skill's `GeocodeCache`
(`data/geocode_cache.json`), so a city is looked up on Open-Meteo once and
cached after.

---

## How to Extend

- **New holiday:** add a `(spoken name, regex, year -> date)` row to `_HOLIDAYS`.
- **New phrasing:** add the case to `tests/test_time_date.py` first; most
  phrasings belong in one of the `_*_RE` patterns near `parse()`.
- **Spanish:** `spanish.py` maps "qué hora es" / "qué día es hoy" only;
  countdowns and world clock in Spanish still go to the LLM.
