"""One "TURN" log line per turn with where its time went, so latency can be
read off the log instead of benchmarked stage by stage:

  TURN route=llm total=2.61s first_audio=2.12s | stt=0.59 addressee=0.57 ...

The clock is per thread (each HTTP request runs on its own). A spoken turn
starts it before STT; a text turn starts it in dispatch_text. first_audio is
when the first reply chunk went out on MQTT, or, for a reply the bridge
publishes itself, when the response was ready.
"""

import logging
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("antigua_core")
_local = threading.local()


def start():
    """Start this thread's turn clock, unless one is already running."""
    if getattr(_local, "t0", None) is None:
        _local.t0 = time.perf_counter()
        _local.stages = {}
        _local.first_audio = None
        _local.route = ""


def add(stage: str, seconds: float):
    if getattr(_local, "t0", None) is not None:
        _local.stages[stage] = _local.stages.get(stage, 0.0) + seconds


@contextmanager
def timed(stage: str):
    t = time.perf_counter()
    try:
        yield
    finally:
        add(stage, time.perf_counter() - t)


def set_route(route: str):
    if getattr(_local, "t0", None) is not None:
        _local.route = route


def audio_out():
    """The first reply audio just went out."""
    if getattr(_local, "t0", None) is not None and _local.first_audio is None:
        _local.first_audio = time.perf_counter() - _local.t0


def finish() -> str | None:
    """Log the turn's line and stop the clock. Returns the line (for tests)."""
    t0 = getattr(_local, "t0", None)
    if t0 is None:
        return None
    total = time.perf_counter() - t0
    first = _local.first_audio if _local.first_audio is not None else total
    stages = " ".join(f"{k}={v:.2f}" for k, v in _local.stages.items())
    line = (f"TURN route={_local.route or '-'} total={total:.2f}s first_audio={first:.2f}s"
            + (f" | {stages}" if stages else ""))
    _local.t0 = None
    log.info(line)
    return line
