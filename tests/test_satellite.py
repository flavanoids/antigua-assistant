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


def test_volume_by_kind():
    # 2026-10-30: the night-hours speaking volume is gone; speech is flat all day.
    import antigua_satellite as sat
    sat.ANTIGUA_VOLUME_PCT = 70
    sat.ALARM_VOLUME_PCT = 75
    assert current_volume_pct(kind="speech") == 70
    assert current_volume_pct(kind="alarm") == 75
    assert _antigua_vol_args(kind="speech") == ["--volume", "45875"]
    assert _antigua_vol_args(kind="alarm") == ["--volume", "49152"]


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


def _rings(kind: str) -> int:
    """How many times _alarm_loop rings, with no sound and no waiting."""
    import antigua_satellite as sat
    played, acks = [], []
    saved = (sat._play_until, sat.mqtt_publish, sat.ALARM_SOUND, sat._ALARM_REPEAT_S, sat._ALARM_MAX_S)
    sat._play_until = lambda cmd, ev, limit_s=None: played.append(cmd)
    sat.mqtt_publish = lambda topic, payload: acks.append(topic)
    sat.ALARM_SOUND = Path(__file__)          # exists, so each ring plays it once
    sat._ALARM_REPEAT_S, sat._ALARM_MAX_S = 0.0002, 0.001   # an alarm rings ~5 times
    try:
        sat._alarm_loop("", "5 minute timer", kind=kind, fire_id=f"t-{kind}")
    finally:
        (sat._play_until, sat.mqtt_publish, sat.ALARM_SOUND,
         sat._ALARM_REPEAT_S, sat._ALARM_MAX_S) = saved
    assert acks == ["antigua/alarm_ack"], acks
    return len(played)


def test_timer_rings_once():
    # 2026-10-22: a finished timer rang three times before anyone said stop.
    assert _rings("timer") == 1
    assert _rings("alarm") > 1               # alarms keep ringing until stopped


def main():
    test_blips_never_fail_over()
    test_outage_and_restore()
    test_volume_by_kind()
    test_satellite_alarm_store()
    test_timer_rings_once()
    print("PASS — satellite suite")


if __name__ == "__main__":
    main()
