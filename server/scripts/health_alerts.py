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

Alerts once when a check starts failing, reminds every REMIND_S while it
stays failing, and says when it recovers. Sends via `hermes send` (bot token,
no gateway or LLM needed). State lives on /home so this still works when
/tmp is the thing that's full.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE_FILE = REPO / "data" / "health_alerts_state.json"
SERVER_LOG = REPO / "logs" / "server.log"
TTS_URL = "http://127.0.0.1:5500/tts"
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


def check_tts_fallbacks(now):
    """Count 'TTS failed, falling back' lines in the last FALLBACK_WINDOW_MIN."""
    try:
        with SERVER_LOG.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 512_000))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    since = now - timedelta(minutes=FALLBACK_WINDOW_MIN)
    n = 0
    for line in lines:
        if "TTS failed, falling back" not in line:
            continue
        try:
            t = datetime.combine(now.date(), datetime.strptime(line[:8], "%H:%M:%S").time())
        except ValueError:
            continue
        if t > now:            # log line from before midnight
            t -= timedelta(days=1)
        if t >= since:
            n += 1
    if n > FALLBACK_MAX:
        return f"{n} TTS requests fell back to the backup TTS in the last {FALLBACK_WINDOW_MIN} min"
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
