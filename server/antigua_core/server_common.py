"""Pieces both servers (primary and fallback) need outside the pipeline.

These used to be copied into each server and drifted: the fallback lost the
snoozed-alarm wording, the display-topic gate and the MQTT connect retries.
"""

import logging
import time
from datetime import datetime
from pathlib import Path

from . import settings

log = logging.getLogger("antigua_core")

# Display-only MQTT topics, for the optional wall display. They're dropped
# unless display.enabled is set; playback and alarm topics are never gated.
DISPLAY_TOPICS = frozenset({
    "antigua/status", "antigua/response", "antigua/transcript",
    "antigua/memory", "antigua/command", "antigua/timer_set",
})


def display_allows(topic: str) -> bool:
    return settings.DISPLAY_ENABLED or topic not in DISPLAY_TOPICS


def mqtt_connect(client, host: str, port: int, attempts: int = 5, delay: float = 3.0) -> bool:
    """Connect and start the network loop, retrying a few times at startup
    (the broker's host may still be booting). paho reconnects on its own once
    the first connect succeeds. Returns False if every attempt failed."""
    for attempt in range(1, attempts + 1):
        try:
            client.connect(host, port, keepalive=60)
            client.loop_start()
            log.info("MQTT connected to %s:%d", host, port)
            return True
        except Exception as e:
            log.warning("MQTT connect attempt %d/%d failed: %s", attempt, attempts, e)
            if attempt < attempts:
                time.sleep(delay)
    log.error("MQTT connect failed after %d attempts — continuing without MQTT", attempts)
    return False


def llm_now_str(now: datetime | None = None) -> str:
    """Coarse 'now' for the LLM system prompt: the date and a daypart.

    Exact time and date questions are answered by the time_date skill before
    they reach the LLM. Minute precision made the ~1100-token system message
    unique on every request, which defeated Ollama's prompt-prefix cache and
    cost 0.4-0.6 s of re-evaluation per turn; the hour changes 24x a day.
    """
    now = now or datetime.now()
    hour = now.hour
    part = (
        "late night" if hour < 5 else
        "early morning" if hour < 8 else
        "morning" if hour < 12 else
        "afternoon" if hour < 17 else
        "evening" if hour < 21 else
        "night"
    )
    return f"{now.strftime('%A, %B %d, %Y')}, around {now.strftime('%-I %p')} ({part})"


def timer_fire_text(label: str, kind: str, message: str | None = None) -> str:
    """What Antigua says when a timer, alarm or reminder rings."""
    if kind == "alarm":
        if "snooze" in label:
            return "Time to get up."
        return "Good morning, your alarm is going off." if "wake" in label \
            else "Your alarm is going off."
    if kind == "reminder":
        return f"Reminder: {message}." if message else "Here's your reminder."
    return f"Your {label} is done."


def prune_audio(audio_dir: Path, ttl: float, cache_ttl: float = 0,
                cache_max_bytes: int = 0) -> int:
    """Delete reply WAVs older than ttl seconds. Content-cached cache_*.wav
    files live cache_ttl longer: synthesize() never reuses one older than
    that, and skipping them once grew audio_out to 11 GB. A cache hit touches
    its file, so mtime is last use; past cache_max_bytes (0 = no cap) the
    least recently used cache files go first. Returns the count."""
    now = time.time()
    removed = 0
    cached = []
    for f in audio_dir.glob("*.wav"):
        try:
            st = f.stat()
        except FileNotFoundError:
            continue
        is_cache = f.name.startswith("cache_")
        if now - st.st_mtime > (ttl + cache_ttl if is_cache else ttl):
            f.unlink(missing_ok=True)
            removed += 1
        elif is_cache:
            cached.append((st.st_mtime, st.st_size, f))
    if cache_max_bytes:
        total = sum(size for _, size, _ in cached)
        for _, size, f in sorted(cached, key=lambda c: c[0]):
            if total <= cache_max_bytes:
                break
            f.unlink(missing_ok=True)
            total -= size
            removed += 1
    return removed
