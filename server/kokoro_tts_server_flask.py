#!/usr/bin/env python3
"""Lightweight Kokoro TTS server using kokoro-onnx (ONNX runtime, no PyTorch).

Serves POST /tts with {"text": "...", "speed": 0.9, "lang": "en"} -> WAV bytes.
lang is "en" (default) or "es".
Runs on port 5500 on both the primary (main TTS, localhost only) and the
backup box (backup for the primary, and TTS for the fallback server).

Voice is a fixed blend ("Antigua"): mostly af_bella + af_heart for a warm,
even delivery, with ef_dora mixed in to give the English a Latin-American
Spanish inflection. Spanish replies flip the proportions (mostly ef_dora, some
bella/heart) so she still sounds like herself, with Spanish phonemes. Blend
weights and the default speed live in BLENDS / SPEED below — the primary
server can override speed per request.

English goes through misaki (Kokoro's own G2P, dictionary + POS-aware) when
it's installed, else espeak; Spanish always uses espeak es-419. Replies are
spoken sentence by sentence with a short gap, then given a gentle high-shelf
cut and a fixed loudness (see _polish).
"""
import io
import os
import re
import threading
import time
from pathlib import Path

import numpy as np
import onnxruntime as rt
import soundfile as sf
from flask import Flask, request, Response

from kokoro_onnx import Kokoro
from phonemizer.backend import EspeakBackend
from phonemizer.phonemize import _phonemize
from phonemizer.separator import default_separator

app = Flask(__name__)

MODELS_DIR = Path(__file__).parent / "models"   # overridden by --models-dir

# "Antigua" voice: bella/heart warmth with a ~25% ef_dora accent pull; for
# Spanish, ef_dora leads (an English voice reading Spanish sounds foreign).
BLENDS = {
    "en": {"ef_dora": 0.25, "af_bella": 0.45, "af_heart": 0.30},   # kept after 10-18 A/B
    "es": {"ef_dora": 0.70, "af_bella": 0.20, "af_heart": 0.10},
}
# es-419 = Latin American Spanish: seseo ("cinco" → sinko) and yeísmo. Plain
# "es" is Castilian ("θinko"), a mismatch for ef_dora, a Latin American voice.
ESPEAK_LANG = {"en": "en-us", "es": "es-419"}
SPEED = 0.90            # default delivery rate; requests may override
SPEED_MIN, SPEED_MAX = 0.7, 1.15
SAMPLE_RATE = 24000
SENTENCE_GAP_S = 0.16   # breath between sentences (kokoro-onnx joins chunks flush)
SHELF_HZ, SHELF_DB = 6000, -3.0   # soften Kokoro's slightly sharp top end
TARGET_RMS_DB = -20.0   # every reply at the same loudness
ROOM_WET = 0.0          # 0 = dry; ~0.08 adds a faint small-room tail

# Respellings clean_for_tts makes for espeak, given exact phonemes under
# misaki (which stresses "Antigwa" as ant-ih-GWAH). misaki's US set: O = oʊ.
MISAKI_SAY = {"Antigwa": "æntˈiɡwə", "antigwa": "æntˈiɡwə", "No-ee": "nˈOˌi"}
_MISAKI_SAY_RE = re.compile("|".join(rf"(?<![\w-]){re.escape(w)}(?![\w-])" for w in MISAKI_SAY))
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+(?=\S)")

kokoro = None
_voices = {}           # lang -> precomputed blended style vector
_espeak_lock = threading.Lock()   # espeak (ours and misaki's fallback) isn't thread-safe
_misaki = None          # misaki G2P for English, or None → espeak
USE_MISAKI = True       # --g2p espeak turns it off


def _reuse_espeak(tokenizer):
    """Phonemize with one long-lived espeak backend.

    kokoro-onnx calls phonemizer.phonemize() per request, which builds a fresh
    espeak backend each time — and each one copies libespeak-ng.so into its own
    /tmp dir that is never freed while the process lives (~1.3 MB of disk plus
    mapped memory per request). On 2026-09-24 that filled /tmp overnight and
    every request 500'd. Output is identical; espeak isn't thread-safe, hence
    the lock.
    """
    backends = {code: EspeakBackend(code, preserve_punctuation=True, with_stress=True)
                for code in ESPEAK_LANG.values()}
    lock = _espeak_lock

    def phonemize(text, lang="en-us", norm=True):
        if lang not in backends:
            raise ValueError(f"unsupported language {lang!r}")
        if norm and lang == "en-us":   # kokoro's normalizer is English-only
            text = tokenizer.normalize_text(text)
        with lock:
            phonemes = _phonemize(backends[lang], text, default_separator, False, 1, False, False)
        return "".join(p for p in phonemes if p in tokenizer.vocab).strip()

    tokenizer.phonemize = phonemize


def _load_misaki():
    """misaki needs spaCy (venv-tts on Python 3.13); without it, stay on espeak."""
    try:
        from misaki import en, espeak
        return en.G2P(trf=False, british=False, fallback=espeak.EspeakFallback(british=False))
    except Exception as exc:   # ImportError, or spaCy's model missing
        print(f"misaki unavailable ({exc!r}); English uses espeak")
        return None


def _misaki_phonemes(text):
    text = _MISAKI_SAY_RE.sub(lambda m: f"[{m.group(0)}](/{MISAKI_SAY[m.group(0)]}/)", text)
    with _espeak_lock:
        return _misaki(text)[0]


def _speak(model, text, voice, speed, lang):
    """One sentence at a time, joined by SENTENCE_GAP_S of silence."""
    gap = np.zeros(int(SAMPLE_RATE * SENTENCE_GAP_S), dtype=np.float32)
    parts = []
    for sentence in _SENTENCE_RE.split(text):
        if lang == "en" and _misaki:
            samples, _ = model.create(_misaki_phonemes(sentence), voice=voice,
                                      speed=speed, is_phonemes=True)
        else:
            samples, _ = model.create(sentence, voice=voice, speed=speed,
                                      lang=ESPEAK_LANG[lang])
        if len(samples):
            parts += [samples, gap]
    return np.concatenate(parts[:-1]) if parts else np.zeros(0, dtype=np.float32)


def _polish(x):
    """High-shelf cut, optional room tail, then a fixed RMS loudness.
    FFT-domain so it needs only numpy; replies are a few seconds long."""
    if not len(x):
        return x
    n = 1 << int(np.ceil(np.log2(len(x) + SAMPLE_RATE // 4)))   # room for the tail
    spec = np.fft.rfft(x, n)
    f = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    shelf = 10 ** (SHELF_DB / 20)
    spec *= 1 + (shelf - 1) / (1 + (SHELF_HZ / np.maximum(f, 1)) ** 2)   # smooth shelf
    if ROOM_WET:
        rng = np.random.default_rng(7)
        t = np.arange(SAMPLE_RATE // 4)
        ir = rng.standard_normal(len(t)) * np.exp(-t / (SAMPLE_RATE * 0.05))
        spec *= (1 - ROOM_WET) + ROOM_WET * 8 * np.fft.rfft(ir / np.abs(ir).sum(), n)
    y = np.fft.irfft(spec, n)[:len(x)]
    y *= 10 ** (TARGET_RMS_DB / 20) / max(float(np.sqrt(np.mean(y ** 2))), 1e-6)
    return np.clip(y, -0.98, 0.98).astype(np.float32)


def get_model():
    global kokoro
    if kokoro is None:
        model_path = MODELS_DIR / "kokoro-v1.0.onnx"
        print(f"Loading Kokoro from {model_path}...")
        t0 = time.time()
        # ONNX Runtime's default thread count spills onto E-cores (12600K) or
        # oversubscribes small CPUs; KOKORO_THREADS is set per host in a
        # systemd drop-in (docs/deployment.md). 0/unset = runtime default.
        opts = rt.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("KOKORO_THREADS") or 0)
        sess = rt.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
        kokoro = Kokoro.from_session(sess, str(MODELS_DIR / "voices-v1.0.bin"))
        _reuse_espeak(kokoro.tokenizer)
        global _misaki
        _misaki = _load_misaki() if USE_MISAKI else None
        for lang, blend in BLENDS.items():
            _voices[lang] = sum(kokoro.get_voice_style(name) * w for name, w in blend.items())
        print(f"Kokoro ready in {time.time()-t0:.1f}s; g2p {'misaki' if _misaki else 'espeak'}; "
              f"threads {opts.intra_op_num_threads or 'default'}; "
              f"voice blends {BLENDS} speed {SPEED}")
    return kokoro


@app.route("/tts", methods=["POST"])
def tts():
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return Response(b"", status=400, mimetype="audio/wav")

    try:
        speed = float(data.get("speed", SPEED))
    except (TypeError, ValueError):
        speed = SPEED
    speed = max(SPEED_MIN, min(SPEED_MAX, speed))
    lang = data.get("lang", "en") if data.get("lang") in BLENDS else "en"

    t0 = time.time()
    model = get_model()
    samples = _polish(_speak(model, text, _voices[lang], speed, lang))

    buf = io.BytesIO()
    sf.write(buf, samples, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    wav_bytes = buf.getvalue()

    elapsed = time.time() - t0
    print(f"TTS: {elapsed:.2f}s {lang} speed={speed:.2f} for {len(text)} chars -> {len(wav_bytes)} bytes")

    return Response(wav_bytes, mimetype="audio/wav")


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "model": "kokoro-onnx", "voice": "antigua-blend",
            "g2p": "misaki" if _misaki else "espeak", "blends": BLENDS, "speed": SPEED}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5500)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--models-dir", type=Path, default=MODELS_DIR)
    parser.add_argument("--g2p", choices=("misaki", "espeak"), default="misaki")
    args = parser.parse_args()
    MODELS_DIR = args.models_dir
    USE_MISAKI = args.g2p == "misaki"

    for lang, word in (("en", "Ready."), ("es", "Lista.")):   # load + warm before traffic
        _speak(get_model(), word, _voices[lang], SPEED, lang)
    print(f"Starting Kokoro TTS server on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)
