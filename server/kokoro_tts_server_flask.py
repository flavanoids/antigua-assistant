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
"""
import io
import threading
import time
from pathlib import Path

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
    "en": {"ef_dora": 0.25, "af_bella": 0.45, "af_heart": 0.30},
    "es": {"ef_dora": 0.70, "af_bella": 0.20, "af_heart": 0.10},
}
# es-419 = Latin American Spanish: seseo ("cinco" → sinko) and yeísmo. Plain
# "es" is Castilian ("θinko"), a mismatch for ef_dora, a Latin American voice.
ESPEAK_LANG = {"en": "en-us", "es": "es-419"}
SPEED = 0.90            # default delivery rate; requests may override
SPEED_MIN, SPEED_MAX = 0.7, 1.15

kokoro = None
_voices = {}           # lang -> precomputed blended style vector


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
    lock = threading.Lock()

    def phonemize(text, lang="en-us", norm=True):
        if lang not in backends:
            raise ValueError(f"unsupported language {lang!r}")
        if norm and lang == "en-us":   # kokoro's normalizer is English-only
            text = tokenizer.normalize_text(text)
        with lock:
            phonemes = _phonemize(backends[lang], text, default_separator, False, 1, False, False)
        return "".join(p for p in phonemes if p in tokenizer.vocab).strip()

    tokenizer.phonemize = phonemize


def get_model():
    global kokoro
    if kokoro is None:
        model_path = MODELS_DIR / "kokoro-v1.0.onnx"
        print(f"Loading Kokoro from {model_path}...")
        t0 = time.time()
        kokoro = Kokoro(str(model_path), str(MODELS_DIR / "voices-v1.0.bin"))
        _reuse_espeak(kokoro.tokenizer)
        for lang, blend in BLENDS.items():
            _voices[lang] = sum(kokoro.get_voice_style(name) * w for name, w in blend.items())
        print(f"Kokoro ready in {time.time()-t0:.1f}s; voice blends {BLENDS} speed {SPEED}")
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
    samples, sample_rate = model.create(text, voice=_voices[lang], speed=speed,
                                        lang=ESPEAK_LANG[lang])

    buf = io.BytesIO()
    sf.write(buf, samples, sample_rate, format="WAV", subtype="PCM_16")
    wav_bytes = buf.getvalue()

    elapsed = time.time() - t0
    print(f"TTS: {elapsed:.2f}s {lang} speed={speed:.2f} for {len(text)} chars -> {len(wav_bytes)} bytes")

    return Response(wav_bytes, mimetype="audio/wav")


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "model": "kokoro-onnx", "voice": "antigua-blend",
            "blends": BLENDS, "speed": SPEED}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5500)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--models-dir", type=Path, default=MODELS_DIR)
    args = parser.parse_args()
    MODELS_DIR = args.models_dir

    for lang, word in (("en", "Ready."), ("es", "Lista.")):   # load + warm before traffic
        get_model().create(word, voice=_voices[lang], speed=SPEED, lang=ESPEAK_LANG[lang])
    print(f"Starting Kokoro TTS server on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)
