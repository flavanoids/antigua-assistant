"""Batch-capture real user utterances of the future wake word from the reSpeaker.

Records one continuous session and splits it into individual WAVs on speech
activity (Silero VAD), so the user can just say the phrase ~N times in a row
with short pauses. Used to bank real "hey antigua" samples as the validation
set for the custom model training (2026-10-08) — synthetic training samples
come from piper, but the model must score the *user's* voice, so real clips
are how we judge it (and later, optionally, train a custom verifier model).

The bridge service must be stopped first (same rule as clip_test.py):
    sudo systemctl stop kitchen-bridge
    venv/bin/python tools/capture_samples.py --label hey_antigua --count 15
    sudo systemctl start kitchen-bridge

Output: kitchen-mic/wake-word-training/user_samples/<label>_<n>_<ts>.wav
Household audio — never commit (root .gitignore blocks *.wav).
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
from openwakeword import VAD

BRIDGE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BRIDGE_DIR / "config.yaml"
OUT_DIR_DEFAULT = BRIDGE_DIR.parent / "wake-word-training" / "user_samples"
SERVER_VENV_PY = BRIDGE_DIR.parents[1] / "venv" / "bin" / "python"

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
VAD_SAMPLES = 480          # 30ms Silero frame
VAD_SPEECH_THRESHOLD = 0.5
SPEECH_START_RUN = 3       # 90ms of speech opens an utterance
UTT_CLOSE_SILENCE_S = 1.2   # this much silence closes an utterance (natural
                             # repetition pauses run shorter than 2s)
PREROLL_S = 0.5            # padding before speech start
TAIL_S = 0.5               # padding after speech end
MIN_UTT_S = 0.4
MAX_UTT_S = 6.0
BRIDGE_SERVICE = "kitchen-bridge.service"


def write_wav(path: Path, raw: bytes):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw)


def split_utterances(pcm: np.ndarray, vad: VAD):
    """Yield (start_sample, end_sample) for each utterance in the PCM."""
    n_frames = len(pcm) // VAD_SAMPLES
    probs = np.zeros(n_frames)
    for i in range(n_frames):
        probs[i] = vad.predict(pcm[i * VAD_SAMPLES:(i + 1) * VAD_SAMPLES], frame_size=VAD_SAMPLES)
    speech = probs > VAD_SPEECH_THRESHOLD

    utterances = []
    start = None
    run = 0
    silent = 0
    for i, is_speech in enumerate(speech):
        if start is None:
            run = run + 1 if is_speech else 0
            if run >= SPEECH_START_RUN:
                start = max(0, (i - run) * VAD_SAMPLES - int(PREROLL_S * SAMPLE_RATE))
        else:
            silent = 0 if is_speech else silent + 1
            dur = (i * VAD_SAMPLES - start) / SAMPLE_RATE
            if silent * VAD_SAMPLES / SAMPLE_RATE >= UTT_CLOSE_SILENCE_S or dur >= MAX_UTT_S:
                end = min(len(pcm), i * VAD_SAMPLES + int(TAIL_S * SAMPLE_RATE))
                if (end - start) / SAMPLE_RATE >= MIN_UTT_S:
                    utterances.append((start, end))
                start, run, silent = None, 0, 0
    if start is not None:
        end = min(len(pcm), n_frames * VAD_SAMPLES)
        if (end - start) / SAMPLE_RATE >= MIN_UTT_S:
            utterances.append((start, end))
    return utterances, probs


async def capture(count: int, seconds: float, stop_silence: float = 3.0) -> list[tuple[np.ndarray, float]]:
    if subprocess.run(["systemctl", "is-active", "--quiet", BRIDGE_SERVICE]).returncode == 0:
        raise SystemExit(f"{BRIDGE_SERVICE} is running — stop it first: "
                         f"sudo systemctl stop {BRIDGE_SERVICE}")

    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    client = APIClient(
        address=cfg["device"]["host"],
        port=cfg["device"].get("port", 6053),
        password=None,
        noise_psk=cfg["device"].get("noise_psk"),
    )
    await asyncio.wait_for(client.connect(login=True), timeout=15)
    entities, services = await client.list_entities_services()
    vnr_key = {e.object_id: e.key for e in entities}.get("voice-to-noise_ratio")
    vnr_max = [0]

    def on_state(state):
        if state.key == vnr_key and isinstance(state.state, (int, float)):
            vnr_max[0] = max(vnr_max[0], state.state)
    client.subscribe_states(on_state)

    chunks = []

    async def handle_start(conversation_id, flags, audio_settings, wake_word_phrase):
        client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START, {})
        return 0

    async def handle_stop(abort):
        client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END, {})

    print(f">>> RECORDING for up to {seconds:.0f}s — say the phrase ~{count} times, "
          f"~2s pause between each")

    # Live VAD only to detect when the user has finished the batch: once real
    # speech has been seen, 3s more of silence closes the session early.
    vad = VAD()
    vad_buf = bytearray()

    async def handle_audio(data, _=None):
        chunks.append(data)
        vad_buf.extend(data)
        while len(vad_buf) >= VAD_SAMPLES * SAMPLE_WIDTH:
            raw = bytes(vad_buf[:VAD_SAMPLES * SAMPLE_WIDTH])
            del vad_buf[:VAD_SAMPLES * SAMPLE_WIDTH]
            if vad.predict(np.frombuffer(raw, dtype=np.int16), frame_size=VAD_SAMPLES) > VAD_SPEECH_THRESHOLD:
                speech_seen.set()
                silence[0] = 0
            else:
                silence[0] += 1

    client.subscribe_voice_assistant(
        handle_start=handle_start, handle_stop=handle_stop, handle_audio=handle_audio,
    )

    start_va = next((s for s in services if s.name == "start_va"), None)
    if not start_va:
        raise SystemExit("start_va service not found on device")
    await client.execute_service(start_va, {})

    t0 = time.monotonic()
    silence = [0]
    speech_seen = asyncio.Event()

    while True:
        await asyncio.sleep(0.1)
        if time.monotonic() - t0 >= seconds:
            break
        if speech_seen.is_set() and silence[0] * 0.03 >= stop_silence:
            # rough live utterance counting: close when the user seems done
            break
    elapsed = time.monotonic() - t0

    for svc in services:
        if svc.name == "stop_va":
            await client.execute_service(svc, {})
    await client.disconnect()

    pcm = np.frombuffer(b"".join(chunks), dtype=np.int16)
    return pcm, elapsed, vnr_max[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", default="hey_antigua")
    ap.add_argument("--count", type=int, default=15)
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--stop-silence", type=float, default=3.0,
                    help="seconds of silence (after speech) that end the session")
    ap.add_argument("--outdir", default=str(OUT_DIR_DEFAULT))
    args = ap.parse_args()

    pcm, elapsed, vnr_max = asyncio.run(capture(args.count, args.seconds, args.stop_silence))
    print(f"captured {len(pcm) / SAMPLE_RATE:.1f}s in {elapsed:.0f}s, vnr max={vnr_max:.0f}")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", args.label)

    # Always keep the raw session — VAD splits can merge/miss utterances, and
    # a re-split must not require the user to re-record.
    full_path = outdir / f"{safe}_full_{ts}.wav"
    write_wav(full_path, pcm.tobytes())
    print(f"full session: {full_path.name}")

    vad = VAD()
    vad.reset_states()
    utterances, probs = split_utterances(pcm, vad)
    print(f"detected {len(utterances)} utterance(s)"
          + (f" (asked for {args.count})" if len(utterances) != args.count else ""))
    saved = []
    for n, (s, e) in enumerate(utterances, start=1):
        path = outdir / f"{safe}_{n:02d}_{ts}.wav"
        write_wav(path, pcm[s:e].tobytes())
        seg = pcm[s:e].astype(np.float32)
        rms = float(np.sqrt(np.mean(seg ** 2)))
        saved.append(path)
        print(f"  {path.name}  {(e - s) / SAMPLE_RATE:.2f}s  rms={rms:.0f}")

    if not utterances:
        path = outdir / f"{safe}_full_{ts}.wav"
        write_wav(path, pcm.tobytes())
        saved.append(path)
        print(f"  no utterances detected — full session saved to {path.name} for inspection")

    print(f"\nsaved {len(saved)} file(s) to {outdir}")
    print("next: venv/bin/python tools/clip_test.py --score-only <files> to verify")


if __name__ == "__main__":
    main()