#!/usr/bin/env python3
"""The satellite's failover debounce (antigua_satellite.PrimaryWatch): one
missed health check is a blip, three in a row are an outage. No services.

Run: python3 tests/test_satellite.py   (also works under pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from antigua_satellite import (  # noqa: E402
    PrimaryWatch,
    SatelliteAlarmStore,
    _antigua_vol_args,
    current_volume_pct,
    in_hours,
)


def feed(results):
    w = PrimaryWatch(misses=3, hits=2)
    return [w.probe(r) for r in results]


def test_blips_never_fail_over():
    # The 2026-10-15 pattern: single misses between good probes.
    assert feed([True, False, True, False, False, True, True]) == [None] * 7


def test_outage_and_restore():
    out = feed([False, False, False, False, True, False, True, True, True])
    assert out == [None, None, "down", None, None, None, None, "up", None], out


def _make_time(hour: int, minute: int):
    import time
    return time.struct_time((2026, 10, 19, hour, minute, 0, 0, 0, -1))


def test_in_hours():
    # Midnight spanning window: 22:30 to 07:00
    w = "22:30-07:00"
    assert not in_hours(w, _make_time(22, 29))
    assert in_hours(w, _make_time(22, 30))
    assert in_hours(w, _make_time(23, 45))
    assert in_hours(w, _make_time(0, 0))
    assert in_hours(w, _make_time(4, 15))
    assert in_hours(w, _make_time(6, 59))
    assert not in_hours(w, _make_time(7, 0))
    assert not in_hours(w, _make_time(12, 0))

    # Same day window: 09:00 to 17:00
    day_w = "09:00-17:00"
    assert not in_hours(day_w, _make_time(8, 59))
    assert in_hours(day_w, _make_time(9, 0))
    assert in_hours(day_w, _make_time(12, 30))
    assert in_hours(day_w, _make_time(16, 59))
    assert not in_hours(day_w, _make_time(17, 0))

    # Empty or invalid
    assert not in_hours("", _make_time(12, 0))
    assert not in_hours("garbage", _make_time(12, 0))


def test_volume_scaling(monkeypatch=None):
    import antigua_satellite as sat

    # Simulate Daytime
    sat.NIGHT_HOURS = "22:30-07:00"
    sat.ANTIGUA_VOLUME_PCT = 70
    sat.NIGHT_VOLUME_PCT = 30
    sat.ALARM_VOLUME_PCT = 75

    # Day time check (14:00)
    day_time = _make_time(14, 0)
    # Monkeypatch time.localtime to day_time
    import time
    orig_local = time.localtime
    try:
        time.localtime = lambda: day_time
        assert current_volume_pct(kind="speech") == 70
        assert current_volume_pct(kind="alarm") == 75
        assert _antigua_vol_args(kind="speech") == ["--volume", "45875"]
        assert _antigua_vol_args(kind="alarm") == ["--volume", "49152"]

        # Night time check (02:00)
        night_time = _make_time(2, 0)
        time.localtime = lambda: night_time
        # Speech drops to night volume (30%)
        assert current_volume_pct(kind="speech") == 30
        assert _antigua_vol_args(kind="speech") == ["--volume", "19661"]
        # Alarm stays at full alarm volume (75%)
        assert current_volume_pct(kind="alarm") == 75
        assert _antigua_vol_args(kind="alarm") == ["--volume", "49152"]
    finally:
        time.localtime = orig_local


def test_satellite_alarm_store():
    import tempfile
    import time

    tmp_dir = tempfile.mkdtemp()
    store_file = Path(tmp_dir) / "satellite_alarms.json"

    store = SatelliteAlarmStore(path=store_file, grace_seconds=5.0)
    t0 = time.time()

    alarms = [
        {"id": "a1", "label": "morning alarm", "kind": "alarm", "fires_at": t0 + 10.0},
        {"id": "a2", "label": "pasta timer", "kind": "timer", "fires_at": t0 + 20.0},
    ]
    store.update_schedule(alarms)

    # Check persistence and reload
    store2 = SatelliteAlarmStore(path=store_file, grace_seconds=5.0)
    assert len(store2._alarms) == 2
    assert "a1" in store2._alarms
    assert "a2" in store2._alarms

    # Before fires_at: nothing due
    assert store.check_due_offline(now=t0 + 9.0) == []
    # At fires_at (within grace period): still waiting on server
    assert store.check_due_offline(now=t0 + 10.0) == []
    assert store.check_due_offline(now=t0 + 14.9) == []

    # At fires_at + 5.0 (grace period elapsed): server missed it, due for offline ringing
    due = store.check_due_offline(now=t0 + 15.0)
    assert len(due) == 1
    assert due[0]["id"] == "a1"
    assert due[0]["label"] == "morning alarm"

    # Repeated check does not re-fire
    assert store.check_due_offline(now=t0 + 16.0) == []

    # Server arrives on time for a2: mark_server_handled cancels offline fallback
    store.mark_server_handled("a2", "pasta timer")
    assert store.check_due_offline(now=t0 + 30.0) == []


def main():
    test_blips_never_fail_over()
    test_outage_and_restore()
    test_in_hours()
    test_volume_scaling()
    test_satellite_alarm_store()
    print("PASS — satellite suite")


if __name__ == "__main__":
    main()
