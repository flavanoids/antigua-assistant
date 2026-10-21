"""Inside the house: the AC controller's data server (Honeywell_SmartAC's
ac-data-server, GET /current) — the thermostat's indoor temperature and
humidity, its setpoint, and the share of the last half hour the AC ran.
The controller logs every 30 minutes, so an answer older than an hour says
when it's from. Read-only: the controller owns the setpoint."""

import logging
import time
from datetime import datetime

import requests
from num2words import num2words

from . import settings

log = logging.getLogger("antigua_core")

_cache: dict = {"at": 0.0, "data": None}
_TTL = 120


def current() -> dict | None:
    if not settings.CLIMATE_URL:
        return None
    if _cache["data"] is not None and time.time() - _cache["at"] < _TTL:
        return _cache["data"]
    try:
        r = requests.get(f"{settings.CLIMATE_URL.rstrip('/')}/current", timeout=settings.CLIMATE_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("Climate: %s", e)
        return None
    _cache.update(at=time.time(), data=data)
    return data


def _deg(x) -> str:
    return num2words(round(float(x)))


def _age(latest: dict) -> str:
    """ " as of 3:01" when the reading is over an hour old, else ""."""
    try:
        t = datetime.fromisoformat(latest["time"])
    except (KeyError, TypeError, ValueError):
        return ""
    if (datetime.now() - t).total_seconds() < 3600:
        return ""
    return f", as of {t:%-I:%M %p}".replace(":00 ", " ")


def answer(kind: str, data: dict | None = None) -> str:
    data = data if data is not None else current()
    latest = (data or {}).get("latest") or {}
    if not latest:
        return "I can't reach the thermostat right now."
    age = _age(latest)
    temp, sp, hum = latest.get("indoor_temp"), latest.get("current_setpoint"), latest.get("humidity_pct")
    run = latest.get("runtime_pct_30m")
    if kind == "running":
        if run is None:
            return "I can't tell whether the AC is running right now."
        run = float(run)
        if run >= 95:
            said = "Yes, it's been running the whole last half hour"
        elif run <= 5:
            said = "No, it hasn't run in the last half hour"
        else:
            said = f"On and off. It ran about {num2words(round(run))} percent of the last half hour"
        tail = f". It's {_deg(temp)} inside, set to {_deg(sp)}" if temp is not None and sp is not None else ""
        return f"{said}{tail}{age}."
    if kind == "setpoint":
        if sp is None:
            return "I can't read the thermostat's setting right now."
        tail = f", and it's {_deg(temp)} inside" if temp is not None else ""
        return f"The thermostat is set to {_deg(sp)}{tail}{age}."
    if kind == "humidity":
        if hum is None:
            return "I can't read the humidity inside right now."
        return f"It's {num2words(round(float(hum)))} percent humidity inside{age}."
    if temp is None:
        return "I can't read the temperature inside right now."
    tail = f", and the thermostat is set to {_deg(sp)}" if sp is not None else ""
    return f"It's {_deg(temp)} degrees inside{tail}{age}."
