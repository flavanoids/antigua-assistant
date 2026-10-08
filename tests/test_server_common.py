#!/usr/bin/env python3
"""Tests for antigua_core.server_common — the helpers both servers share.

These were once copied into each server and drifted (the fallback lost the
snoozed-alarm wording and the display-topic gate), so pin their behavior.

Run: python3 tests/test_server_common.py
"""

import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import server_common as sc  # noqa: E402
from antigua_core import settings  # noqa: E402


def run():
    # What rings: every kind, including the snooze wording the fallback lost.
    assert sc.timer_fire_text("pasta timer", "timer") == "Your pasta timer is done."
    assert sc.timer_fire_text("wake up alarm", "alarm") == "Good morning, your alarm is going off."
    assert sc.timer_fire_text("7 AM alarm", "alarm") == "Your alarm is going off."
    assert sc.timer_fire_text("7 AM alarm snooze", "alarm") == "Time to get up."  # pipeline._handle_snooze label
    assert sc.timer_fire_text("reminder", "reminder", "move the laundry") == "Reminder: move the laundry."
    assert sc.timer_fire_text("reminder", "reminder") == "Here's your reminder."

    # Prompt time: hour precision, so it is stable within the hour (prefix cache).
    a = sc.llm_now_str(datetime(2026, 9, 28, 15, 2))
    b = sc.llm_now_str(datetime(2026, 9, 28, 15, 58))
    assert a == b == "Monday, September 28, 2026, around 3 PM (afternoon)", a
    assert sc.llm_now_str(datetime(2026, 9, 28, 2, 0)).endswith("(late night)")

    # Display gate: display-only topics drop unless enabled; playback never does.
    settings.configure({})
    assert not sc.display_allows("antigua/transcript")
    assert sc.display_allows("antigua/play") and sc.display_allows("antigua/alarm")
    settings.configure({"display": {"enabled": True}})
    assert sc.display_allows("antigua/transcript")

    # configure() starts from pristine defaults, not from the previous call.
    settings.configure({"search": {"home_city": "Springfield"}})
    settings.configure({})
    assert settings.SEARCH_HOME_CITY == "" and not settings.DISPLAY_ENABLED

    # Audio pruning: cache_*.wav files outlive plain replies by cache_ttl.
    d = Path(tempfile.mkdtemp(prefix="antigua_prune_test_"))
    old = time.time() - 400
    for name in ("reply.wav", "cache_abc.wav", "fresh.wav"):
        (d / name).write_bytes(b"")
    for name in ("reply.wav", "cache_abc.wav"):
        os.utime(d / name, (old, old))
    assert sc.prune_audio(d, ttl=300, cache_ttl=3600) == 1
    assert sorted(p.name for p in d.iterdir()) == ["cache_abc.wav", "fresh.wav"]

    # Over the size cap, the least recently used cache files go first.
    d = Path(tempfile.mkdtemp(prefix="antigua_prune_test_"))
    for i, name in enumerate(("cache_old.wav", "cache_mid.wav", "cache_new.wav")):
        (d / name).write_bytes(b"x" * 100)
        t = time.time() - 300 + i * 100
        os.utime(d / name, (t, t))
    assert sc.prune_audio(d, ttl=60, cache_ttl=3600, cache_max_bytes=250) == 1
    assert sorted(p.name for p in d.iterdir()) == ["cache_mid.wav", "cache_new.wav"]

    print("All server_common checks passed")


if __name__ == "__main__":
    run()
