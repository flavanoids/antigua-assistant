"""Combined wake-word + capture test, single session.

Phase 1 (first ~40s): device sits idle in normal mww-detecting state. If
micro_wake_word detects a wake word, the device will spontaneously call
handle_start with wake_word_phrase set — that alone proves wake detection.
Phase 2: if no detection, we call start_va and capture 15s of PCM for a
transcribable WAV (proves mic path, gives us audio to compare).

Prints clear prompts so the operator knows when to speak.
"""

import asyncio
import struct
import time
import wave
from pathlib import Path

import yaml
from aioesphomeapi import APIClient
from aioesphomeapi.model import VoiceAssistantEventType

BRIDGE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BRIDGE_DIR / "config.yaml"
OUT = BRIDGE_DIR / "captures"
OUT.mkdir(exist_ok=True)

state = {
    "detected": False,
    "wake_word": None,
    "chunks": [],
    "phase": "idle",
}


async def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    client = APIClient(
        address=cfg["device"]["host"],
        port=cfg["device"]["port"],
        password=None,
        noise_psk=cfg["device"]["noise_psk"],
    )
    await client.connect(login=True)
    print("Connected to kitchen-mic.")

    _, services = await client.list_entities_services()
    start_va = next(s for s in services if s.name == "start_va")
    stop_va = next(s for s in services if s.name == "stop_va")

    async def handle_start(conversation_id, flags, audio_settings, wake_word_phrase):
        state["wake_word"] = wake_word_phrase
        state["chunks"] = []
        state["detected"] = True
        print(f"\n>>> PIPELINE START: wake_word={wake_word_phrase!r} (phase={state['phase']})")
        client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START, {})
        return 0

    async def handle_audio(data, _=None):
        state["chunks"].append(data)

    async def handle_stop(abort):
        print(f">>> PIPELINE STOP (abort={abort}), captured {sum(len(c) for c in state['chunks'])} bytes")
        client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END, {})

    client.subscribe_voice_assistant(
        handle_start=handle_start,
        handle_stop=handle_stop,
        handle_audio=handle_audio,
    )

    print("\n=== PHASE 1: WAKE WORD TEST (~40s) ===")
    print(">>> pre-roll: waiting 60s before listening starts")
    for i in range(60, 0, -10):
        print(f">>> listening starts in {i}s...")
        await asyncio.sleep(10)
    print(">>> SAY THE WAKE WORDS NOW: 'hey jarvis' ... 'okay nabu' ... repeatedly, near the device.")
    print(">>> (device is in its normal idle state — mww should be listening)")
    t0 = time.time()
    while time.time() - t0 < 40 and not state["detected"]:
        await asyncio.sleep(0.5)
    if state["detected"]:
        print(f"\n*** WAKE WORD DETECTED: {state['wake_word']!r} — mww works! ***")
        # let them speak a command into the started pipeline for 10s
        print(">>> Say a short command sentence now (10s)...")
        await asyncio.sleep(10)
    else:
        print("\n=== PHASE 1 COMPLETE: no wake-word detection in 40s ===")
        print("\n=== PHASE 2: start_va capture test (15s) ===")
        print(">>> SPEAK A SENTENCE NOW — any sentence, e.g. 'the quick brown fox'")
        state["chunks"] = []
        await client.execute_service(start_va, {})
        await asyncio.sleep(15)
        await client.execute_service(stop_va, {})

    raw = b"".join(state["chunks"])
    if raw:
        n = len(raw) // 2
        samples = struct.unpack(f"<{n}h", raw[: n * 2])
        peak = max(abs(s) for s in samples) if samples else 0
        rms = (sum(s * s for s in samples) / n) ** 0.5 if samples else 0
        out_path = OUT / f"combined_{int(time.time())}.wav"
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(raw)
        print(f"\nCaptured {len(raw)} bytes -> {out_path}")
        print(f"peak={peak} rms={rms:.1f} ({'SILENCE' if peak < 100 else 'AUDIO PRESENT'})")
    else:
        print("\nNo audio captured.")

    print(f"\nFINAL: detected={state['detected']} wake_word={state['wake_word']!r}")
    await client.disconnect()


asyncio.run(main())
