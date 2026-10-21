#!/usr/bin/env python3
"""Weekday "good morning" off the back door: replays real mornings of the
door sensor and checks she speaks once, after the trip back in.

Run: python3 tests/test_morning.py   (also works under pytest)
"""

import os
import sys
import time
from pathlib import Path

os.environ["TZ"] = "America/Chicago"     # the door times below are Central
time.tzset()

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import morning  # noqa: E402


class FakeTimer:
    made: list["FakeTimer"] = []

    def __init__(self, delay, fn):
        self.delay, self.fn, self.cancelled, self.daemon = delay, fn, False, False
        FakeTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


def _run(events, **cfg):
    """Feed `events` [(state, "YYYY-MM-DD HH:MM:SS"), ...] (Central time),
    then fire whichever timer is still live. Returns what was said."""
    FakeTimer.made = []
    said = []
    m = morning.MorningGreeting(
        morning.Config(door="binary_sensor.back_door", name="Sam", **cfg),
        announce=said.append, timer=FakeTimer)
    for state, when in events:
        st = {"state": state, "last_changed": when.replace(" ", "T") + "-05:00"}
        m.on_state(st)
        m.on_state(st)                      # the poller repeats what the push sent
    live = [t for t in FakeTimer.made if not t.cancelled]
    assert len(live) <= 1, live
    for t in live:
        assert t.delay <= 12
        t.fn()
    return said


def _morning(day, *pairs):
    return [ev for o, c in pairs for ev in (("on", f"{day} {o}"), ("off", f"{day} {c}"))]


def test_out_then_in():
    # Thu 10-08: out 5:50:56, back 5:51:51.
    said = _run(_morning("2026-10-08", ("05:50:56", "05:51:01"), ("05:51:51", "05:51:58")))
    assert len(said) == 1 and said[0].startswith("Good morning, Sam. "), said
    assert said[0].split(". ", 1)[1] in [a for a, _ in morning.AFFIRMATIONS]


def test_a_bounce_is_the_same_trip():
    # Thu 10-15: the sensor bounced 2 s after the first close; back at 6:07:38.
    events = _morning("2026-10-15", ("06:05:06", "06:05:15"), ("06:05:17", "06:05:18"))
    assert _run(events) == []               # still outside
    events += _morning("2026-10-15", ("06:07:38", "06:07:43"))
    assert len(_run(events)) == 1


def test_door_reopened_while_coming_in():
    # Wed 10-14: back in at 6:08:59, then the door opened twice more.
    said = _run(_morning("2026-10-14", ("06:05:54", "06:05:56"), ("06:08:59", "06:09:05"),
                         ("06:09:07", "06:09:11"), ("06:09:30", "06:09:32")))
    assert len(said) == 1, said
    # Only the last close's timer is live: the earlier ones were cancelled.
    assert sum(not t.cancelled for t in FakeTimer.made) == 1


def test_weekend_and_outside_the_window():
    assert _run(_morning("2026-10-10", ("08:14:07", "08:14:13"), ("08:15:58", "08:16:04"))) == []
    assert _run(_morning("2026-10-08", ("15:51:30", "15:51:33"), ("15:53:26", "15:53:31"))) == []
    assert _run(_morning("2026-10-08", ("04:20:00", "04:20:05"), ("04:22:00", "04:22:05"))) == []


def test_not_back_in_time():
    events = _morning("2026-10-13", ("06:00:27", "06:00:32"), ("06:40:00", "06:40:05"))
    assert _run(events) == []


def test_once_a_day_even_across_a_restart():
    day = _morning("2026-10-12", ("07:19:02", "07:19:08"), ("07:21:14", "07:21:18"))
    assert len(_run(day)) == 1
    later = _morning("2026-10-12", ("08:30:00", "08:30:05"), ("08:31:00", "08:31:05"))
    assert _run(later) == []                # a fresh MorningGreeting: the restart case
    assert len(_run(_morning("2026-10-13", ("06:00:27", "06:00:32"),
                             ("06:02:11", "06:02:16")))) == 1


def test_no_repeats_until_the_deck_runs_out():
    morning._state_path().unlink(missing_ok=True)
    lines = {morning.pick() for _ in morning.AFFIRMATIONS}
    assert len(lines) == len(morning.AFFIRMATIONS) == 60
    assert all(src in morning.SOURCES for _, src in morning.AFFIRMATIONS)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
