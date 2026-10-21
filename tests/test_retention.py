#!/usr/bin/env python3
"""Retention of the SQLite logs: tool calls (they hold transcripts) keep 7
days like the mic captures, camera/lock events keep 30, and both prune while
the server runs, not only at startup.

Run: python3 tests/test_retention.py   (also works under pytest)
"""

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import security  # noqa: E402
from antigua_core.mcp_client import ToolLog  # noqa: E402

DAY = 86400


def _calls(path: Path) -> list[str]:
    with sqlite3.connect(path) as db:
        return [r[0] for r in db.execute("SELECT tool FROM calls ORDER BY ts")]


def _seed_calls(path: Path, ages_days: dict[str, float]) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS calls (ts REAL, trace TEXT, path TEXT,"
                   " conversation TEXT, speaker TEXT, transcript TEXT, server TEXT,"
                   " tool TEXT, args TEXT, ok INTEGER, error TEXT, result TEXT, ms REAL)")
        for tool, age in ages_days.items():
            db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (time.time() - age * DAY, "", "", "", "", "what I said", "s",
                        tool, "{}", 1, "", "", 1.0))


def test_tool_calls_keep_7_days_at_startup():
    path = Path(tempfile.mkdtemp()) / "tool_calls.db"
    _seed_calls(path, {"old": 8, "edge": 6.9, "new": 0})
    ToolLog().open(path)
    assert _calls(path) == ["edge", "new"], _calls(path)


def test_tool_calls_prune_while_running():
    path = Path(tempfile.mkdtemp()) / "tool_calls.db"
    log = ToolLog()
    log.open(path)
    _seed_calls(path, {"aged_out": 10})
    log._pruned = 0.0  # the last prune was over an hour ago
    log._rows.put((time.time(), "", "", "", "", "", "s", "fresh", "{}", 1, "", "", 1.0))
    for _ in range(100):
        if "fresh" in _calls(path):
            break
        time.sleep(0.02)
    assert _calls(path) == ["fresh"], _calls(path)


def _events(log: security.EventLog) -> list[str]:
    with log._db() as db:
        return [r[0] for r in db.execute("SELECT event FROM events ORDER BY ts")]


def test_security_events_keep_30_days():
    path = Path(tempfile.mkdtemp()) / "events.db"
    log = security.EventLog(path)
    log.add("garage", "old", ts=time.time() - 31 * DAY)
    log.add("garage", "recent", ts=time.time() - 29 * DAY)
    security.EventLog(path)  # a restart prunes
    assert _events(log) == ["recent"], _events(log)

    log.add("garage", "old_again", ts=time.time() - 40 * DAY)
    log._pruned = time.time() - 2 * DAY  # the last prune was over a day ago
    log.add("garage", "now")
    assert _events(log) == ["recent", "now"], _events(log)


def main():
    test_tool_calls_keep_7_days_at_startup()
    test_tool_calls_prune_while_running()
    test_security_events_keep_30_days()
    print("PASS — retention suite")


if __name__ == "__main__":
    main()
