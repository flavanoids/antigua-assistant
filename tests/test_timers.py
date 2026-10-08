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


# ── Polish (2026-10): phrasing the parser used to get wrong ─────────────────
for text, secs in [
    ("set a timer for an hour and 15 minutes", 4500),
    ("set a timer for 1 hour and a half", 5400),
    ("add a minute", 60),
    ("half an hour", 1800),                        # fuzzy forms untouched
    ("a quarter of an hour", 900),
]:
    eq(f"duration {text!r}", C.resolve_duration(text), float(secs))

for text, want in [
    ("set a timer for 10", 600.0),                 # bare number = minutes
    ("timer for 5", 300.0),
]:
    eq(f"bare-minute timer {text!r}", spec(text).seconds, want)
eq("timer name after the length", spec("set a timer for 12 minutes for the pasta").name, "pasta")
eq("timer 'name it'", spec("set a timer for 25 minutes and name it pizza").name, "pizza")

for text, want in [
    ("set an alarm for half past six", (6, 30)),
    ("set an alarm for quarter to seven", (6, 45)),
    ("set an alarm for 6 30", (6, 30)),
    ("set an alarm for six forty five", (6, 45)),
    ("set an alarm for 6 oh 5", (6, 5)),
    ("set an alarm for noon", (12, 0)),
    ("set an alarm for midnight", (0, 0)),
    ("set an alarm for 12 midnight", (0, 0)),
]:
    a = alarm(text)
    eq(f"clock {text!r}", a and (a.hour, a.minute), want)
eq("a past time today never rings now", alarm("set an alarm for 12:01 am today").seconds > 0, True)
eq("'tonight' in the past rolls a day", alarm("set an alarm for 12:01 am tonight").seconds > 0, True)

for text, want in [
    ("set an alarm for 6 am on weekdays", "weekdays"),
    ("set an alarm for 6:45 am monday through friday", "weekdays"),
    ("set an alarm for 8 on saturday and sunday", "weekends"),
    ("set an alarm for 8 on weekends", "weekends"),
    ("wake me up at 7 on mondays", "0"),
    ("set an alarm for 7 on monday and wednesday", "0,2"),
    ("set an alarm for 7 on friday", None),        # one day is a one-off
]:
    eq(f"repeat {text!r}", alarm(text).repeat, want)
eq("repeat phrase", pipeline._repeat_phrase("0,2,4"), "every Monday, Wednesday, and Friday")

r = rem("remind me to take my pills at 9 every night")
eq("'every night' is PM", (r.hour, r.repeat), (21, "daily"))
r = rem("remind me to check the oven in 10")
eq("reminder bare 'in 10'", (r.message, round(r.seconds)), ("check the oven", 600))
r = rem("set a reminder for 5 pm to call dad")
eq("reminder 'for 5 pm to'", (r.message, r.hour), ("call dad", 17))
r = rem("remind me to stretch every hour")
eq("reminder every hour", (r.message, r.repeat, round(r.seconds)), ("stretch", "every:3600", 3600))
r = rem("remind me every 30 minutes to drink water")
eq("reminder every 30 minutes", (r.message, r.repeat), ("drink water", "every:1800"))
eq("interval too short", rem("remind me to blink every minute"), None)
r = rem("remind me to call mom at six thirty tonight")
eq("reminder spoken clock", (r.message, r.hour, r.minute), ("call mom", 18, 30))
eq("interval reply", pipeline.format_set_reply(rem("remind me to stretch every hour")),
   "Okay, I'll remind you to stretch every hour.")

for text, want in [
    ("cancel my alarm", ("label", "my")),          # one alarm — asks if several
    ("cancel my timer", ("label", "my")),
    ("clear my timers", ("all", None)),
    ("cancel my reminder to call mom", ("label", "call mom")),
    ("cancel my alarm for tomorrow only", ("label", "tomorrow")),
    ("remove 2 minutes from the timer", None),     # not a cancel
]:
    eq(f"cancel {text!r}", C.parse_timer_cancel_request(text), want)
eq("'my' names nothing", C.parse_timer_ref("my").empty(), True)
eq("day ref", C.parse_timer_ref("tomorrow's alarm").day_word, "tomorrow")

for text, want in [
    ("take 2 minutes off the timer", (-120.0, "it")),
    ("subtract a minute from the pasta timer", (-60.0, "pasta")),
    ("knock 30 seconds off", (-30.0, "it")),
    ("add five more minutes", (300.0, "it")),
    ("add 30 seconds", (30.0, "it")),
    ("subtract 5 minutes from 3 hours", None),     # arithmetic, not a timer
]:
    eq(f"add/take off {text!r}", C.parse_add_time_request(text), want)

for text, want in [
    ("snooze", 300), ("snooze for 10 minutes", 600), ("snooze 10 more minutes", 600),
    ("snooze 10", 600), ("snooze for half an hour", 1800), ("give me 5 more minutes", 300),
]:
    eq(f"snooze {text!r}", C.parse_snooze_request(text), want)

for text, want in [
    ("set an alarm", True), ("wake me up", True), ("set an alarm for tomorrow", True),
    ("set an alarm for tomorrow morning", True), ("set an alarm for 7", False),
    ("I walk every morning", False), ("wake me up in 5 minutes", False),
]:
    eq(f"alarm missing time {text!r}", C.alarm_missing_time(text), want)
for text, want in [
    ("set a timer", ""), ("start a pasta timer", "pasta"),
    ("set a timer for the rice", "rice"), ("set a timer for 5 minutes", None),
]:
    eq(f"timer missing length {text!r}", C.timer_missing_length(text), want)

for text, want in [
    ("change my alarm to 7:30", ("", "alarm", "7:30", None)),
    ("move my gym alarm to 6", ("gym", "alarm", "6", None)),
    ("push my alarm back 15 minutes", ("", "alarm", None, 900.0)),
    ("move my alarm 10 minutes earlier", ("", "alarm", None, -600.0)),
    ("set an alarm for 7", None),                  # a new alarm, not a change
]:
    eq(f"change {text!r}", C.parse_alarm_change(text), want)
now0 = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
tgt = C.resolve_new_time("7:30", 19, None, (now0 + timedelta(days=1)).replace(hour=19).timestamp(), now=now0)
eq("change keeps PM and the day", (tgt[1], tgt[2], tgt[0].date()), (19, 30, (now0 + timedelta(days=1)).date()))

for text, want in [
    ("skip tomorrow's alarm", ("tomorrow's", "alarm")),
    ("skip my alarm on friday", ("on friday", "alarm")),
    ("skip the next alarm", ("next", "alarm")),
    ("turn off my alarm for tomorrow", ("tomorrow", "alarm")),
    ("turn off my alarm", None),
]:
    eq(f"skip {text!r}", C.parse_skip_request(text), want)

for text, want in [
    ("pause the timer", ("pause", "")), ("pause the pasta timer", ("pause", "pasta")),
    ("resume the timer", ("resume", "")), ("start the timer again", ("resume", "")),
    ("restart the timer", None), ("pause the music", None), ("stop the timer", None),
]:
    eq(f"pause {text!r}", C.parse_pause_request(text), want)

for text, want in [
    ("stop", True), ("stop it", True), ("okay stop", True), ("turn it off", True),
    ("turn off the alarm", True), ("I'm up", True), ("okay thanks", True),
    ("stop the music", False), ("cancel my alarm", False), ("what time is it", False),
]:
    eq(f"dismiss {text!r}", C.is_dismiss_request(text), want)

for text in ["what time is my alarm", "is my alarm set", "what time did I set my alarm for",
             "how much longer", "how much time on the pasta", "is there an alarm"]:
    eq(f"routes {text!r}", C.classify(text), "timer_status")
eq("status ref by day", C.parse_timer_status_ref("do I have any alarms tomorrow").day_word, "tomorrow")


# ── Store: restarts, repeats, skip, pause, reschedule ───────────────────────
def _load(entries):
    settings.TIMER_STORE_PATH.write_text(json.dumps(entries))
    tm = TimerManager(on_fire=lambda label, kind="timer", message=None: rung.append(label))
    tm._load()
    return tm


rung = []
now = time.time()
three_days = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0) - timedelta(days=3)
tm6 = _load([
    {"label": "alarm for 7:00 AM, every day", "kind": "alarm", "fires_at": three_days.timestamp(),
     "repeat": "daily", "hour": 7, "minute": 0},
    {"label": "egg timer", "kind": "timer", "fires_at": now - 120},              # missed in restart
    {"label": "old timer", "kind": "timer", "fires_at": now - 3600},             # long gone
    {"label": "paused timer", "kind": "timer", "fires_at": now - 9999, "paused_left": 240},
])
time.sleep(1.3)
eq("missed-in-restart rings on load", rung, ["egg timer"])
rows = {r["label"]: r for r in tm6.list_active()}
eq("daily alarm days behind rolls into the future",
   rows["alarm for 7:00 AM, every day"]["fires_at"] > time.time(), True)
eq("daily alarm keeps 7:00", datetime.fromtimestamp(rows["alarm for 7:00 AM, every day"]["fires_at"]).hour, 7)
eq("paused survives restart", (rows["paused timer"]["paused"], round(rows["paused timer"]["remaining_s"])),
   (True, 240))
eq("expired dropped", "old timer" in rows, False)
tm6.cancel_all()

tm7 = TimerManager(on_fire=lambda label, kind="timer", message=None: rung.append(label))
rung.clear()
tid = tm7.set(TimerSpec(0.3, "pasta timer", "timer", name="pasta"))
eq("pause keeps at least a second", round(tm7.pause(tid)), 1)
time.sleep(0.8)
eq("paused timer doesn't ring", rung, [])
eq("add to a paused timer", round(tm7.add_time_id(tid, 60)), 61)
eq("resume", round(tm7.resume(tid)) in (60, 61), True)
eq("resumed is running", tm7.list_active()[0]["paused"], False)
tm7.cancel_all()

iv = tm7.set(TimerSpec(0.2, "stretch", "reminder", message="stretch", repeat="every:3600", duration_s=0))
time.sleep(1.2)
eq("interval reminder rang", rung[-1:], ["stretch"])
nxt = tm7.list_active()[0]
eq("interval reminder re-armed an hour on", 3500 < nxt["remaining_s"] <= 3600, True)
tm7.cancel_all()

wd = C.parse_alarm_request("set an alarm for 6 am every day")
aid = tm7.set(wd)
first = datetime.fromtimestamp(tm7.list_active()[0]["fires_at"]).date()
nxt = tm7.skip(aid, first)
eq("skip moves past that day", datetime.fromtimestamp(nxt).date(), first + timedelta(days=1))
later = first + timedelta(days=3)
tm7.skip(aid, later)
eq("a later skip is remembered", tm7.list_active()[0]["skip"], [first.isoformat(), later.isoformat()])
row = tm7.reschedule(aid, (datetime.combine(first + timedelta(days=1), datetime.min.time())
                           + timedelta(hours=6, minutes=30)).timestamp(), 6, 30)
eq("reschedule relabels", row["label"], "alarm for 6:30 AM, every day")
eq("reschedule clears skips", row["skip"], [])
tm7.cancel_all()


if FAILS:
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1)
print("\nPASS — timer/alarm suite")
