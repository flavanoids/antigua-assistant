# Skill: Timers, Alarms & Reminders

**Status:** Active
**Pipeline stage:** Deterministic routes (`timer_set`, `alarm_set`, `reminder_set`,
`timer_cancel`, `timer_add`, `timer_reset`, `timer_status`, `snooze`) — Python
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
| `reset` / `add time` | yes | no (meaningless) | no |
| Recurring | no | yes ("every weekday") | yes ("every morning") |
| Persisted label | fixed | regenerated on load so "tomorrow" never goes stale | the action phrase (no time in it to go stale) |

State persists across restarts (`data/timers.json`); the old
`[{label, fires_at}]` format migrates automatically.

---

## How Users Trigger It

**Timers** (`parse_timer_request` → `TimerSpec`):
- "Set a timer for 5 minutes" / "for an hour and a half" / "for two and a half minutes" / "for 1 hour 30 minutes"
- "Set a pasta timer for 10 minutes" / "a 20 minute timer for banana bread"
- "Set a timer named laundry for 45 minutes"
- "Countdown 5 minutes" / "give me a 10 minute timer"

**Reminders** (`parse_reminder_request` → `TimerSpec(kind="reminder", message=…)`):
- "Remind me to move the laundry in 40 minutes" / "remind me in 2 hours to check the roast"
- "Remind me to call the dentist at 3 tomorrow" / "remind me about the meeting at 2 PM"
- "Don't let me forget to take the trash out tonight" / "remind me to water the garden tomorrow morning"
- "Set a reminder to call mom at 6 PM"
- **Recurring:** "remind me to take my pills every day at 8 AM" / "remind me to stretch every evening"
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
- "Wake me up at 7" (am/pm inferred: 5–11 → AM, 1–4 & 12 → PM; "morning"/"evening" override)
- "Set an alarm for 6:30 tomorrow" / "for 8 on Friday" / "for 8 o'clock"
- "Wake me at seven tomorrow" (word-numbers)
- **Recurring:** "every weekday at 7", "every morning at 6:45", "every Monday and Thursday at 6", "every day at 10 PM"
- "Set an alarm named gym for 6 AM"
- "Wake me up in 5 minutes" → parsed as a *timer* (relative time), not a clock alarm

**Manage:**
- Status: "How much time is left?", "How long on the pasta timer?", "When's my alarm?", "What timers are running?", "Do I have any alarms?", "What are my reminders?"
- Add time: "Add 5 minutes to the timer" / "to the pasta timer"
- Reset: "Reset the timer" / "Restart my pasta timer" (timers only; picks the soonest when unnamed)
- Cancel: "Cancel my pasta timer", "Cancel my 7 AM alarm", "Cancel the dentist reminder", "Cancel all timers", "Stop everything"
- Snooze: "Snooze" (5 min) / "Snooze for 10 minutes" — a bare "snooze" only refers to something that rang in the last 15 minutes

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
| `parse_add_time_request` | `(seconds, label_substring)` or None |
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
| `add_time(sub, secs)` | extends a running timer |
| `reset(sub)` | timers only; re-arms from `duration_s` |
| `cancel(sub)` / `cancel_all()` | substring or all |
| `_load()` | migrates legacy format; rolls a lapsed recurring alarm forward; refreshes alarm labels |

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

`_alarm_loop` speaks the announcement once, rings `audio_in/alarm_clock.ogg` for
`satellite.alarm_ring_seconds` (default **5s** — the mic sits next to the speaker,
so the wake word is unreliable while it rings), then auto-stops and publishes
`antigua/alarm_ack`. Saying the wake word stops all ringing alarms early and drops
into conversation mode, so "snooze for 10 minutes" works immediately.

---

## Config (`server/config/server.yaml` — none; `config/satellite.yaml`)

```yaml
satellite:
  alarm_ring_seconds: 5
```

Persistence path: `settings.TIMER_STORE_PATH` → `data/timers.json`.

---

## MQTT Events

| Topic | Direction | Payload | When |
|---|---|---|---|
| `antigua/timer_set` | server → Pi | `{id, label, kind, fires_at}` | set / reset |
| `antigua/alarm` | server → Pi | `{text, audio_url, label, kind}` | fires |
| `antigua/alarm_ack` | Pi → broker | `{label}` | ring ends |

PinedaDisplay subscribes to the same topics to drive its timer card.

---

## Tests

- `tests/test_timers.py` — snapshot suite: duration parsing, alarm + reminder
  specs (relative / clock / daypart / recurring), message extraction, phrasing,
  `TimerManager` (type split, `find`, `add_time`, `reset`, message round-trip,
  fire-callback payload, legacy-format migration, recurring roll-forward).
- `tests/test_pipeline.py` — end-to-end: set / cancel / add / status / recurring,
  plus a reminder set → status → cancel flow.
- `tests/fixtures/routing.yaml` — `timer_*` / `alarm_set` / `reminder_set` fixtures.

---

## Limitations

- "Half past six" / "quarter to seven" don't parse (needs numeric or "N:MM").
- Snooze targets the most recently fired alarm (within 15 min); with several
  alarms firing together it may pick the wrong one.
- Reminder message extraction is heuristic. A phrasing it can't split cleanly
  yields `message=None` (falls back to "Here's your reminder.") — no worse than
  before, never a wrong action.
- "This morning" that has already passed rolls silently to tomorrow.
- Ordinal / calendar dates ("on the first", "on the 15th", "March 3") don't
  parse — only relative days, weekday names, and dayparts.
- The clarifying prompt only fires for a *day without a time*. "Remind me to
  buy milk" (nothing at all) still goes to the LLM rather than asking "when?".
- Recurring alarms/reminders reschedule in-process; they also roll forward
  correctly on a restart, but a server down across the fire time misses that
  occurrence.
- Timer threads are daemons — a graceful shutdown doesn't wait on them.

---

## How to Extend

**Per-alarm ring length / sound:** add fields to `TimerSpec`, pass through
`antigua/alarm`, look up in `_alarm_loop`. A gentler cue for `kind="reminder"`
(shorter ring, softer sound) would be a natural first use — the `kind` is
already on the MQTT payload.
**"Half past" / "quarter to":** extend the clock-time branch of `_resolve_clock_time`.
**Edit / reschedule a reminder:** "move the dentist reminder to 4" — currently
you cancel and re-add.
