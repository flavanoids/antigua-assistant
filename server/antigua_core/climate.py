"""Inside the house: the AC controller's data server (Honeywell_SmartAC's
ac-data-server, GET /current) — the thermostat's indoor temperature and
humidity, its setpoint, its mode and whether it's cooling or heating right
now (telemetry, every 15 min), and the share of the last half hour the AC
ran. The controller logs every 30 minutes, so an answer older than an hour
says when it's from. The controller owns the setpoint; the one switch voice
gets is windows-open mode (the controller's windows.py, run on this host),
which the controller turns into system Off at its next 30-minute run."""

import json
import logging
import subprocess
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


def _clock(iso) -> str:
    try:
        return f"{datetime.fromisoformat(iso):%-I:%M %p}".replace(":00 ", " ")
    except (TypeError, ValueError):
        return ""


def _windows_cmd(*args) -> dict | None:
    """Run the controller's windows.py with --json; its state, or None."""
    try:
        out = subprocess.run(["/usr/bin/python3", settings.CLIMATE_WINDOWS_SCRIPT, *args, "--json"],
                             capture_output=True, text=True, timeout=15, check=True).stdout
        return json.loads(out.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
        log.warning("Windows mode %s: %s", args, e)
        return None


def windows(kind: str, seconds: float | None = None) -> str:
    """"windows_open" / "windows_closed": flip the controller's windows-open
    mode and say what happens next."""
    if not settings.CLIMATE_WINDOWS_SCRIPT:
        return "I can't switch windows open mode from here."
    before = _windows_cmd("status")
    if before is None:
        return "I can't reach the AC controller right now."
    if not before.get("enabled"):
        return "Windows open mode is turned off in the AC controller's settings."
    _cache.update(at=0.0, data=None)
    if kind == "windows_open":
        args = ("open", f"{seconds / 3600:g}") if seconds else ("open",)
        st = _windows_cmd(*args)
        if not st or not st.get("open"):
            return "I couldn't turn on windows open mode."
        return ("Windows open mode is on. The AC shuts off within half an hour, "
                f"and it comes back on by itself at {_clock(st['open_until'])} if you forget.")
    if not before.get("open") and not before.get("engine_off"):
        return "Windows open mode wasn't on, so the AC is already running normally."
    st = _windows_cmd("closed")
    if st is None:
        return "I couldn't turn off windows open mode."
    if st.get("engine_off"):
        return "Windows open mode is off. The AC comes back on within half an hour."
    return "Windows open mode is off. The AC never shut off, so nothing changes."


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


def _windows_open(win: dict) -> bool:
    return bool(win.get("enabled") and win.get("open"))


def _status(data: dict, temp) -> str | None:
    """Cooling / heating / off, from the telemetry's mode and operation
    status, with windows-open mode said first. None when there's no fresh
    telemetry (older than 45 min), so the caller falls back to runtime."""
    th, win = data.get("thermostat") or {}, data.get("windows") or {}
    try:
        fresh = (datetime.now() - datetime.fromisoformat(th["time"])).total_seconds() < 2700
    except (KeyError, TypeError, ValueError):
        fresh = False
    if not fresh or not th.get("mode"):
        return None
    mode, op = th["mode"], th.get("operation_status") or ""
    inside = f" It's {_deg(temp)} inside." if temp is not None else ""
    if _windows_open(win):
        until = _clock(win.get("open_until"))
        if mode == "Off":
            return ("Off. Windows open mode is engaged to let fresh air into the house "
                    f"without running cooling or heating, until {until}.{inside}")
        doing = {"Cool": "cooling", "Heat": "heating"}.get(op, "not running")
        return (f"Windows open mode is engaged until {until}. The AC is {doing} right now, "
                f"and it shuts off within half an hour.{inside}")
    cool, heat = th.get("cool_setpoint"), th.get("heat_setpoint")
    if mode == "Off":
        back = (" Windows open mode just ended, so it comes back on within half an hour."
                if win.get("engine_off") else " The system is switched off.")
        return f"Off.{back}{inside}"
    if op == "Cool":
        return f"Cooling.{inside}" + (f" It's set to {_deg(cool)}." if cool else "")
    if op == "Heat":
        return f"Heating.{inside}" + (f" The heat is set to {_deg(heat)}." if heat else "")
    if mode == "Auto" and cool and heat:
        hold = f" It's in auto, holding between {_deg(heat)} and {_deg(cool)}."
    elif mode == "Heat" and heat:
        hold = f" It's set to heat to {_deg(heat)}."
    else:
        hold = f" It's set to cool to {_deg(cool)}." if cool else ""
    return f"Off right now.{inside}{hold}"


def answer(kind: str, data: dict | None = None) -> str:
    data = data if data is not None else current()
    latest = (data or {}).get("latest") or {}
    if not latest:
        return "I can't reach the thermostat right now."
    age = _age(latest)
    temp, sp, hum = latest.get("indoor_temp"), latest.get("current_setpoint"), latest.get("humidity_pct")
    run = latest.get("runtime_pct_30m")
    if kind == "windows_status":
        win = data.get("windows") or {}
        if _windows_open(win):
            return f"Yes, windows open mode is on until {_clock(win.get('open_until'))}."
        return "No, windows open mode is off."
    if kind == "running":
        said = _status(data, temp)
        if said:
            return said
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
