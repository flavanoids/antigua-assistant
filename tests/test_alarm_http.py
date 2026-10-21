#!/usr/bin/env python3
"""The /alarms routes PinedaDisplay's phone menu uses (antigua_core.alarm_http),
over real HTTP on a throwaway port.

Run: python3 tests/test_alarm_http.py   (also works under pytest)
"""

import json
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import requests  # noqa: E402

from antigua_core import settings  # noqa: E402

settings.configure({})
settings.TIMER_STORE_PATH = Path(tempfile.mkdtemp()) / "timers.json"

from antigua_core.alarm_http import AlarmRoutes  # noqa: E402
from antigua_core.stores import TimerManager  # noqa: E402

READY = {"on": True}


class Handler(AlarmRoutes, BaseHTTPRequestHandler):
    alarm_timers = TimerManager()

    def alarms_ready(self):
        return READY["on"]

    def log_message(self, *a):
        pass

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        self.route_alarms("GET") or self._json(404, {})

    def do_POST(self):
        self.route_alarms("POST") or self._json(404, {})

    def do_PATCH(self):
        self.route_alarms("PATCH") or self._json(404, {})

    def do_DELETE(self):
        self.route_alarms("DELETE") or self._json(404, {})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    assert requests.get(base + "/alarms").json() == {"alarms": []}
    r = requests.post(base + "/alarms", json={"kind": "reminder", "time": "18:00",
                                              "date": "2030-01-02", "message": "call mom"})
    assert r.status_code == 201, r.text
    row = r.json()
    assert (row["kind"], row["message"], row["enabled"]) == ("reminder", "call mom", True)

    bad = requests.post(base + "/alarms", json={"kind": "alarm", "time": "25:00"})
    assert bad.status_code == 400 and bad.json()["error"] == "time must be HH:MM"
    assert requests.post(base + "/alarms", data="nope").status_code == 400

    off = requests.patch(f"{base}/alarms/{row['id']}", json={"enabled": False})
    assert off.json()["enabled"] is False
    assert requests.patch(f"{base}/alarms/{row['id']}", json={"enabled": "no"}).status_code == 400

    READY["on"] = False   # the fallback while the primary is up
    assert requests.get(base + "/alarms").status_code == 503
    READY["on"] = True

    assert requests.delete(f"{base}/alarms/{row['id']}").status_code == 200
    assert requests.delete(f"{base}/alarms/{row['id']}").status_code == 404
    assert requests.get(base + "/other").status_code == 404
    server.shutdown()
    print("PASS — alarm HTTP suite")


def test_alarm_http():
    main()


if __name__ == "__main__":
    main()
