# Skill: Timers, Alarms & Reminders

**Status:** Active
**Pipeline stage:** Deterministic routes (`timer_set`, `alarm_set`, `reminder_set`,
`timer_add`, `alarm_skip`, `timer_cancel`, `alarm_change`, `timer_pause`,
`timer_reset`, `timer_status`, `snooze`), plus a pre-route `_handle_ringing`
("stop" while something rings) — Python
parses the request, builds the spoken reply, and calls TTS directly. The LLM is
never in the loop, so a parse failure can't turn into "I set your timer" when
nothing was set.

---

## What It Does

Parses natural-language timer and alarm requests, tracks each with a background
thread (robust 1s sleep loop — survives NTP jumps and suspend/resume), and fires a
spoken announcement + alarm sound through the satellite when time is up.

Timers, alarms, and reminders are a distinct **`kind`** (`TimerSpec.kind`):

| | Timer | Alarm | Reminder |
|---|---|---|---|
| Set from | a duration ("10 minutes") | a clock time ("7 AM") | either — "in 40 minutes" **or** "at 3 tomorrow" / "tonight" |
| Carries an action phrase | no | no | yes — `TimerSpec.message`, spoken on fire |
| Status reads | time remaining | the clock time / cadence | the action + when |
| `reset` / `add time` / pause | yes | no (meaningless) | add / pause yes |
| Change / skip a day | no | yes | yes |
| Recurring | no | yes ("every weekday", "on weekdays", "Monday through Friday") | yes ("every morning", "every hour") |
| Persisted label | fixed | regenerated on load so "tomorrow" never goes stale | the action phrase (no time in it to go stale) |

State persists across restarts (`data/timers.json`); the old
`[{label, fires_at}]` format migrates automatically.

---

## How Users Trigger It

**Timers** (`parse_timer_request` → `TimerSpec`; a bare number is minutes, "set a timer for 10"):
- "Set a timer for 5 minutes" / "for an hour and a half" / "for two and a half minutes" / "for 1 hour 30 minutes"
- "Set a pasta timer for 10 minutes" / "a 20 minute timer for banana bread"
- "Set a timer named laundry for 45 minutes" / "for 12 minutes for the pasta" / "... and name it pizza"
- "Set a timer" / "start a pasta timer" (no length) → "For how long?" / "How long for the pasta timer?"
- "Countdown 5 minutes" / "give me a 10 minute timer"

**Reminders** (`parse_reminder_request` → `TimerSpec(kind="reminder", message=…)`):
- "Remind me to move the laundry in 40 minutes" / "remind me in 2 hours to check the roast"
- "Remind me to call the dentist at 3 tomorrow" / "remind me about the meeting at 2 PM"
- "Don't let me forget to take the trash out tonight" / "remind me to water the garden tomorrow morning"
- "Set a reminder to call mom at 6 PM"
- **Recurring:** "remind me to take my pills every day at 8 AM" / "remind me to stretch every evening"
- **Intervals:** "remind me to stretch every hour" / "every 30 minutes" / "hourly" (`repeat="every:<s>"`, at least 5 minutes)
- The action phrase is spoken back on fire ("Reminder: move the laundry.") and
  in status ("I'll remind you to move the laundry in 12 minutes.").
- A reminder with **no trigger time** ("remind me to buy milk") isn't one we can
  set — it falls through to the LLM.
- Bare daypart words resolve to defaults: morning 8 AM, afternoon 2 PM,
  evening 7 PM, night/tonight 9 PM, noon 12 PM.

**Clarifying question** — when a reminder names a *day* but no *time* ("remind me
to call the dentist tomorrow", "...on Friday"), Antigua doesn't guess a point
inside the day. She asks **"What time tomorrow would you like to be reminded?"**
and holds the partial parse (`_pending_reminders`, keyed by conversation, 60 s).
The next turn's answer is folded in:
- "3 PM" / "at 9" / "6:30" → clock time
- "in the evening" / "in the morning" → daypart default
- "never mind" / "cancel" → "Okay, no reminder."
- anything else → the pending reminder is dropped and the turn routes normally.

Same mechanism as the memory "who is this for?" prompt — it works within the
15 s wake-word continuation window; there's no separate "expecting a reply" flag.

**Alarms** (`parse_alarm_request` → `TimerSpec`):
- "Wake me up at 7" (am/pm inferred: 5–11 → AM, 1–4 & 12 → PM; "morning"/"evening"/"night" override; "9 every night" is 9 PM)
- "Half past six" / "quarter to seven" / "6 30" / "six oh five" (`normalize_clock`), "noon", "midnight"
- A time already past today ("7 tonight" said at 8 PM) rolls to tomorrow — never rings immediately
- "Set an alarm" / "wake me up" / "set an alarm for tomorrow morning" (no clock time) → "For what time?"
- "Set an alarm for 6:30 tomorrow" / "for 8 on Friday" / "for 8 o'clock"
- "Wake me at seven tomorrow" (word-numbers)
- **Recurring:** "every weekday at 7", "every morning at 6:45", "every Monday and Thursday at 6", "every day at 10 PM", "6 AM on weekdays", "Monday through Friday", "on Saturday and Sunday", "on Mondays"
- "Set an alarm named gym for 6 AM"
- "Wake me up in 5 minutes" → parsed as a *timer* (relative time), not a clock alarm

**Manage:**
- Status: "How much time is left?", "How long on the pasta timer?", "When's my alarm?", "What timers are running?", "Do I have any alarms?", "What are my reminders?", "Is my alarm set?", "What time is my alarm?", "What time did I set my alarm for?", "How much longer?", "Do I have any alarms tomorrow?"
- Add time: "Add 5 minutes to the timer" / "to the pasta timer" / "add five more minutes" / "add a minute"
- Take time off: "Take 2 minutes off the timer" / "subtract a minute from the pasta timer" (negative add; refuses to go past zero)
- Pause / resume: "Pause the timer" / "Pause the pasta timer" / "Resume the timer" / "Start the timer again" (needs the word "timer" — a bare "pause" is the music)
- Change: "Change my alarm to 7:30" (no am/pm → nearest to the old time; keeps its day/cadence) / "Push my alarm back 15 minutes" / "Move my alarm 10 minutes earlier" / "Move tomorrow's alarm to 8" (repeating alarm: skips that day, sets a one-off) / "Change my reminder to 4"
- Skip: "Skip tomorrow's alarm" / "Skip my alarm on Friday" / "Skip the next alarm" / "Turn off my alarm for tomorrow" — a repeating entry sits that day out (`_Entry.skip`); a one-off is cancelled
- Reset: "Reset the timer" / "Restart my pasta timer" (timers only; picks the soonest when unnamed)
- Cancel: "Cancel my pasta timer", "Cancel my 7 AM alarm", "Cancel the dentist reminder", "Cancel my reminder to call mom", "Cancel all timers", "Clear my timers", "Stop everything". "Cancel my alarm" (singular) with several set asks which one.
- Snooze: "Snooze" (5 min) / "Snooze for 10 minutes" / "snooze 10 more minutes" / "snooze 10" — refers to whatever rang in the last 15 minutes; a snoozed timer/reminder rings as itself again. "Give me 5 more minutes" only snoozes if something just rang.
- Stop the ring: "Stop" / "turn it off" / "I'm up" / "okay thanks" while something rings, or within 30 s of the wake word silencing it

---

## Parsing pieces (`server/antigua_core/classify.py`)

| Function | Returns |
|---|---|
| `resolve_duration(text)` | total seconds — sums every "<n> <unit>" term ("1 hour 30 minutes" → 5400) and the fuzzy forms ("half an hour", "an hour and a half", "quarter of an hour") |
| `_replace_word_numbers` | "five" → 5, "twenty five" → 25 (compound), "2 and a half" → 2.5 |
| `_extract_timer_name` | multi-word name from any position ("banana bread timer", "timer for the pasta"); rejects duration phrases |
| `parse_timer_request` | `TimerSpec(kind="timer")` or None |
| `parse_alarm_request` | `TimerSpec(kind="alarm")` or None — thin wrapper over `_resolve_clock_time` |
| `parse_reminder_request` | `TimerSpec(kind="reminder", message=…)` or None — relative or clock trigger; `None` when neither is present |
| `_resolve_clock_time(src, now, allow_daypart=…)` | `(target, hour, minute, repeat)` or None — am/pm inference, day words (also caught when they don't follow the time), `_parse_repeat`, daypart defaults. Shared by alarm + reminder parsing |
| `_extract_reminder_message` | the action phrase, with the framing and the leading/trailing time clause stripped; `None` if nothing's left |
| `reminder_missing_time` | `(message, day_phrase)` when the request names a day but no time — the pipeline asks; `None` otherwise |
| `parse_add_time_request` | `(seconds, label_substring)` or None; negative for "take 2 minutes off" |
| `normalize_clock` | "half past 6" → "6:30", "quarter to 7" → "6:45", "6 30" → "6:30" |
| `alarm_missing_time` / `timer_missing_length` | the pipeline asks "For what time?" / "For how long?" (`_pending_set`) |
| `parse_alarm_change` / `resolve_new_time` | `(ref, kind, new_time_text, shift_s)`; the new fire time |
| `parse_skip_request` / `parse_pause_request` / `is_dismiss_request` | skip a day / pause-resume / "stop" while ringing |
| `parse_timer_cancel_request` | `("all", None)` / `("label", substring)` / None |
| `parse_timer_reset_request` / `parse_timer_status_request` / `parse_snooze_request` | as before, widened |

`resolve_day_offset()` (weekday/tomorrow parsing) is shared with the weather
skill's timeframe resolver.

---

## State & firing (`server/antigua_core/stores.py`)

`TimerManager` holds typed `_Entry` records.

| Method | Notes |
|---|---|
| `set(TimerSpec)` | arms a thread; publishes `antigua/timer_set` |
| `list_active()` | rows with `kind`, `name`, `repeat`, `fires_at`, `remaining_s`, sorted by fire time |
| `find(substring)` | label- or name-match, for specific status/cancel |
| `add_time(sub, secs)` / `add_time_id` | extends (or, negative, shortens) a timer; a paused one too |
| `pause(id)` / `resume(id)` | freezes `paused_left`; the thread idles; persisted |
| `skip(id, date)` | a repeating entry sits out that date |
| `reschedule(id, fires_at, hour, minute, repeat)` | change an alarm/reminder in place |
| `reset(sub)` | timers only; re-arms from `duration_s` |
| `cancel(sub)` / `cancel_all()` | substring or all |
| `_load()` | migrates legacy format; rings anything under 10 min late (missed during a restart); rolls a lapsed recurring alarm forward however many days it lapsed; refreshes alarm labels |

On fire: `_on_fire(label, kind, message)` (backend hook), then if `repeat` the
entry re-arms itself at the next matching day (`_next_occurrence`). A recurring
reminder keeps its message label on re-arm; only alarms get their label
regenerated.

`on_fire` is `_on_timer_fire(label, kind, message)` in each server — picks the
spoken line ("Your pasta timer is done." / "Good morning, your alarm is going
off." / "Reminder: move the laundry.") and publishes `antigua/alarm` with
`kind`.

---

## Spoken replies (`server/antigua_core/pipeline.py`)

All deterministic:

| | Example |
|---|---|
| `format_set_reply` | "Pasta timer set for 10 minutes." / "Alarm set for 7 AM tomorrow." / "Okay, I'll remind you to move the laundry in 40 minutes." / "Okay, I'll remind you to call the dentist at 3 PM tomorrow." |
| `format_timer_status_reply` | "Your pasta timer has 8 minutes left." / "I'll remind you to call the dentist at 3 PM tomorrow." / "You've got your pasta timer at 8 minutes, and a reminder to call mom at 6 PM." |
| cancel | "Cancelled the pasta timer." / "Cancelled the reminder to call mom." / "Cancelled all 3." / "I couldn't find a tea timer, alarm, or reminder." |
| add / reset / snooze | "Added 5 minutes. The pasta timer now has 13 minutes left." / "Restarted the timer." / "Snoozing for 10 minutes." |

`_round_remaining()` rounds a running timer up to the whole minute above ~90s so
status never says "14 minutes 59 seconds".

---

## Satellite ring (`antigua_satellite.py`)

`_alarm_loop` speaks the announcement and rings `audio_in/alarm_clock.ogg` for
`alarm_ring_seconds` (default **5s**), then rings again every
`alarm_repeat_seconds` (30s) until stopped, for at most `alarm_max_seconds`
(300s). A reminder plays a soft two-tone chime before its line instead of the
bell. Every kind repeats.

The bell stays short because the mic sits next to the speaker: the wake word is
heard in the quiet between rings. Any of these stops all ringing:
- the wake word (`antigua/listening` active — the bridge publishes it at every recording start)
- `antigua/alarm_stop` — the server's reply to "stop" / "I'm up" / a snooze
- `antigua/stop` — the wake word over a reply

When a ring ends the satellite publishes `antigua/alarm_ack {label, id}`; the
servers track what is ringing (`pipeline._ringing`) so "stop" means the alarm,
not the music, while it rings and for 30 s after.

---

## Config (`server/config/server.yaml` — none; `config/satellite.yaml`)

```yaml
satellite:
  alarm_ring_seconds: 5      # bell per ring
  alarm_repeat_seconds: 30   # rings again this often...
  alarm_max_seconds: 300     # ...until stopped, at most this long
```

Persistence path: `settings.TIMER_STORE_PATH` → `data/timers.json`.

---

## MQTT Events

| Topic | Direction | Payload | When |
|---|---|---|---|
| `antigua/timer_set` | server → Pi | `{id, label, kind, fires_at}` | set / reset |
| `antigua/alarm` | server → Pi | `{text, audio_url, label, kind, id}` | fires |
| `antigua/alarm_stop` | server → Pi | `{}` | "stop" / snooze while ringing |
| `antigua/alarm_ack` | Pi → servers | `{label, id}` | ring ends |

PinedaDisplay subscribes to the same topics to drive its timer card (paused
timers drop off it until resumed).

---

## Tests

- `tests/test_timers.py` — snapshot suite: duration parsing, alarm + reminder
  specs (relative / clock / daypart / recurring), message extraction, phrasing,
  `TimerManager` (type split, `find`, `add_time`, `reset`, message round-trip,
  fire-callback payload, legacy-format migration, recurring roll-forward), and
  the 2026-10 polish: spoken clocks, repeats without "every", intervals,
  take-off/bare add, snooze forms, ask-for-time, change/skip/pause parsing,
  "stop" detection, restart rings, pause/resume, skip, reschedule.
- `tests/test_pipeline.py` — end-to-end: set / cancel / add / status / recurring,
  a reminder set → status → cancel flow, ask-for-time, take-off / pause,
  "cancel my alarm" asks, skip / change / move, and "stop" / snooze while ringing.
- `tests/fixtures/routing.yaml` — `timer_*` / `alarm_*` / `reminder_set` fixtures.

---

## Limitations

- Snooze targets the most recently fired entry (within 15 min); with several
  firing together it may pick the wrong one.
- Reminder message extraction is heuristic. A phrasing it can't split cleanly
  yields `message=None` (falls back to "Here's your reminder.") — no worse than
  before, never a wrong action.
- A time already past rolls silently to tomorrow, and the reply says so ("7 PM tomorrow").
- Ordinal / calendar dates ("on the first", "on the 15th", "March 3") don't
  parse — only relative days, weekday names, and dayparts.
- "Remind me to buy milk" (no time at all) still goes to the LLM rather than asking "when?".
- "On Saturday and Sunday" makes a repeating alarm (said back as "every weekend").
- "Set two timers, one for 5 and one for 10" sets only the first.
- A missed alarm rings on restart only if under 10 minutes late. If the fallback
  already rang it during a short outage, it can ring twice.
- Timer threads are daemons — a graceful shutdown doesn't wait on them.
- New replies (pause, change, skip, "Okay." to stop) are English-only; the
  Spanish reply table doesn't cover them yet.

---

## How to Extend

**Per-alarm ring length / sound:** add fields to `TimerSpec`, pass through
`antigua/alarm`, look up in `_alarm_loop` (the reminder chime is the model).
**Calendar dates:** extend `_resolve_clock_time`'s day-word branch.
