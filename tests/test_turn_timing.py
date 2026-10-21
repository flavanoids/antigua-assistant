#!/usr/bin/env python3
"""The per-turn "TURN" timing line (antigua_core/turn_timing.py): one line
per turn, the stages that ran, and nothing leaking into the next turn.
Stubbed backend, no services.

Run: python3 tests/test_turn_timing.py   (also works under pytest)
"""

import logging
import sys
import time
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))
sys.path.insert(0, str(Path(__file__).parent))

from antigua_core import pipeline, settings, turn_timing  # noqa: E402
from test_pipeline import make_backend  # noqa: E402


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        msg = record.getMessage()
        if msg.startswith("TURN "):
            self.lines.append(msg)


def _capture():
    h = _Lines()
    logging.getLogger("antigua_core").addHandler(h)
    logging.getLogger("antigua_core").setLevel(logging.INFO)
    return h


def test_clock():
    assert turn_timing.finish() is None          # no clock, no line
    turn_timing.start()
    with turn_timing.timed("stt"):
        time.sleep(0.01)
    turn_timing.add("tts", 0.25)
    turn_timing.add("tts", 0.25)                 # repeated stages add up
    turn_timing.set_route("llm")
    line = turn_timing.finish()
    assert line.startswith("TURN route=llm total="), line
    assert "stt=0.01" in line and "tts=0.50" in line, line
    assert turn_timing.finish() is None          # stopped


def test_spoken_llm_turn():
    h = _capture()
    pipeline.init(make_backend("what's a good name for a cat", llm_sentences=("Luna.", "Or Mochi.")))
    pipeline.B.check_addressed = lambda *a, **kw: True
    pipeline.run_pipeline("/nonexistent.wav")
    assert len(h.lines) == 1, h.lines            # one line, though STT started the clock
    line = h.lines[0]
    for stage in ("stt=", "addressee=", "llm_first=", "tts_first=", "tts_rest="):
        assert stage in line, (stage, line)
    assert "route=llm" in line, line


def test_text_turn_and_dismiss():
    h = _capture()
    pipeline.init(make_backend(""))
    pipeline.B.check_addressed = lambda *a, **kw: False
    pipeline.dispatch_text("what's a good name for a cat", quiet=True)
    assert len(h.lines) == 1 and "addressee=" in h.lines[0], h.lines
    assert "llm_first" not in h.lines[0], h.lines   # dismissed before the model


def test_parallel_search_router():
    """router_parallel: the search router runs during the addressee check,
    so the two cost about the slower one, not the sum; a dismissed turn
    never waits for it."""
    def slow(answer):
        def f(*a, **kw):
            time.sleep(0.3)
            return answer
        return f
    settings.SEARCH_ENABLED = True
    settings.SEARCH_ROUTER_PARALLEL = True
    try:
        h = _capture()
        pipeline.init(make_backend(""))
        pipeline.B.check_addressed = slow(True)
        pipeline.B.route_search_with_llm = slow(None)
        t0 = time.time()
        pipeline.dispatch_text("how do bees make honey", quiet=True)
        took = time.time() - t0
        assert took < 0.5, took                    # ~0.3, not ~0.6
        assert "search_router=0.0" in h.lines[-1], h.lines[-1]   # already answered

        pipeline.B.check_addressed = slow(False)   # dismissed: no waiting on the router
        t0 = time.time()
        pipeline.dispatch_text("how do bees make honey", quiet=True)
        assert time.time() - t0 < 0.5
    finally:
        settings.SEARCH_ENABLED = False
        settings.SEARCH_ROUTER_PARALLEL = False


def test_split_long_first_sentence():
    """tts.split_first_over_chars: a long first sentence is spoken as head
    (to a comma) + the rest, and the rest joins the tail utterance."""
    long_first = ("They work because of magnetic fields, which are created by moving "
                  "electric charges inside the atoms of the metal.")
    said = []
    for limit, expect in ((0, [long_first, "Neat, right?"]),
                          (80, ["They work because of magnetic fields,",
                                "which are created by moving electric charges inside the "
                                "atoms of the metal. Neat, right?"])):
        settings.TTS_SPLIT_FIRST_OVER_CHARS = limit
        pipeline.init(make_backend("how do magnets work", llm_sentences=(long_first, "Neat, right?")))
        pipeline.B.check_addressed = lambda *a, **kw: True
        synth = pipeline.B.synthesize
        said.clear()
        pipeline.B.synthesize = lambda text, lang="en", synth=synth: (said.append(text), synth(text, lang))[1]
        r = pipeline.dispatch_text("how do magnets work", quiet=True)
        assert said == expect, (limit, said)
        assert r["response"] == f"{long_first} Neat, right?", r["response"]   # what was said, whole
    settings.TTS_SPLIT_FIRST_OVER_CHARS = 0


def main():
    settings.configure({})
    settings.SEARCH_ENABLED = False
    test_clock()
    test_spoken_llm_turn()
    test_text_turn_and_dismiss()
    test_parallel_search_router()
    test_split_long_first_sentence()
    print("PASS — turn timing suite")


if __name__ == "__main__":
    main()
