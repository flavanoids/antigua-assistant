"""Read-only system awareness: is the internet up (netstatus), when it last
went out, and how this server is doing — load, memory, disks, temperatures,
the GPU, uptime, and whether Antigua's services and the other boxes answer.

Everything here only reads: /proc, sysfs, `systemctl is-active` on a fixed
list, TCP connects to fixed addresses. Nothing from the transcript ever
reaches a command, and there is no restart, reboot or delete.
"""

import os
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import netstatus, settings

# Local units that make Antigua work (the server itself is answering).
SERVICES = {
    "antigua-tts": "the voice",
    "antigua-whisper": "speech recognition",
    "ollama": "the language model",
    "kitchen-bridge": "the kitchen mic",
}
DISKS = {"/": "the main disk", "/tmp": "the temp disk", "/home": "the home disk"}
_DISK_WARN = 90
_CPU_HOT = 80
_GPU_HOT = 85


def _clock(ts: float) -> str:
    t = datetime.fromtimestamp(ts)
    s = f"{t:%-I:%M %p}".replace(":00 ", " ")
    if t.date() == datetime.now().date():
        return s
    if (datetime.now().date() - t.date()).days == 1:
        return f"{s} yesterday"
    return f"{s} on {t:%A}"


def duration_words(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    if m < 60:
        return f"{m} minute{'s' * (m != 1)}"
    h, m = divmod(m, 60)
    if h >= 48:
        return f"{round(h / 24)} days"
    hs = f"{h} hour{'s' * (h != 1)}"
    return f"{hs} and {m} minute{'s' * (m != 1)}" if m else hs


# ── the internet ─────────────────────────────────────────────────────────────

def net_answer(kind: str = "status") -> str:
    mon = netstatus.monitor
    if mon is None:
        return "I'm not watching the internet connection right now."
    if kind == "outages":
        return _outages_answer(mon)
    if mon.state == "offline":
        return (f"No, the internet's been down since {_clock(mon.since)}. "
                f"It looks like {netstatus.CAUSES[mon.cause]}. "
                "Timers, alarms, the TV, most lights and basic questions still work.")
    if mon.state == "unknown":
        return "I'm still checking the connection. Ask me again in a few seconds."
    web = mon.last.get("web_s")
    if mon.state == "degraded":
        say = f"We're online, but it's slow: a simple web request took {web:.0f} seconds."
    elif kind == "why":
        say = "The internet's working fine right now."
    else:
        say = "Yes, the internet's working."
    recent = [o for o in mon.outages if o.get("end") and time.time() - o["end"] < 12 * 3600]
    if recent:
        o = recent[-1]
        say += (f" It was out for {duration_words(o['end'] - o['start'])} earlier, "
                f"from {_clock(o['start'])} to {_clock(o['end'])}.")
    return say


def offline_phrase() -> str:
    """"The internet's been down since 4:02 PM." — for skills that need it."""
    mon = netstatus.monitor
    return f"The internet's been down since {_clock(mon.since)}." if mon else "The internet's down."


def offline_hint() -> str:
    """A per-turn line for the model while the internet is down."""
    since = f" since {_clock(netstatus.monitor.since)}" if netstatus.monitor else ""
    return (f"The internet has been down{since}, so you can't look anything up. If they ask "
            "for news, scores, prices, traffic or anything current, say you can't check "
            "until it's back; don't guess.")


def _outages_answer(mon) -> str:
    if mon.state == "offline":
        return f"It's down right now, since {_clock(mon.since)}: {netstatus.CAUSES[mon.cause]}."
    week = [o for o in mon.outages if o.get("end") and time.time() - o["start"] < 7 * 86400]
    if not week:
        return "The internet hasn't gone out in the last week, as far as I've seen."
    o = week[-1]
    say = (f"The last outage was from {_clock(o['start'])} to {_clock(o['end'])}, "
           f"about {duration_words(o['end'] - o['start'])}. {_cap(netstatus.CAUSES[o['cause']])}.")
    if len(week) > 1:
        total = sum(x["end"] - x["start"] for x in week)
        say += f" That's {len(week)} outages this week, {duration_words(total)} in all."
    return say


# ── this server ──────────────────────────────────────────────────────────────

def _meminfo() -> dict[str, int]:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        out[k] = int(v.split()[0]) * 1024
    return out


def _read(path) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def cpu_temp() -> float | None:
    """The CPU package temperature (coretemp / k10temp), in °C."""
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        if _read(hw / "name") in ("coretemp", "k10temp", "zenpower"):
            t = _read(hw / "temp1_input")
            if t:
                return int(t) / 1000
    return None


def gpu() -> dict | None:
    """AMD GPU busy %, VRAM and temperature from sysfs (amdgpu)."""
    for dev in sorted(Path("/sys/class/drm").glob("card[0-9]/device")):
        busy = _read(dev / "gpu_busy_percent")
        if busy is None:
            continue
        used, total = _read(dev / "mem_info_vram_used"), _read(dev / "mem_info_vram_total")
        temp = next((int(t) / 1000 for t in (_read(p) for p in dev.glob("hwmon/hwmon*/temp1_input")) if t), None)
        return {"busy": int(busy), "temp": temp,
                "vram_pct": round(100 * int(used) / int(total)) if used and total else None}
    return None


def stats() -> dict:
    load1, _, load15 = os.getloadavg()
    mem = _meminfo()
    disks = {}
    for path, name in DISKS.items():
        try:
            du = shutil.disk_usage(path)
            disks[path] = {"name": name, "pct": round(100 * du.used / du.total),
                           "free_gb": round(du.free / 1e9)}
        except OSError:
            pass
    return {"load1": load1, "load15": load15, "cpus": os.cpu_count() or 1,
            "mem_pct": round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"])),
            "mem_total_gb": round(mem["MemTotal"] / 2**30),
            "disks": disks, "cpu_temp": cpu_temp(), "gpu": gpu(),
            "uptime": float(Path("/proc/uptime").read_text().split()[0])}


def services_down() -> list[str]:
    down = []
    for unit, name in SERVICES.items():
        try:
            r = subprocess.run(["systemctl", "is-active", "--quiet", unit], timeout=3)
            if r.returncode != 0:
                down.append(name)
        except (OSError, subprocess.TimeoutExpired):
            pass
    for p in settings.SYSINFO_PEERS:      # other boxes, from server.yaml's sysinfo: block
        try:
            socket.create_connection((p["host"], int(p["port"])), timeout=1.5).close()
        except (OSError, KeyError, ValueError):
            down.append(p.get("name", p.get("host", "a server")))
    return down


def _load_word(s) -> str:
    per = s["load1"] / s["cpus"]
    return "light" if per < 0.3 else "moderate" if per < 0.8 else "heavy"


def _problems(s, down) -> list[str]:
    p = [f"{n} isn't answering" for n in down]
    p += [f"{d['name']} is {d['pct']} percent full" for d in s["disks"].values() if d["pct"] >= _DISK_WARN]
    if s["cpu_temp"] and s["cpu_temp"] >= _CPU_HOT:
        p.append(f"the CPU is running hot at {round(s['cpu_temp'])} degrees")
    g = s["gpu"]
    if g and g["temp"] and g["temp"] >= _GPU_HOT:
        p.append(f"the GPU is running hot at {round(g['temp'])} degrees")
    if s["load1"] / s["cpus"] >= 1.5:
        p.append("the processor is badly overloaded")
    if netstatus.monitor and netstatus.monitor.state == "offline":
        p.append("the internet is down")
    return p


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"


def system_answer(kind: str = "summary") -> str:
    s = stats()
    if kind == "load":
        return (f"The processor load is {_load_word(s)}: {s['load1']:.1f} over the last minute "
                f"across {s['cpus']} cores.")
    if kind == "memory":
        return f"Memory is {s['mem_pct']} percent used, of {s['mem_total_gb']} gigabytes."
    if kind == "disk":
        parts = [f"{d['name']} is {d['pct']} percent full with {d['free_gb']} gigabytes free"
                 for d in s["disks"].values()]
        return _cap(_join(parts)) + "."
    if kind == "temp":
        parts = []
        if s["cpu_temp"]:
            parts.append(f"the CPU is {round(s['cpu_temp'])} degrees")
        if s["gpu"] and s["gpu"]["temp"]:
            parts.append(f"the GPU is {round(s['gpu']['temp'])} degrees")
        return (_cap(_join(parts)) + " Celsius.") if parts else "I can't read the temperatures."
    if kind == "gpu":
        g = s["gpu"]
        if not g:
            return "I can't read the graphics card."
        say = f"The GPU is {g['busy']} percent busy"
        if g["vram_pct"] is not None:
            say += f", with {g['vram_pct']} percent of its memory in use"
        return say + (f", at {round(g['temp'])} degrees." if g["temp"] else ".")
    if kind == "uptime":
        return f"The server has been up for {duration_words(s['uptime'])}."
    down = services_down()
    if kind == "services":
        return (f"Not quite: {_join(down)} {'is' if len(down) == 1 else 'are'} not answering."
                if down else "Yes, everything's running: " + _join(
                    list(SERVICES.values()) + [p.get("name", p["host"]) for p in settings.SYSINFO_PEERS]) + ".")
    problems = _problems(s, down)
    head = (f"Heads up: {_join(problems)}." if problems else "The server's healthy.")
    root = s["disks"].get("/")
    return (f"{head} Load is {_load_word(s)}, memory is {s['mem_pct']} percent used"
            + (f", the main disk is {root['pct']} percent full" if root else "")
            + f", and it's been up {duration_words(s['uptime'])}.")
