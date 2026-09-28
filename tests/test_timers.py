#!/usr/bin/env python3
"""Snapshot tests for timer/alarm parsing, phrasing, and the TimerManager.

No services: everything is pure or in-memory. Spoken strings are asserted
verbatim; a wording change shows up as a failure.

Run: python3 tests/test_timers.py   (also works under pytest)
"""

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

settings.configure({})
settings.TIMER_STORE_PATH = Path(tempfile.mkdtemp()) / "timers.json"

from antigua_core import classify as C  # noqa: E402
from antigua_core import pipeline  # noqa: E402
from antigua_core.stores import TimerManager, TimerSpec  # noqa: E402

FAILS = []


def eq(label, got, want):
    if got != want:
        FAILS.append(f"{label}\n   want: {want!r}\n   got:  {got!r}")
        print(f"[FAIL] {label}: {got!r} != {want!r}")
    else:
        print(f"[ok]   {label}: {got!r}")


# ── duration parsing ────────────────────────────────────────────────────────
for text, secs in [
    ("set a timer for 5 minutes", 300),
    ("set a timer for an hour and a half", 5400),
    ("set a timer for two and a half minutes", 150),
    ("set a timer for 1 hour 30 minutes", 5400),
    ("half an hour timer", 1800),
    ("quarter of an hour", 900),
    ("set a timer for ninety seconds", 90),
    ("set a timer for twenty five minutes", 1500),
    ("remind me in 45 seconds", 45),
]:
    eq(f"duration {text!r}", C.resolve_duration(C._replace_word_numbers(text)), float(secs))


# ── timer specs ─────────────────────────────────────────────────────────────
def spec(text):
    return C.parse_timer_request(text)


s = spec("set a pasta timer for 10 minutes")
eq("pasta timer name", (s.seconds, s.label, s.kind, s.name),
   (600.0, "pasta timer", "timer", "pasta"))
s = spec("set a 20 minute timer for banana bread")
eq("multi-word name", (s.label, s.name), ("banana bread timer", "banana bread"))
s = spec("remind me in 30 seconds")
eq("reminder kind", (s.kind, s.label), ("reminder", "reminder"))
eq("not a timer", spec("how long is a marathon"), None)


# ── alarm specs ─────────────────────────────────────────────────────────────
def alarm(text):
    return C.parse_alarm_request(text)


a = alarm("set an alarm for every weekday at 7")
eq("recurring weekdays", (a.kind, a.repeat, a.hour), ("alarm", "weekdays", 7))
a = alarm("wake me up at 5 every monday and wednesday")
eq("recurring mon/wed", a.repeat, "0,2")
a = alarm("wake me at seven tomorrow")
eq("word-number alarm", (a.hour, a.wake), (7, True))
eq("relative is a timer not alarm", alarm("wake me up in 5 minutes"), None)
a = alarm("set an alarm for 3")
eq("bare hour -> PM", a.hour, 15)


# ── reminder specs (message payload + relative / clock / recurring) ─────────
def rem(text):
    return C.parse_reminder_request(text)


r = rem("remind me to move the laundry in 40 minutes")
eq("reminder relative", (r.kind, r.message, r.hour, round(r.seconds)),
   ("reminder", "move the laundry", None, 2400))
r = rem("remind me in 2 hours to check the roast")
eq("reminder message after time", r.message, "check the roast")
r = rem("remind me to call the dentist at 3 tomorrow")
eq("reminder clock time", (r.message, r.hour), ("call the dentist", 15))
r = rem("set a reminder to call mom at 6pm")
eq("reminder 'set a reminder' framing", (r.message, r.hour), ("call mom", 18))
r = rem("remind me to water the plants every morning at 9")
eq("reminder recurring", (r.message, r.hour, r.repeat),
   ("water the plants", 9, "daily"))
r = rem("remind me in 30 seconds")
eq("reminder no message", (r.kind, r.message), ("reminder", None))
eq("reminder needs a trigger time", rem("remind me to bring an umbrella"), None)
eq("not a reminder at all", rem("what's the weather"), None)
r = rem("don't let me forget to take the trash out tonight")
eq("reminder daypart 'tonight'", (r.message, r.hour), ("take the trash out", 21))
r = rem("remind me to water the garden tomorrow morning")
eq("reminder daypart 'tomorrow morning'", (r.message, r.hour), ("water the garden", 8))
r = rem("remind me at noon to call the bank")
eq("reminder 'at noon'", (r.message, r.hour), ("call the bank", 12))

# under-specified: a day but no time -> the pipeline should ask
eq("missing time: 'tomorrow'",
   C.reminder_missing_time("remind me to call the dentist tomorrow"),
   ("call the dentist", "tomorrow"))
eq("missing time: 'on friday'",
   C.reminder_missing_time("remind me to email the landlord on friday"),
   ("email the landlord", "on friday"))
eq("not missing: has a time", C.reminder_missing_time("remind me to stretch at 6 tomorrow"), None)
eq("not missing: has a daypart", C.reminder_missing_time("remind me to stretch tomorrow morning"), None)
eq("not missing: no day at all", C.reminder_missing_time("remind me to bring an umbrella"), None)


# ── phrasing (needs a Backend stub for B.timers) ────────────────────────────
class _StubBackend:
    timers = TimerManager()


pipeline.B = _StubBackend()

eq("set reply timer",
   pipeline.format_set_reply(TimerSpec(600, "pasta timer", "timer", name="pasta")),
   "Pasta timer set for 10 minutes.")
eq("set reply reminder",
   pipeline.format_set_reply(TimerSpec(90, "reminder", "reminder")),
   "Okay, I'll remind you in 1 minute 30 seconds.")
eq("set reply reminder with message (relative)",
   pipeline.format_set_reply(
       TimerSpec(2400, "move the laundry", "reminder", message="move the laundry")),
   "Okay, I'll remind you to move the laundry in 40 minutes.")
eq("set reply reminder with message (clock)",
   pipeline.format_set_reply(C.parse_reminder_request(
       "remind me to call the dentist at 3 tomorrow")),
   "Okay, I'll remind you to call the dentist at 3 PM tomorrow.")

# recurring alarm -> names the cadence (built via the parser so seconds align)
future = C.parse_alarm_request("set an alarm for 7 am every weekday")
eq("set reply recurring alarm",
   pipeline.format_set_reply(future), "Alarm set for 7 AM every weekday.")


# ── TimerManager: type split, find, add_time, reset ─────────────────────────
tm = TimerManager()
tm.set(TimerSpec(600, "pasta timer", "timer", name="pasta"))
tm.set(TimerSpec(300, "tea timer", "timer", name="tea"))
rows = tm.list_active()
eq("two active", len(rows), 2)
eq("find by name", [r["label"] for r in tm.find("pasta")], ["pasta timer"])

res = tm.add_time("pasta", 120)
eq("add_time returns label", res[0], "pasta timer")
eq("add_time extends", round(res[1]) > 700, True)

eq("reset unknown", tm.reset("laundry"), None)
eq("reset pasta", tm.reset("pasta"), "pasta timer")
eq("cancel tea", tm.cancel("tea"), ["tea timer"])
tm.cancel_all()

# alarms are not resettable
tm.set(TimerSpec(3600, "alarm for 7:00 AM", "alarm", duration_s=0, hour=7))
eq("alarm not resettable", tm.reset(""), None)
tm.cancel_all()

# reminders: message survives to list_active / status phrasing / fire callback
fired = []
tm_r = TimerManager(on_fire=lambda label, kind="timer", message=None: fired.append((label, kind, message)))
tm_r.set(TimerSpec(1800, "call the dentist", "reminder", message="call the dentist"))
row = tm_r.list_active()[0]
eq("reminder row carries message", row["message"], "call the dentist")
eq("reminder cancel by message substring", tm_r.cancel("dentist"), ["call the dentist"])

# fire callback receives the message
tm_r.set(TimerSpec(0.05, "take the pizza out", "reminder", message="take the pizza out"))
time.sleep(0.3)
eq("fire callback gets message", fired and fired[-1], ("take the pizza out", "reminder", "take the pizza out"))
tm_r.cancel_all()


# ── persistence: old format migrates, alarm labels refresh ──────────────────
old = [{"label": "5 minutes timer", "fires_at": time.time() + 300}]
settings.TIMER_STORE_PATH.write_text(json.dumps(old))
tm2 = TimerManager()
tm2._load()
eq("legacy load", tm2.list_active()[0]["kind"], "timer")
tm2.cancel_all()

# a recurring alarm whose fire time already passed rolls forward on load
past = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0) - timedelta(hours=2)
rec = [{"label": "alarm for 7:00 AM, every day", "kind": "alarm",
        "fires_at": past.timestamp(), "duration_s": 0, "repeat": "daily",
        "hour": 7, "minute": 0, "wake": False}]
settings.TIMER_STORE_PATH.write_text(json.dumps(rec))
tm3 = TimerManager()
tm3._load()
row = tm3.list_active()[0]
eq("recurring alarm rolled forward", row["fires_at"] > time.time(), True)
eq("recurring alarm label kept cadence", "every day" in row["label"], True)
tm3.cancel_all()


# ── failover: the fallback adopts the primary's alarms ──────────────────────
now = time.time()
prim = TimerManager()
prim.set(TimerSpec(3600, "alarm for 7:00 AM", "alarm", duration_s=0, hour=7))
snap = prim.snapshot()
eq("snapshot is on-disk form", (len(snap), snap[0]["kind"]), (1, "alarm"))
prim.cancel_all()

rang = []
fb = TimerManager(on_fire=lambda label, kind="timer", message=None: rang.append(label))
fb.set(TimerSpec(600, "tea timer", "timer", name="tea"))            # the fallback's own
due = [
    {"label": "future alarm", "kind": "alarm", "fires_at": now + 600},
    {"label": "missed while down", "kind": "reminder", "fires_at": now - 20, "message": "x"},
    {"label": "rung before it died", "kind": "timer", "fires_at": now - 60},
    {"label": "too late", "kind": "timer", "fires_at": now - 3600},
    {"label": "daily past", "kind": "timer", "fires_at": now - 60, "repeat": "daily"},
]
# primary last answered 30s ago: the -60s entries were its to ring, the -20s one wasn't
eq("adopt count", fb.adopt(due, primary_alive_until=now - 30), 3)
time.sleep(1.5)
eq("overdue since primary went quiet rings now", rang, ["missed while down"])
labels = sorted(t["label"] for t in fb.list_active())
eq("adopted + own", labels, ["daily past", "future alarm", "tea timer"])
daily = next(t for t in fb.list_active() if t["label"] == "daily past")
eq("repeating the primary rang rolls forward", daily["fires_at"] > now + 3600, True)
eq("adopted never persisted here", [t["label"] for t in json.loads(settings.TIMER_STORE_PATH.read_text())],
   ["tea timer"])
eq("drop_adopted", fb.drop_adopted(), 2)
eq("own timer survives drop", [t["label"] for t in fb.list_active()], ["tea timer"])
fb.cancel_all()

# a rung one-shot leaves the file immediately (the fallback mirrors it)
tm4 = TimerManager()
tm4.set(TimerSpec(0.05, "egg timer", "timer"))
time.sleep(1.3)
eq("fired one-shot removed from disk", json.loads(settings.TIMER_STORE_PATH.read_text()), [])


# ── Timer references: which entry a request means ──────────────────────────
R = C.TimerRef
for phrase, want in [
    ("the first 5 minute timer", R(kind="timer", seconds=300.0, ordinal=0)),
    ("the ten minute timer", R(kind="timer", seconds=600.0)),
    ("an hour and a half timer", R(kind="timer", seconds=5400.0)),
    ("my gym alarm", R(kind="alarm", name="gym")),
    ("my 8 AM alarm", R(kind="alarm", hour=8, meridiem=True)),
    ("the 7:30 alarm", R(kind="alarm", hour=7, minute=30)),
    ("the dentist reminder", R(kind="reminder", name="dentist")),
    ("eggs", R(name="eggs")),
    ("the other one", R(other=True)),
    ("the one I set first", R(ordinal=0)),
    ("the last one", R(ordinal=-1)),
    ("the one with 3 minutes left", R(seconds=180.0)),
    ("the 30 second timer", R(kind="timer", seconds=30.0)),
    ("the second 30 second timer", R(kind="timer", seconds=30.0, ordinal=1)),
]:
    eq(f"ref {phrase!r}", C.parse_timer_ref(phrase), want)

for text, want in [
    ("How much time is left?", R()),
    ("How much time is left on the pasta timer?", R(kind="timer", name="pasta")),
    ("How long left on the laundry?", R(name="laundry")),
    ("How much time is left on eggs?", R(name="eggs")),
    ("How much time is left on the 5 minute timer?", R(kind="timer", seconds=300.0)),
    ("How long on the first timer?", R(kind="timer", ordinal=0)),
    ("How long until my gym alarm?", R(kind="alarm", name="gym")),
    ("When is my gym alarm?", R(kind="alarm", name="gym")),
    ("How much time until my 8 AM alarm?", R(kind="alarm", hour=8, meridiem=True)),
    ("When does the pasta timer go off?", R(kind="timer", name="pasta")),
    ("What timers do I have?", R(kind="timer")),
]:
    eq(f"status ref {text!r}", C.parse_timer_status_ref(text), want)
    eq(f"routes {text!r}", C.classify(text), "timer_status")

for text, want in [
    ("Add 2 minutes to the 5 minute timer", (120.0, "5 minute")),   # was 7 minutes
    ("add 5 minutes to the timer", (300.0, "timer")),
    ("extend the pasta timer by 5 minutes", (300.0, "pasta")),
    ("add a minute to the second one", (60.0, "second")),
    ("add 5 minutes to it", (300.0, "it")),
    ("give it 2 more minutes", (120.0, "it")),
    ("give it 2 more minutes to the pasta timer", (120.0, "pasta")),
]:
    eq(f"add {text!r}", C.parse_add_time_request(text), want)

for text, want in [
    ("cancel both timers", ("all", None)),
    ("cancel both", ("all", None)),
    ("cancel all alarms", ("all", None)),
    ("cancel the 5 minute timer", ("label", "5 minute")),
    ("cancel the first one", ("label", "first one")),
    ("turn off the other one", None),
    ("cancel the other one", ("label", "other one")),
]:
    eq(f"cancel {text!r}", C.parse_timer_cancel_request(text), want)
eq("generic cancel names nothing", C.parse_timer_ref(C.parse_timer_cancel_request("cancel the timer")[1]).empty(), True)

eq("named alarm keeps its name", C.parse_alarm_request("Set an alarm for 7 AM called gym").name, "gym")
eq("named alarm reply", pipeline.format_set_reply(C.parse_alarm_request("Set an alarm for 7 AM called gym")).split(" set for")[0], "Gym alarm")

# store: set length survives add_time; cancel by id / by kind; persisted
tm5 = TimerManager()
a = tm5.set(TimerSpec(300, "5 minutes timer"))
b = tm5.set(TimerSpec(300, "5 minutes timer"))
tm5.set(TimerSpec(3600, "alarm for 7:00 AM", "alarm", duration_s=0, hour=7))
eq("add_time_id", round(tm5.add_time_id(a, 120)) in (419, 420), True)
eq("set_s unchanged by add", {r["id"]: r["set_s"] for r in tm5.list_active() if r["kind"] == "timer"}, {a: 300.0, b: 300.0})
eq("set order kept", [r["id"] for r in sorted(tm5.list_active(), key=lambda r: r["created_at"])][:2], [a, b])
saved = json.loads(settings.TIMER_STORE_PATH.read_text())
eq("set_s + created_at persisted", all("set_s" in d and "created_at" in d for d in saved), True)
eq("cancel_ids", tm5.cancel_ids([b, "nope"]), ["5 minutes timer"])
eq("cancel_all(kind)", tm5.cancel_all("alarm"), ["alarm for 7:00 AM"])
eq("timer left after kind cancel", [r["id"] for r in tm5.list_active()], [a])
tm5.cancel_all()


if FAILS:
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1)
print("\nPASS — timer/alarm suite")
