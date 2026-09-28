#!/usr/bin/env python3
"""Regenerate Antigua's UI cue sounds (warm-pluck family).

    python3 audio/generate_cues.py

Writes 48 kHz stereo WAVs next to this file:
  start_listening.wav  — wake word detected ("I'm listening"): a single warm
                         pluck at A5.
  endpoint.wav         — end of speech / now processing: a quick two-note pluck
                         down (A5 -> E5), a "got it, closing" gesture.

The satellite (antigua_satellite.py) plays these via beep_wake() / beep_thinking()
and falls back to a synth beep if the file is missing. Levels are deliberately
low; paplay applies the configured Antigua volume on top.
"""
import numpy as np
import soundfile as sf
from pathlib import Path

SR = 48000
HERE = Path(__file__).parent

# Warm pluck: fundamental + 3 harmonics, each decaying faster than the last.
PARTS = ([1.0, 0.45, 0.22, 0.10], [7, 11, 16, 22])
A5, E5 = 880.00, 659.25


def _t(d):
    return np.linspace(0, d, int(SR * d), endpoint=False)


def _env(n, attack, release):
    e = np.ones(n)
    a = min(int(SR * attack), n // 2)
    r = min(int(SR * release), n // 2)
    e[:a] = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, a))
    e[n - r:] = 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, r))
    return e


def _pluck(freq, dur, decay_scale=1.0, attack=0.004):
    x = _t(dur)
    amps, decays = PARTS
    out = sum(a * np.sin(2 * np.pi * freq * k * x) * np.exp(-d * decay_scale * x)
              for k, (a, d) in enumerate(zip(amps, decays), start=1))
    return out * _env(len(out), attack, min(0.08, dur * 0.4))


def _add(buf, sig, start_s, gain=1.0):
    o = int(SR * start_s)
    if o + len(sig) > len(buf):
        buf = np.concatenate([buf, np.zeros(o + len(sig) - len(buf))])
    buf[o:o + len(sig)] += gain * sig
    return buf


def _room(sig, wet=0.14, decay=9.0, length=0.28):
    n = int(SR * length)
    rng = np.random.default_rng(42)
    ir = rng.standard_normal(n) * np.exp(-decay * np.linspace(0, length, n))
    ir[: int(SR * 0.004)] = 0
    wet_s = np.convolve(sig, ir)[: len(sig)]
    wet_s /= np.max(np.abs(wet_s)) + 1e-9
    return (1 - wet) * sig + wet * wet_s


def _finalize(mono, peak_db, pad_end=0.12):
    mono = np.concatenate([mono, np.zeros(int(SR * pad_end))])
    mono *= 10 ** (peak_db / 20) / (np.max(np.abs(mono)) + 1e-9)
    return np.stack([mono, mono], axis=1)


def start_listening():
    return _finalize(_room(_pluck(A5, 0.5)), peak_db=-16)


def endpoint():
    sig = _add(np.zeros(1), _pluck(A5, 0.16), 0.0, gain=0.85)
    sig = _add(sig, _pluck(E5, 0.40), 0.085)
    return _finalize(_room(sig, decay=10), peak_db=-18)


if __name__ == "__main__":
    for name, fn in (("start_listening", start_listening), ("endpoint", endpoint)):
        sf.write(str(HERE / f"{name}.wav"), fn(), SR, subtype="PCM_16")
        print(f"wrote {name}.wav")
