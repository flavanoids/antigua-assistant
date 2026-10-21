"""Alarms & reminders over HTTP, for PinedaDisplay's phone menu.

Mixed into both servers' request handlers: GET /alarms lists them, POST sets
one (stores.phone_spec), PATCH /alarms/<id> {"enabled": bool} switches one
off or on, DELETE removes it. The same TimerManager as the voice ones, so
both show up in either place.

The fallback answers only while it's standing in for the primary
(alarms_ready); otherwise 503 sends pineda-web on to the primary. The
primary also takes POST /handoff: the fallback's alarms, memories and lists
when the primary comes back (see antigua_fallback_server._hand_back).
"""

import json
import logging
from urllib.parse import urlparse

from . import server_common
from .stores import phone_spec

log = logging.getLogger("antigua_core")


class AlarmRoutes:
    """Needs `self.alarm_timers` (a TimerManager) and `self._json(code, data)`."""

    alarm_timers = None

    def alarms_ready(self) -> bool:
        return True

    def route_alarms(self, method: str) -> bool:
        """Handle the request if it's an /alarms one; True when it was."""
        path = urlparse(self.path).path
        if path != "/alarms" and not path.startswith("/alarms/"):
            return False
        if not self.alarms_ready():
            self._json(503, {"error": "the primary has them"})
            return True
        tid = path[len("/alarms/"):] if path.startswith("/alarms/") else None
        timers = self.alarm_timers
        if method == "GET" and tid is None:
            self._json(200, {"alarms": timers.list_all()})
        elif method == "POST" and tid is None:
            data = self.read_json()
            if data is None:
                return True
            try:
                spec = phone_spec(data)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return True
            new = timers.set(spec)
            self._json(201, next((t for t in timers.list_all() if t["id"] == new), None))
        elif method == "PATCH" and tid:
            data = self.read_json()
            if data is None:
                return True
            if not isinstance(data.get("enabled"), bool):
                self._json(400, {"error": "enabled must be true or false"})
                return True
            row = timers.set_enabled(tid, data["enabled"])
            if row is None:
                self._json(404, {"error": "gone"})
            else:
                self._json(200, row)
        elif method == "DELETE" and tid:
            if timers.cancel_ids([tid]):
                self._json(200, {"deleted": tid})
            else:
                self._json(404, {"error": "gone"})
        else:
            self._json(405, {"error": "method not allowed"})
        return True

    def read_json(self):
        """The request body as a dict, or None after answering 400."""
        body = server_common.read_body(self, server_common.MAX_JSON_BYTES)
        if body is None:
            return None
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            self._json(400, {"error": "invalid JSON"})
            return None
        return data
