#!/usr/bin/env python3
"""Tests for the Kokoro voice shaping: ellipses kept as hesitations, the
misaki pronunciation overrides, sentence splitting and the loudness/EQ polish.

No models: the TTS server module is imported but Kokoro is never loaded.

Run: python3 tests/test_tts_voice.py   (also works under pytest)
"""

import sys
from pathlib import Path

import numpy as np

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

settings.configure({})

import kokoro_tts_server_flask as tts  # noqa: E402
from antigua_core.tts_text import clean_for_tts  # noqa: E402


def test_ellipsis_survives_cleaning():
    assert clean_for_tts("Okay... done.") == "Okay… done."
    assert clean_for_tts("Hmm… let me check.") == "Hmm… let me check."
    assert clean_for_tts("See you then..") == "See you then."
    assert clean_for_tts("Bueno... ya está.", "es") == "Bueno… ya está."


def test_misaki_overrides_respellings():
    sub = lambda t: tts._MISAKI_SAY_RE.sub(lambda m: f"[{m.group(0)}]", t)
    assert sub("Good morning, No-ee.") == "Good morning, [No-ee]."
    assert sub("This is Antigwa.") == "This is [Antigwa]."
    assert sub("No-eel Antigwan") == "No-eel Antigwan"   # whole words only


def test_sentence_split():
    split = tts._SENTENCE_RE.split
    assert split("It's done! Want another? Okay.") == ["It's done!", "Want another?", "Okay."]
    assert split("Hmm… I'm not sure.") == ["Hmm…", "I'm not sure."]
    assert split("Seventy-two degrees.") == ["Seventy-two degrees."]


def test_polish_levels_and_keeps_length():
    t = np.arange(tts.SAMPLE_RATE) / tts.SAMPLE_RATE
    for amp in (0.02, 0.5):
        x = (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        y = tts._polish(x)
        assert len(y) == len(x) and y.dtype == np.float32
        assert abs(20 * np.log10(np.sqrt(np.mean(y ** 2))) - tts.TARGET_RMS_DB) < 0.1
    assert len(tts._polish(np.zeros(0, dtype=np.float32))) == 0


def test_polish_cuts_highs_not_lows():
    t = np.arange(tts.SAMPLE_RATE) / tts.SAMPLE_RATE
    lo, hi = np.sin(2 * np.pi * 300 * t), np.sin(2 * np.pi * 10000 * t)
    y = tts._polish((0.1 * (lo + hi)).astype(np.float32))
    spec = np.abs(np.fft.rfft(y))
    ratio_db = 20 * np.log10(spec[10000] / spec[300])   # 1 s → 1 Hz bins
    assert -3.5 < ratio_db < -2.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
