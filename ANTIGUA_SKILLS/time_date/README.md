# Skill: Time & Date

**Status:** Active  
**Pipeline stage:** LLM bypass — Python formats the answer directly, LLM never called

---

## What It Does

Intercepts pure time and date queries and answers them immediately using `datetime.now()`, skipping the LLM entirely. This eliminates ~1-2s of LLM latency for the most common factual queries.

---

## How Users Trigger It

- "What time is it?"
- "What's the time?"
- "What time is it right now?"
- "What day is it?"
- "What's today's date?"
- "What is the date today?"
- "What day of the week is it?"
- "What year is it?"
- "Which is today?" (STT variant)

**Does NOT fire on:**
- "What's the weather today?" (weather skill handles)
- "How much time is left on my timer?" (timer skill handles)
- "What time does the sun set?" (goes to LLM — requires reasoning)

---

## Detection

```python
format_time_date_response(transcript)  # returns answer string or None
```

Internally uses two regexes:

- `_TIME_QUERY_RE` — matches time-of-day requests ("what time is it", "current time", etc.)
- `_DATE_QUERY_RE` — matches date/day requests ("what day is it", "today's date", "which is today", etc.)

Both in `server/antigua_server.py` (~line 788).

The function returns:
- `"It's 3:47 PM."` — time only
- `"Today is Wednesday, April 22."` — date only
- `"It's 3:47 PM on Wednesday, April 22."` — both matched

---

## Response

Python-generated, no LLM. Formatted with `datetime.now()`:

```python
time_str = now.strftime("%-I:%M %p")   # e.g. "3:47 PM"
date_str = now.strftime("%A, %B %-d")  # e.g. "Wednesday, April 22"
```

Goes straight to `synthesize()` (TTS) and returns. Total pipeline: STT + TTS only.

---

## Code Location

| What | Where |
|---|---|
| Detection regexes | `_TIME_QUERY_RE`, `_DATE_QUERY_RE` (~line 788, `server/antigua_server.py`) |
| Response formatter | `format_time_date_response(transcript)` (~line 805) |
| Pipeline hook | `run_pipeline()` — "Time/date shortcut" block, after volume check |

---

## Config Knobs

None. Time is read from the server's system clock (`datetime.now()`).

If the server's timezone is wrong, the reported time will be wrong. Set the system timezone on the primary:

```bash
sudo timedatectl set-timezone America/Chicago
```

---

## MQTT Events

None specific to this skill.

---

## Limitations

- Does not add weather context (unlike the LLM path, which always appended weather). If the user asks "what time is it and what's the weather?", this skill fires and the weather half is dropped.
- Year is not included in date responses by default to keep the answer short. "What year is it?" returns the full date string (e.g., "Today is Wednesday, April 22.") — the user must infer the year.
- The date format (`%B %-d`) omits the year. If you want it: change `"%A, %B %-d"` to `"%A, %B %-d, %Y"`.

---

## How to Extend

**Include the year:** Change `now.strftime("%A, %B %-d")` to `now.strftime("%A, %B %-d, %Y")` in `format_time_date_response()`.

**Add weather to the bypass response:** Call `weather_cache.format_for_prompt()` and append a one-liner if the transcript includes weather keywords. Keep the response short — TTS cost scales with length.

**Add a time zone query:** Detect "what time is it in Tokyo?" and use `pytz` to convert. If matched, return the bypass response with the foreign time; otherwise fall through to LLM.
