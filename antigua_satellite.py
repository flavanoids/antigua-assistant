#!/usr/bin/env python3
"""Antigua Voice Assistant Satellite — runs on the satellite Pi

Audio-output only: TTS/alarm playback (paplay), AirPlay (shairport-sync),
and MQTT. Voice input (wake word + STT capture) was retired from this file
2026-09-21 — it now comes from the kitchen-mic reSpeaker Lite bridge running
on the primary (kitchen-mic/bridge/kitchen_bridge.py), which does its own wake
detection against Antigua's shared "alexa" openWakeWord model and POSTs
straight to /pipeline. See kitchen-mic/README.md.
"""

import io
import json
import logging
import math
import os
import queue
import re
import signal
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from threading import Event, Lock, Thread, Timer

import paho.mqtt.client as mqtt
import requests

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config" / "satellite.yaml"
AUDIO_DIR = Path(__file__).parent / "audio"
SOUND_SLEEP = AUDIO_DIR / "acknowledge.wav"
CUE_CACHE_DIR = AUDIO_DIR / "cues"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("antigua-sat")

# Defaults — real hosts come from config/satellite.yaml
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 9393
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
ALARM_SOUND = Path(__file__).parent / "audio_in" / "alarm_clock.ogg"
# PulseAudio sink Antigua and shairport-sync share (satellite.output_device).
# This default is a HiFiBerry DAC+ on a Pi; `pactl list short sinks` lists yours.
_PA_SINK = "alsa_output.platform-soc_sound.stereo-fallback"
ANTIGUA_VOLUME_PCT = 70          # Antigua's own speaking level (0-100). Per-stream
                                 # volume, independent of the shairport-sync / AirPlay
                                 # sink volume that iOS controls.


def _antigua_vol_args():
    """paplay args so Antigua speaks at ANTIGUA_VOLUME_PCT of full scale as a
    per-stream (sink-input) volume. Does NOT change the sink volume that
    shairport-sync / AirPlay uses."""
    pct = max(0, min(100, ANTIGUA_VOLUME_PCT))
    return ["--volume", str(round(65536 * pct / 100))]


# PinedaDisplay integration — Antigua pushes its state (wake / listening /
# speaking / transcript / audio level) to the Pi's card display over MQTT.
# Off by default; set display.enabled: true in satellite.yaml to turn it on.
# Functional MQTT (antigua/play, antigua/alarm, alarm_ack, shairport-sync's own topics) is
# never gated by this. antigua/done used to be display-only too, but the
# kitchen-mic bridge now subscribes to it as a real playback-finished signal
# (arms its follow-up window / un-mutes the mic), so it can't be gated behind
# display.enabled anymore.
DISPLAY_ENABLED = False
_DISPLAY_TOPICS = frozenset({
    "antigua/wake", "antigua/status", "antigua/response",
    "antigua/audio_level",
})

# Fallback server — activated when the primary goes offline
FALLBACK_HOST = "127.0.0.1"
FALLBACK_PORT = 9394
_HEALTH_CHECK_INTERVAL = 20      # seconds between primary probes
_FALLBACK_NOTIFY_TIMEOUT = 5     # seconds to wait for /activate or /deactivate

_primary_up = True               # optimistic: assume primary is up at boot
_server_lock = Lock()


def load_config():
    global SERVER_HOST, SERVER_PORT, MQTT_BROKER, MQTT_PORT
    global ANTIGUA_VOLUME_PCT, DISPLAY_ENABLED, _PA_SINK
    global FALLBACK_HOST, FALLBACK_PORT, _HEALTH_CHECK_INTERVAL

    if not CONFIG_PATH.exists():
        return

    import yaml
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    if "server" in cfg:
        SERVER_HOST = cfg["server"].get("host", SERVER_HOST)
        SERVER_PORT = cfg["server"].get("port", SERVER_PORT)
        FALLBACK_HOST = cfg["server"].get("fallback_host", FALLBACK_HOST)
        FALLBACK_PORT = cfg["server"].get("fallback_port", FALLBACK_PORT)
        _HEALTH_CHECK_INTERVAL = cfg["server"].get("health_check_interval", _HEALTH_CHECK_INTERVAL)
    if "mqtt" in cfg:
        MQTT_BROKER = cfg["mqtt"].get("broker", MQTT_BROKER)
        MQTT_PORT = cfg["mqtt"].get("port", MQTT_PORT)
    if "display" in cfg:
        DISPLAY_ENABLED = cfg["display"].get("enabled", DISPLAY_ENABLED)
    if "satellite" in cfg:
        s = cfg["satellite"]
        ANTIGUA_VOLUME_PCT = s.get("antigua_volume_pct", ANTIGUA_VOLUME_PCT)
        _PA_SINK = s.get("output_device", _PA_SINK)
        global _ALARM_RING_S
        _ALARM_RING_S = float(s.get("alarm_ring_seconds", _ALARM_RING_S))


# ── MQTT ─────────────────────────────────────────────────────────────────────

mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="antigua-satellite")


def _on_mqtt_message(client, userdata, msg):
    if msg.topic == "antigua/play":
        try:
            data = json.loads(msg.payload)
            audio_url = data.get("audio_url", "")
            if audio_url:
                _play_queue.put(audio_url)
        except Exception as e:
            log.error(f"antigua/play error: {e}")

    elif msg.topic == "antigua/cue":
        # UI cues (e.g. the kitchen bridge's wake chime) bypass the serial
        # play queue: they must sound the instant they're published, not
        # after whatever is queued, and they don't emit antigua/done.
        try:
            audio_url = json.loads(msg.payload).get("audio_url", "")
            if audio_url:
                Thread(target=_play_cue, args=(audio_url,), daemon=True).start()
        except Exception as e:
            log.error(f"antigua/cue error: {e}")

    elif msg.topic == "antigua/listening":
        try:
            active = bool(json.loads(msg.payload).get("active"))
            Thread(target=_set_duck, args=(active,), daemon=True).start()
        except Exception as e:
            log.error(f"antigua/listening error: {e}")

    elif msg.topic == "antigua/alarm":
        try:
            data = json.loads(msg.payload)
            audio_url = data.get("audio_url", "")
            label = data.get("label", "timer")
            if audio_url:
                # Multiple concurrent alarms are supported; each gets its own thread
                Thread(target=_alarm_loop, args=(audio_url, label), daemon=True).start()
        except Exception as e:
            log.error(f"antigua/alarm error: {e}")


def _on_mqtt_connect(client, userdata, flags, rc, props):
    # Subscribe on every (re)connect: the session is clean, so a broker
    # restart drops subscriptions and paho's auto-reconnect won't restore
    # them — the satellite would stay up but never play anything again.
    for topic in ("antigua/play", "antigua/alarm", "antigua/cue", "antigua/listening"):
        client.subscribe(topic)


def mqtt_connect():
    mqtt_client.on_message = _on_mqtt_message
    mqtt_client.on_connect = _on_mqtt_connect
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()
    log.info(f"MQTT connected to {MQTT_BROKER}:{MQTT_PORT}")


def mqtt_publish(topic: str, payload: dict):
    # PinedaDisplay state topics are suppressed unless display.enabled is set.
    if topic in _DISPLAY_TOPICS and not DISPLAY_ENABLED:
        return
    mqtt_client.publish(topic, json.dumps(payload), qos=1)


# ── Music ducking ───────────────────────────────────────────────────────────
# While the kitchen mic hears a command (MQTT antigua/listening, true from the
# wake word until the bridge goes idle), turn the AirPlay stream down right
# here in PulseAudio: ~50ms. Ducking through Music Assistant + RAOP took
# ~1.5s, so the start of every command was buried in music ("Pause" was
# transcribed "Follow us"). Shairport applies the AirPlay/MA volume inside
# itself and leaves its sink-input at 100%, so this doesn't fight MA's level.
DUCK_APP = "Shairport Sync"
DUCK_PCT = 10            # % of the stream's level while ducked
DUCK_SAFETY_S = 60       # restore anyway if the "stopped" message is lost

_duck_lock = Lock()
_duck_restore = None     # stream volume % to restore; None = not ducked
_duck_timer = None


def _duck_inputs():
    """[(sink-input index, volume %)] for every shairport-sync stream."""
    out = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True,
                         text=True, timeout=3).stdout
    found, idx, vol = [], None, None
    for line in out.splitlines():
        m = re.match(r"Sink Input #(\d+)", line)
        if m:
            idx, vol = m.group(1), None
            continue
        m = re.match(r"\s*Volume: .*?/\s*(\d+)%", line)
        if m:
            vol = int(m.group(1))
        elif idx and f'application.name = "{DUCK_APP}"' in line:
            found.append((idx, vol))
    return found


def _set_duck(active: bool):
    global _duck_restore, _duck_timer
    with _duck_lock:
        try:
            inputs = _duck_inputs()
            if active:
                if _duck_restore is None:
                    _duck_restore = max((v for _, v in inputs if v), default=100)
                level = f"{round(_duck_restore * DUCK_PCT / 100)}%"
                if _duck_timer:
                    _duck_timer.cancel()
                _duck_timer = Timer(DUCK_SAFETY_S, _set_duck, args=(False,))
                _duck_timer.daemon = True
                _duck_timer.start()
            elif _duck_restore is not None:
                # Every current stream, not just the ones ducked: a new song
                # can start a new stream mid-duck, and module-stream-restore
                # hands it the ducked level.
                level, _duck_restore = f"{_duck_restore}%", None
                if _duck_timer:
                    _duck_timer.cancel()
                    _duck_timer = None
            else:
                return
            for idx, _ in inputs:
                subprocess.run(["pactl", "set-sink-input-volume", idx, level], timeout=3)
            log.info(f"Music {'ducked' if active else 'restored'} to {level} ({len(inputs)} stream(s))")
        except Exception as e:
            log.warning(f"Music duck failed: {e}")


# ── Audio Playback ──────────────────────────────────────────────────────────

# Serial play queue — MQTT antigua/play chunks are enqueued here and played one at a time
_play_queue: queue.Queue = queue.Queue()


def _play_queue_worker():
    while True:
        audio_url = _play_queue.get()
        try:
            if audio_url:
                stream_and_play(audio_url)
                # A short breath between response chunks so a multi-part answer
                # doesn't run together. Skipped after the final chunk.
                if not _play_queue.empty():
                    time.sleep(0.18)
                else:
                    # Queue drained — the response has fully finished playing.
                    # Lets downstream mics (e.g. the kitchen bridge) know when
                    # it's safe to open a follow-up listening window.
                    mqtt_publish("antigua/done", {"state": "complete"})
        except Exception as e:
            log.error(f"Play queue worker error: {e}")
        finally:
            _play_queue.task_done()


Thread(target=_play_queue_worker, daemon=True, name="play-queue").start()

# ── Beep synthesis ───────────────────────────────────────────────────────────

def _play_beep(tones, volume=0.25):
    """Play a sequence of (freq_hz, duration_ms) tones inline via aplay.
    Use freq=0 for silence gaps.
    """
    sr = 16000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        for freq, dur_ms in tones:
            n = int(sr * dur_ms / 1000)
            for i in range(n):
                if freq:
                    val = int(32767 * volume * math.sin(2 * math.pi * freq * i / sr))
                else:
                    val = 0
                wf.writeframes(struct.pack("<h", val))
    tmp = tempfile.mktemp(suffix=".wav", dir="/tmp")
    with open(tmp, "wb") as f:
        f.write(buf.getvalue())
    subprocess.run(
        ["paplay", "--device", _PA_SINK, *_antigua_vol_args(), tmp],
        capture_output=True
    )
    try:
        os.unlink(tmp)
    except OSError:
        pass


def beep_sleep():
    """Mycroft acknowledge sound — conversation ended."""
    if SOUND_SLEEP.exists():
        play_audio(str(SOUND_SLEEP))
    else:
        _play_beep([(880, 100), (0, 40), (660, 100), (0, 40), (440, 180)])


def beep_alarm():
    """Three-pulse alarm pattern — non-obnoxious but attention-getting."""
    _play_beep([(880, 180), (0, 120), (880, 180), (0, 120), (880, 180)])


# ── Volume control ────────────────────────────────────────────────────────────

_VOL_STEP_PCT = 10  # ±10% per voice command

def _get_pa_volume() -> int:
    """Read current volume as percentage from PulseAudio sink."""
    result = subprocess.run(
        ["pactl", "get-sink-volume", _PA_SINK],
        capture_output=True, text=True,
    )
    # Output: "Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB"
    m = re.search(r"(\d+)%", result.stdout)
    return int(m.group(1)) if m else 100

def set_volume(direction: str):
    """Adjust PulseAudio sink volume (works with shairport PA backend)."""
    current = _get_pa_volume()
    new_pct = min(100, current + _VOL_STEP_PCT) if direction == "volume_up" else max(0, current - _VOL_STEP_PCT)
    subprocess.run(["pactl", "set-sink-volume", _PA_SINK, f"{new_pct}%"], capture_output=True)
    log.info(f"Volume {direction}: {current}% -> {new_pct}%")


# ── Alarm loop ───────────────────────────────────────────────────────────────

_alarm_stops: dict[str, Event] = {}
_alarm_ringing_labels: set[str] = set()


_ALARM_RING_S = 5.0  # mic sits next to the speaker — wake word is unreliable while ringing


def _alarm_loop(audio_url: str, label: str):
    stop_event = Event()
    _alarm_stops[label] = stop_event
    _alarm_ringing_labels.add(label)
    log.info(f"Alarm ringing: {label} — auto-stops after {_ALARM_RING_S:.0f}s (or wake word)")

    # Announce once with TTS voice, then ring the bell for a fixed window
    download_and_play(audio_url)

    proc = None
    if ALARM_SOUND.exists():
        proc = subprocess.Popen(
            ["paplay", "--device", _PA_SINK, str(ALARM_SOUND)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        beep_alarm()

    end_time = time.time() + _ALARM_RING_S
    while not stop_event.is_set() and time.time() < end_time:
        stop_event.wait(timeout=0.2)

    if proc is not None and proc.poll() is None:
        proc.terminate()

    _alarm_ringing_labels.discard(label)
    _alarm_stops.pop(label, None)
    log.info(f"Alarm stopped: {label}")
    mqtt_publish("antigua/alarm_ack", {"label": label})


def play_audio(wav_path: str, blocking: bool = True):
    """Play a WAV file through the DAC+."""
    if not wav_path or not Path(wav_path).exists():
        log.warning(f"Cannot play audio: file not found ({wav_path})")
        return

    # Use paplay (PulseAudio) to avoid ALSA device-busy conflicts with shairport-sync
    cmd = ["paplay", "--device", _PA_SINK, *_antigua_vol_args(), wav_path]
    log.info(f"Playing: {wav_path}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    if blocking:
        proc.wait()
        if proc.returncode != 0:
            log.error(f"paplay failed: {proc.stderr.read().decode()}")
    else:
        return proc


# ── Server Communication ────────────────────────────────────────────────────

def _probe_primary() -> bool:
    try:
        r = requests.get(f"http://{SERVER_HOST}:{SERVER_PORT}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _health_check_loop():
    global _primary_up
    while running:
        time.sleep(_HEALTH_CHECK_INTERVAL)
        up = _probe_primary()
        with _server_lock:
            was_up = _primary_up
            _primary_up = up

        if was_up and not up:
            log.warning("Primary offline — activating the backup's fallback server")
            mqtt_publish("antigua/status", {"state": "fallback_active"})
            try:
                requests.post(
                    f"http://{FALLBACK_HOST}:{FALLBACK_PORT}/activate",
                    timeout=_FALLBACK_NOTIFY_TIMEOUT,
                )
                log.info("Fallback /activate sent — 0.8b model pre-warming")
            except Exception as e:
                log.warning("Could not notify fallback server: %s", e)

        elif not was_up and up:
            log.info("Primary restored — deactivating the fallback server")
            mqtt_publish("antigua/status", {"state": "primary_restored"})
            try:
                requests.post(
                    f"http://{FALLBACK_HOST}:{FALLBACK_PORT}/deactivate",
                    timeout=_FALLBACK_NOTIFY_TIMEOUT,
                )
            except Exception as e:
                log.warning("Could not notify fallback server of restore: %s", e)


def _parse_wav_header(data: bytes):
    """Parse a WAV header and return (rate, fmt_str, channels, pcm_offset) or None."""
    import struct
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    # Find fmt chunk
    fmt_offset = None
    fmt_size = 0
    i = 12
    while i < len(data) - 8:
        chunk_id = data[i:i + 4]
        chunk_size = struct.unpack("<I", data[i + 4:i + 8])[0]
        if chunk_id == b"fmt ":
            fmt_offset = i + 8
            fmt_size = chunk_size
            break
        i += 8 + chunk_size
        if i % 2:
            i += 1
    if fmt_offset is None or fmt_size < 16:
        return None
    nchannels = struct.unpack("<H", data[fmt_offset + 2:fmt_offset + 4])[0]
    rate = struct.unpack("<I", data[fmt_offset + 4:fmt_offset + 8])[0]
    bits = struct.unpack("<H", data[fmt_offset + 14:fmt_offset + 16])[0]
    # Find data chunk
    pcm_offset = None
    i = 12
    while i < len(data) - 8:
        chunk_id = data[i:i + 4]
        chunk_size = struct.unpack("<I", data[i + 4:i + 8])[0]
        if chunk_id == b"data":
            pcm_offset = i + 8
            break
        i += 8 + chunk_size
        if i % 2:
            i += 1
    if pcm_offset is None:
        return None
    fmt_str = {8: "u8", 16: "s16le", 24: "s24le", 32: "s32le"}.get(bits, "s16le")
    return rate, fmt_str, nchannels, pcm_offset


def stream_and_play(audio_url: str):
    """Stream audio directly to paplay without saving to disk.
    Parses the WAV header, then pipes raw PCM to paplay --raw so playback
    starts while bytes are still arriving.
    """
    if not audio_url:
        log.warning("No audio URL in response")
        return

    log.info(f"Streaming response audio: {audio_url}")
    try:
        resp = requests.get(audio_url, stream=True, timeout=10)
        resp.raise_for_status()

        # Read enough to parse WAV header (usually 44 bytes, allow some slack)
        buf = b""
        for chunk in resp.iter_content(1024):
            buf += chunk
            if len(buf) >= 512:
                break

        parsed = _parse_wav_header(buf)
        if parsed is None:
            log.warning("Could not parse WAV header, falling back to download-and-play")
            return download_and_play(audio_url)

        rate, fmt_str, nchannels, pcm_offset = parsed

        cmd = [
            "paplay",
            "--device", _PA_SINK,
            *_antigua_vol_args(),
            "--rate", str(rate),
            "--format", fmt_str,
            "--channels", str(nchannels),
            "--raw",
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        # Write PCM data that arrived with the header
        proc.stdin.write(buf[pcm_offset:])

        # Stream remaining chunks directly to paplay
        for chunk in resp.iter_content(chunk_size=4096):
            proc.stdin.write(chunk)

        proc.stdin.close()
        proc.wait()
        if proc.returncode != 0:
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            log.error(f"paplay streaming failed: {stderr}")
    except Exception as e:
        log.error(f"Streaming playback error: {e}")
        try:
            download_and_play(audio_url)
        except Exception as e2:
            log.error(f"Fallback download-and-play also failed: {e2}")


def _play_cue(audio_url: str):
    """Play a short UI cue from a local cache — fetched from the server once,
    then never over HTTP again, so it can't be delayed by the network."""
    path = CUE_CACHE_DIR / Path(audio_url).name
    if not path.exists():
        resp = requests.get(audio_url, timeout=10)
        resp.raise_for_status()
        CUE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    play_audio(str(path), blocking=False)


def download_and_play(audio_url: str):
    """Download audio from URL and play it through speakers."""
    if not audio_url:
        log.warning("No audio URL in response")
        return

    log.info(f"Downloading response audio: {audio_url}")
    try:
        resp = requests.get(audio_url, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Failed to download audio: {e}")
        return

    tmp_path = tempfile.mktemp(suffix=".wav", dir="/tmp")
    with open(tmp_path, "wb") as f:
        f.write(resp.content)

    play_audio(tmp_path, blocking=True)

    try:
        Path(tmp_path).unlink()
    except OSError:
        pass


# ── Main Satellite Loop ────────────────────────────────────────────────────

running = True


def signal_handler(sig, frame):
    global running
    log.info("Shutting down...")
    running = False


def main():
    load_config()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Background health check: probes primary every 20s, triggers failover/restore
    Thread(target=_health_check_loop, daemon=True).start()
    log.info("Failover health check started (primary=%s:%d, fallback=%s:%d, interval=%ds)",
             SERVER_HOST, SERVER_PORT, FALLBACK_HOST, FALLBACK_PORT, _HEALTH_CHECK_INTERVAL)

    log.info("=== Antigua Satellite Starting (audio output only — voice input via kitchen-mic bridge) ===")

    # Set a known-good volume on the PulseAudio sink before AirPlay connects.
    # iOS volume control (via shairport-sync PA backend) will adjust this once a
    # session starts; this just ensures a predictable level on boot.
    subprocess.run(
        ["pactl", "set-sink-volume", _PA_SINK, "70%"],
        capture_output=True,
    )
    log.info("Volume initialized to 70% on PA sink")
    # A crash mid-duck would leave module-stream-restore holding the ducked
    # level for every future AirPlay stream; start from full.
    global _duck_restore
    _duck_restore = 100
    _set_duck(False)

    log.info(f"Server:            http://{SERVER_HOST}:{SERVER_PORT}")
    log.info(f"PinedaDisplay:     {'enabled' if DISPLAY_ENABLED else 'disabled'}")

    mqtt_connect()
    mqtt_publish("antigua/status", {"state": "ready", "satellite": "online"})
    log.info("Satellite ready — playback, AirPlay, and alarms only. "
             "Wake word + STT now handled by the kitchen-mic bridge on the primary.")

    try:
        while running:
            time.sleep(1)
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
    finally:
        mqtt_publish("antigua/status", {"state": "offline"})
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        log.info("Satellite shut down.")


if __name__ == "__main__":
    main()