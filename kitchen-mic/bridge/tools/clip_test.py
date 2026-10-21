"""One-clip-at-a-time mic capture + wake-word scoring for the kitchen reSpeaker.

Two jobs (2026-10-08): measure real far-field pickup of the current 'alexa'
model from different spots in the room (to tune WAKE_WORD_THRESHOLD /
XMOS DSP gain from data, not guesses), and bank clean 'Hey Antigua'
utterances as validation samples for the future custom wake-word model
(the deferred task in PLAN_kitchenmic.md).

The bridge service must be stopped while capturing — only one API client
can own the reSpeaker's voice stream. The tool refuses to start otherwise:
    sudo systemctl stop kitchen-bridge
    venv/bin/python tools/clip_test.py --label "counter-6ft"
    ...repeat per room position, one clip at a time...
    sudo systemctl start kitchen-bridge

Each capture arms the stream (start_va), waits for speech, closes ~2s after
speech stops (or at --seconds), then scores the clip through the same
pipeline kitchen_bridge.py uses live — 80ms frames fed to openWakeWord in
order, no per-frame reset — and prints:
  - peak score + every frame that scored >= 0.4, with timestamps
  - the longest run of consecutive frames over each threshold 0.50-0.90, so
    a (threshold, confirm-frames) pair that separates real clips from the
    TV noise in captures/nearmiss_*.wav can be read straight off the table
  - RMS / peak dBFS, and the max XMOS voice-to-noise estimate during the clip
  - a faster-whisper transcript (server venv, run as a subprocess so the
    bridge venv stays free of STT deps)

Clips land in bridge/clips/<label>_<ts>.wav. Household audio: never commit
(root .gitignore blocks *.wav; .githooks/pre-commit enforces it).

--score-only re-scores existing WAVs (near-misses, saved clips, anything
16kHz-ish) without touching the device — e.g. after a threshold change, or
to compare today's near-misses against a new clip set. --model-path scores
a custom openWakeWord model file (.onnx/.tflite, key set by --model), ready
for the 'hey antigua' model once trained.

Caveat on scoring short clips: openWakeWord keeps a rolling ~1.9s feature
window; live it is always warm, but a clip that starts mid-speech (e.g. a
2.5s near-miss capture) begins with a cold window, so its scores read a
little lower than the live log showed. Clips captured here include ~1-2s
of lead-in silence, which warms the window the same way live does.
"""

import argparse
import asyncio
import re
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import yaml
from aioesphomeapi import APIClient
from aioesphomeapi.model import VoiceAssistantEventType
from openwakeword.model import Model as WakeWordModel
from openwakeword import VAD

BRIDGE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BRIDGE_DIR / "config.yaml"
CLIPS_DIR = BRIDGE_DIR / "clips"
SERVER_VENV_PY = BRIDGE_DIR.parents[1] / "venv" / "bin" / "python"

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
OWW_SAMPLES = 1280        # 80ms — openWakeWord's expected frame size
VAD_SAMPLES = 480         # 30ms — Silero's frame size
VAD_SILENCE_THRESHOLD = 0.3
# kitchen_bridge.py's current pair: a wake needs this many consecutive
# frames at/over this score.
BRIDGE_THRESHOLD = 0.75
BRIDGE_CONFIRM_FRAMES = 3
THRESHOLDS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)
NEAR_MISS = 0.40          # frames at/over this are listed individually
BRIDGE_SERVICE = "kitchen-bridge.service"


# ── audio helpers ────────────────────────────────────────────────────────────

def load_wav(path: Path) -> np.ndarray:
    """Read a WAV as 16kHz mono int16, the format openWakeWord expects."""
    with wave.open(str(path)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        rate, channels = w.getframerate(), w.getnchannels()
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if rate != SAMPLE_RATE:
        from scipy.signal import resample_poly
        x = resample_poly(x.astype(np.float32), SAMPLE_RATE, rate)
        x = np.clip(x, -32768, 32767).astype(np.int16)
    return x


def write_wav(path: Path, raw: bytes):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw)


def longest_run(scores, threshold: float) -> int:
    run = best = 0
    for s in scores:
        run = run + 1 if s >= threshold else 0
        best = max(best, run)
    return best


def transcribe(path: Path) -> str | None:
    if not SERVER_VENV_PY.exists():
        return None
    script = (
        "import sys\n"
        "from faster_whisper import WhisperModel\n"
        "m = WhisperModel('base.en', device='cpu', compute_type='int8')\n"
        "segs, _ = m.transcribe(sys.argv[1], language='en')\n"
        "print(''.join(s.text for s in segs))\n"
    )
    try:
        r = subprocess.run(
            [str(SERVER_VENV_PY), "-c", script, str(path)],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        print(f"  (transcript failed: {r.stderr.strip()[-200:]})")
        return None
    return r.stdout.strip()


# ── scoring ──────────────────────────────────────────────────────────────────

def score_frames(x: np.ndarray, model_key: str, model_path: str | None):
    """Feed the clip through openWakeWord exactly like the bridge: whole
    80ms frames, in order, no reset between frames. Returns (scores, keys)."""
    if model_path:
        oww = WakeWordModel(wakeword_model_paths=[model_path])
    else:
        oww = WakeWordModel(vad_threshold=0.0)
    keys = sorted(oww.models.keys())
    scores = []
    for i in range(0, len(x) - OWW_SAMPLES + 1, OWW_SAMPLES):
        try:
            pred = oww.predict(x[i:i + OWW_SAMPLES])
        except Exception as e:
            print(f"  (predict error at frame {i // OWW_SAMPLES}: {e})")
            scores.append(0.0)
            continue
        scores.append(float(pred.get(model_key, 0.0)))
    return scores, keys


def report(path: Path, scores, keys, model_key: str):
    peak = max(scores, default=0.0)
    print(f"  {model_key}-model scores: peak={peak:.2f}   (models: {', '.join(keys)})")
    over = [(i * OWW_SAMPLES / SAMPLE_RATE, s) for i, s in enumerate(scores) if s >= NEAR_MISS]
    if over:
        detail = "  ".join(f"{t:.2f}s {s:.2f}" for t, s in over)
        print(f"  frames >= {NEAR_MISS}: {detail}")
    runs = "  ".join(f"{thr:.2f}:{longest_run(scores, thr)}" for thr in THRESHOLDS)
    print(f"  longest runs of consecutive frames over threshold:  {runs}")
    run_now = longest_run(scores, BRIDGE_THRESHOLD)
    verdict = "YES" if run_now >= BRIDGE_CONFIRM_FRAMES else "NO"
    print(f"  would wake at bridge settings ({BRIDGE_THRESHOLD}/{BRIDGE_CONFIRM_FRAMES}): {verdict}"
          + (f"  ({run_now} consecutive frames >= {BRIDGE_THRESHOLD}, need {BRIDGE_CONFIRM_FRAMES})"
             if verdict == "NO" else ""))


def level_stats(x: np.ndarray) -> str:
    f = x.astype(np.float32)
    voiced = f[np.abs(f) > 200]
    rms = float(np.sqrt(np.mean(voiced ** 2))) if voiced.size else 0.0
    peak_db = 20 * np.log10(max(float(np.max(np.abs(f))) / 32768, 1e-9))
    return f"voiced-RMS={rms:.0f}  peak={peak_db:.1f} dBFS  ({len(x) / SAMPLE_RATE:.1f}s)"


def score_file(path: Path, model_key: str, model_path: str | None, do_transcribe: bool):
    print(f"\n== {path.name}")
    x = load_wav(path)
    print(f"  {level_stats(x)}")
    scores, keys = score_frames(x, model_key, model_path)
    if model_key not in keys:
        print(f"  !! model key {model_key!r} not in loaded models {keys} — scores are all zero")
    report(path, scores, keys, model_key)
    if do_transcribe:
        text = transcribe(path)
        print(f"  transcript: {text!r}" if text is not None else "  (no transcript)")


# ── live capture ──────────────────────────────────────────────────────────────

async def capture(label: str, seconds: float, min_silence: float,
                  model_key: str, model_path: str | None) -> Path:
    import subprocess as sp
    if sp.run(["systemctl", "is-active", "--quiet", BRIDGE_SERVICE]).returncode == 0:
        raise SystemExit(
            f"{BRIDGE_SERVICE} is running — stop it first (sudo systemctl stop {BRIDGE_SERVICE}); "
            "only one client can own the reSpeaker's voice stream."
        )

    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    client = APIClient(
        address=cfg["device"]["host"],
        port=cfg["device"].get("port", 6053),
        password=None,
        noise_psk=cfg["device"].get("noise_psk"),
    )
    await asyncio.wait_for(client.connect(login=True), timeout=15)

    entities, services = await client.list_entities_services()
    by_id = {e.object_id: e.key for e in entities}
    vnr_key = by_id.get("voice-to-noise_ratio")
    vnr_max = [0]

    def on_state(state):
        if state.key == vnr_key and isinstance(state.state, (int, float)):
            vnr_max[0] = max(vnr_max[0], state.state)
    client.subscribe_states(on_state)

    chunks = []
    vad = VAD()
    vad.reset_states()
    vad_buf = bytearray()
    silence_chunks = [0]
    speech_seen = asyncio.Event()

    async def handle_start(conversation_id, flags, audio_settings, wake_word_phrase):
        client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START, {})
        return 0

    async def handle_audio(data, _=None):
        chunks.append(data)
        vad_buf.extend(data)
        while len(vad_buf) >= VAD_SAMPLES * SAMPLE_WIDTH:
            raw = bytes(vad_buf[:VAD_SAMPLES * SAMPLE_WIDTH])
            del vad_buf[:VAD_SAMPLES * SAMPLE_WIDTH]
            prob = vad.predict(np.frombuffer(raw, dtype=np.int16), frame_size=VAD_SAMPLES)
            if prob > VAD_SILENCE_THRESHOLD:
                speech_seen.set()
                silence_chunks[0] = 0
            else:
                silence_chunks[0] += 1

    async def handle_stop(abort):
        client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END, {})

    client.subscribe_voice_assistant(
        handle_start=handle_start,
        handle_stop=handle_stop,
        handle_audio=handle_audio,
    )

    start_va = next((s for s in services if s.name == "start_va"), None)
    if not start_va:
        raise SystemExit("start_va service not found on device")
    await client.execute_service(start_va, {})
    print(f">>> SPEAK NOW:  \"Alexa\" ... pause ... \"Hey, Antigua\"   "
          f"(auto-closes {min_silence:.0f}s after you stop, {seconds:.0f}s cap)")

    t0 = time.monotonic()
    reason = "timeout"
    while True:
        await asyncio.sleep(0.1)
        if speech_seen.is_set() and silence_chunks[0] * VAD_SAMPLES / SAMPLE_RATE >= min_silence:
            reason = "speech done"
            break
        if time.monotonic() - t0 >= seconds:
            break
    elapsed = time.monotonic() - t0

    for svc in services:
        if svc.name == "stop_va":
            await client.execute_service(svc, {})
    await client.disconnect()

    raw = b"".join(chunks)
    CLIPS_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label) or "clip"
    out_path = CLIPS_DIR / f"{safe}_{int(time.time())}.wav"
    write_wav(out_path, raw)

    print(f"\ncaptured {len(raw) / (SAMPLE_RATE * SAMPLE_WIDTH):.1f}s in {elapsed:.1f}s ({reason})"
          + (f", vnr max={vnr_max[0]:.0f}" if vnr_max[0] else ""))
    print(f"saved: {out_path.relative_to(BRIDGE_DIR)}")
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", default="clip", help="position tag for the clip filename")
    ap.add_argument("--seconds", type=float, default=15.0, help="max capture window (default 15)")
    ap.add_argument("--min-silence", type=float, default=2.0,
                    help="close this many seconds after speech stops (default 2)")
    ap.add_argument("--model", default="alexa", help="openWakeWord model key to score (default alexa)")
    ap.add_argument("--model-path", default=None,
                    help="path to a custom openWakeWord model (.onnx/.tflite); --model names its key")
    ap.add_argument("--score-only", nargs="+", metavar="WAV",
                    help="score existing WAV files without touching the device")
    args = ap.parse_args()

    if args.score_only:
        for wav in args.score_only:
            score_file(Path(wav), args.model, args.model_path, do_transcribe=True)
        return

    out_path = asyncio.run(capture(args.label, args.seconds, args.min_silence,
                                  args.model, args.model_path))
    print(f"  {level_stats(load_wav(out_path))}")
    scores, keys = score_frames(load_wav(out_path), args.model, args.model_path)
    if args.model not in keys:
        print(f"  !! model key {args.model!r} not in loaded models {keys} — scores are all zero")
    report(out_path, scores, keys, args.model)
    text = transcribe(out_path)
    print(f"  transcript: {text!r}" if text is not None else "  (no transcript)")


if __name__ == "__main__":
    main()