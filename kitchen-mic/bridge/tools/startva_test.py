"""Empirical mic test for the kitchen-mic reSpeaker Lite.

Bypasses micro_wake_word entirely: connects via aioesphomeapi, calls the
device's start_va action (voice_assistant.start), and records whatever PCM
the device streams. If audio arrives and contains energy, the mic pipeline
is fine and the bug is in wake-word-land. If we get silence/no data, the
I2S capture path itself is broken.

Also reads switch states (mic_mute_switch, mute_state, mute_toggle GPIO) so
we can see if the XMOS hardware mute is engaged.
"""

import asyncio
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

chunks = []
events = []


async def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    client = APIClient(
        address=cfg["device"]["host"],
        port=cfg["device"]["port"],
        password=None,
        noise_psk=cfg["device"]["noise_psk"],
    )
    await client.connect(login=True)
    info = await client.device_info()
    print(f"Connected: {info.name} esphome={info.esphome_version} flags={info.voice_assistant_feature_flags}")

    entities, services = await client.list_entities_services()
    print("\n--- services ---")
    for s in services:
        print(f"  service: {s.name} args={list(s.args_model().keys()) if hasattr(s, 'args_model') else '?'}")

    print("\n--- switch states ---")
    switch_states = {}

    def on_state(state):
        if hasattr(state, "state") and getattr(state, "key", None) in switch_key_to_name:
            name = switch_key_to_name[state.key]
            switch_states[name] = state.state
            print(f"  [state] {name} = {state.state}")

    switch_key_to_name = {}
    for e in entities:
        if e.__class__.__name__ == "SwitchInfo":
            switch_key_to_name[e.object_id] = e.name

    # subscribe first, then list, then fetch states
    client.subscribe_states(on_state)
    await asyncio.sleep(1.0)

    # find start_va service
    start_va = None
    _, services = await client.list_entities_services()
    for s in services:
        if s.name == "start_va":
            start_va = s
    if not start_va:
        print("!! start_va service not found")
        return

    # Subscribe as the voice assistant "server" (like HA does)
    async def handle_start(conversation_id, flags, audio_settings, wake_word_phrase):
        print(f">> handle_start: conv={conversation_id} wake={wake_word_phrase!r} settings={audio_settings}")
        chunks.clear()
        client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START, {})
        return 0

    async def handle_audio(data, _=None):
        chunks.append(data)
        if len(chunks) % 50 == 0:
            print(f"   ...audio: {len(chunks)} chunks, {sum(len(c) for c in chunks)} bytes")

    async def handle_stop(abort):
        print(f">> handle_stop: abort={abort}")
        client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END, {})

    client.subscribe_voice_assistant(
        handle_start=handle_start,
        handle_stop=handle_stop,
        handle_audio=handle_audio,
    )

    # Trigger voice assistant directly — bypass micro_wake_word
    print("\n=== calling start_va (bypassing wake word) ===")
    await client.execute_service(start_va, {})
    print("start_va called; talk into the mic NOW (30s window)")

    for i in range(30):
        await asyncio.sleep(1)
        if chunks:
            total = sum(len(c) for c in chunks)
            print(f"[{i}s] {len(chunks)} chunks, {total} bytes")
        else:
            print(f"[{i}s] no audio yet")

    client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END, {})
    # stop_va to end the pipeline
    for s in services:
        if s.name == "stop_va":
            await client.execute_service(s, {})
            print("stop_va called")

    if chunks:
        total = sum(len(c) for c in chunks)
        out_path = OUT / f"startva_{int(time.time())}.wav"
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"".join(chunks))
        print(f"\nWROTE {out_path} ({total} bytes PCM)")

        # energy check
        import struct
        raw = b"".join(chunks)
        n = len(raw) // 2
        samples = struct.unpack(f"<{n}h", raw[: n * 2])
        peak = max(abs(s) for s in samples) if samples else 0
        rms = (sum(s * s for s in samples) / n) ** 0.5 if samples else 0
        print(f"peak={peak} rms={rms:.1f}  ({'SILENCE' if peak < 100 else 'AUDIO PRESENT'})")
    else:
        print("\nNO AUDIO RECEIVED — mic I2S path confirmed dead")

    await client.disconnect()


asyncio.run(main())
