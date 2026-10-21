#!/usr/bin/env python3
"""Telegram alerts for silent failures on the primary. Run every 5 min from root's
crontab:
  */5 * * * * /usr/bin/python3 <repo>/server/scripts/health_alerts.py >> <repo>/logs/health_alerts.log 2>&1

Checks:
  tts   — local Kokoro (:5500) can synthesize, and antigua-server hasn't been
          quietly falling back to the backup's TTS (on 2026-09-24 every request
          did for 13 hours and nothing looked wrong).
  disk  — /tmp and / below DISK_PCT. A full /tmp broke TTS, apt, logrotate
          and Hermes at once.
  server, whisper, ollama — antigua-server answers /health, whisper.cpp
          answers (else STT silently drops to CPU small.en), and the voice
          model is loaded in Ollama.
  mic   — the kitchen bridge isn't stuck reconnecting (device off or off Wi-Fi).
  satellite — antigua-satellite is running on the Pi (all speech plays there).
  backup — the backup's fallback server and TTS answer; without them a
          primary outage is a total outage (it was off ~32 h unnoticed, 10-14/15).

Alerts once when a check starts failing, reminds every REMIND_S while it
stays failing, and says when it recovers. Sends via `hermes send` (bot token,
no gateway or LLM needed). State lives on /home so this still works when
/tmp is the thing that's full.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE_FILE = REPO / "data" / "health_alerts_state.json"
SERVER_LOG = REPO / "logs" / "server.log"
BRIDGE_LOG = REPO / "logs" / "kitchen-bridge.log"
TTS_URL = "http://127.0.0.1:5500/tts"
SERVER_HEALTH_URL = "http://127.0.0.1:9393/health"
WHISPER_URL = "http://127.0.0.1:8178/"
OLLAMA_PS_URL = "http://127.0.0.1:11434/api/ps"
SATELLITE_SSH = "airplaypi"
BRIDGE_CRASHES_MAX = 3    # reconnects in FALLBACK_WINDOW_MIN before alerting
DISKS = ["/tmp", "/"]
DISK_PCT = 80
FALLBACK_WINDOW_MIN = 10
FALLBACK_MAX = 3          # fallbacks allowed in the window before alerting
REMIND_S = 6 * 3600
TMPDIR = REPO / "data" / "tmp"   # for hermes send's children, never /tmp
HERMES = shutil.which("hermes") or "/root/.local/bin/hermes"   # cron PATH lacks it


def check_tts_probe():
    req = urllib.request.Request(
        TTS_URL, data=json.dumps({"text": "Health check."}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status == 200 and len(resp.read()) > 1000:
                return None
            return f"local TTS returned HTTP {resp.status} with no audio"
    except Exception as exc:
        return f"local TTS synth failed: {exc}"


def _recent_lines(log_path, needle, now, minutes=FALLBACK_WINDOW_MIN):
    """How many lines containing `needle` the log got in the last `minutes`."""
    try:
        with log_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 512_000))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    since = now - timedelta(minutes=minutes)
    n = 0
    for line in lines:
        if needle not in line:
            continue
        try:
            t = datetime.combine(now.date(), datetime.strptime(line[:8], "%H:%M:%S").time())
        except ValueError:
            continue
        if t > now:            # log line from before midnight
            t -= timedelta(days=1)
        if t >= since:
            n += 1
    return n


def check_tts_fallbacks(now):
    """Count 'TTS failed, falling back' lines in the last FALLBACK_WINDOW_MIN."""
    n = _recent_lines(SERVER_LOG, "TTS failed, falling back", now) or 0
    if n > FALLBACK_MAX:
        return f"{n} TTS requests fell back to the backup TTS in the last {FALLBACK_WINDOW_MIN} min"
    return None


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


def check_server():
    try:
        status, _ = _get(SERVER_HEALTH_URL)
        return None if status == 200 else f"/health returned HTTP {status}"
    except Exception as exc:
        return f"antigua-server not answering: {exc}"


def check_whisper():
    try:
        _get(WHISPER_URL)
        return None
    except Exception as exc:
        return f"whisper.cpp (GPU STT) not answering — turns use CPU STT: {exc}"


def check_ollama(model):
    try:
        _, body = _get(OLLAMA_PS_URL)
        loaded = [m.get("name") for m in json.loads(body).get("models", [])]
    except Exception as exc:
        return f"Ollama not answering: {exc}"
    if model and model not in loaded:
        return f"voice model not loaded in Ollama (loaded: {', '.join(loaded) or 'nothing'})"
    return None


def check_mic(now):
    n = _recent_lines(BRIDGE_LOG, "Bridge crashed", now)
    if n is not None and n >= BRIDGE_CRASHES_MAX:
        return f"kitchen bridge reconnected {n} times in {FALLBACK_WINDOW_MIN} min — is the reSpeaker on?"
    return None


def check_satellite():
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", SATELLITE_SSH,
                            "systemctl", "is-active", "antigua-satellite"],
                           capture_output=True, text=True, timeout=20)
    except Exception as exc:
        return f"can't reach the Pi: {exc}"
    state = r.stdout.strip()
    if state == "active":
        return None
    return f"antigua-satellite is {state or 'unreachable'} (ssh: {r.stderr.strip()[:120]})"


def _backup_endpoints():
    """(name, host, port) of the backup's fallback server and TTS, from the
    bridge's fallback_server_url and server.yaml's tts.fallback_url."""
    import yaml
    from urllib.parse import urlsplit
    urls = []
    try:
        bridge = yaml.safe_load((REPO / "kitchen-mic" / "bridge" / "config.yaml").read_text())
        urls.append(("fallback server", bridge.get("fallback_server_url")))
    except Exception:
        pass
    try:
        cfg = yaml.safe_load((REPO / "server" / "config" / "server.yaml").read_text())
        urls.append(("backup TTS", (cfg.get("tts") or {}).get("fallback_url")))
    except Exception:
        pass
    return [(name, u.hostname, u.port) for name, url in urls if url
            for u in [urlsplit(url)] if u.hostname and u.port]


def check_backup():
    down = []
    for name, host, port in _backup_endpoints():
        try:
            socket.create_connection((host, port), timeout=3).close()
        except OSError as exc:
            down.append(f"{name} {host}:{port} ({exc})")
    if down:
        return "no failover — " + "; ".join(down)
    return None


def _voice_model():
    try:
        import yaml
        cfg = yaml.safe_load((REPO / "server" / "config" / "server.yaml").read_text())
        return cfg["ollama"]["model"]
    except Exception:
        return None


def check_disks():
    full = []
    for d in DISKS:
        u = shutil.disk_usage(d)
        pct = 100 * u.used / u.total
        if pct >= DISK_PCT:
            full.append(f"{d} {pct:.0f}% used ({u.free / 2**30:.1f} GB free)")
    return "; ".join(full) or None


def send(text):
    TMPDIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TMPDIR=str(TMPDIR))
    try:
        subprocess.run([HERMES, "send", "-q", "-t", "telegram", text],
                       env=env, timeout=60, check=True, capture_output=True)
        return True
    except Exception as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return False


def main():
    now = datetime.now()
    problems = {}
    tts = [p for p in (check_tts_probe(), check_tts_fallbacks(now)) if p]
    if tts:
        problems["tts"] = "; ".join(tts)
    if disk := check_disks():
        problems["disk"] = disk
    for key, msg in (("server", check_server()), ("whisper", check_whisper()),
                     ("ollama", check_ollama(_voice_model())), ("mic", check_mic(now)),
                     ("satellite", check_satellite()), ("backup", check_backup())):
        if msg:
            problems[key] = msg

    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}

    ts = time.time()
    for key, msg in problems.items():
        last = state.get(key)
        if last is None or ts - last["sent"] >= REMIND_S:
            prefix = "still failing" if last else "problem"
            if send(f"⚠️ Antigua ({key}) {prefix}: {msg}"):
                state[key] = {"sent": ts, "since": last["since"] if last else ts}
        print(f"{key}: {msg}")
    for key in [k for k in state if k not in problems]:
        mins = (ts - state[key]["since"]) / 60
        if send(f"✅ Antigua ({key}) recovered after {mins:.0f} min"):
            del state[key]

    STATE_FILE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
