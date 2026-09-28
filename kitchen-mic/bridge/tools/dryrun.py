"""Dry-run the kitchen-mic pipeline end to end without touching a speaker.

Drives the real KitchenBridge state machine (openWakeWord -> Silero VAD ->
POST /pipeline -> follow-up window) with a fake reSpeaker client and a fake
MQTT client, against the live antigua_server on localhost. Nothing reaches
the Pi: the bridge's own antigua/play and antigua/cue publishes are only
recorded, and the server is told X-Play-Topic: antigua/dryrun, which nothing
subscribes to, so streamed LLM chunks are synthesized but never played.

Audio: TTS'd prompts (Kokoro on the backup) scaled to the kitchen mic's real
level, plus real reSpeaker captures replayed via push-to-talk. A virtual
clock advances with the audio fed, so timeouts run faster than real time.

Don't add timer/alarm/reminder prompts: the server would schedule a real
one and it would ring on the Pi later.

Run (from kitchen-mic/bridge): venv/bin/python tools/dryrun.py
"""

import asyncio
import io
import os
import sys
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import requests
import yaml
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).parent.parent))
import kitchen_bridge as kb  # noqa: E402

TTS_URL = os.environ.get("ANTIGUA_TTS_URL", "http://127.0.0.1:5500/tts")
DRYRUN_TOPIC = "antigua/dryrun"
CHUNK_BYTES = 1024  # reSpeaker voice_assistant frame size
TARGET_RMS = None   # set from a real capture in main()
# Real kitchen captures whose logged transcript was "What time is it?", and
# one that came back empty (1790107933).
REAL_CAPTURES = {
    "utterance_1790107910.wav": "time",
    "utterance_1790118282.wav": "time",
    "utterance_1790133021.wav": "time",
}
# Real kitchen clips live here locally only: gitignored and blocked by
# .githooks/pre-commit (household audio never goes in git). captures/ is
# pruned after 7 days, so copy any clip you want to keep into fixtures/.
REAL_DIR = Path(__file__).parent / "fixtures"
PREROLL_SAMPLES = 3 * kb.OWW_CHUNK_BYTES // kb.SAMPLE_WIDTH  # deque(maxlen=3) of 80ms chunks
NAMES = {v: k for k, v in vars(kb).items() if k.startswith("LED_")}


class Clock:
    t = 1_800_000_000.0

    def time(self):
        return self.t


CLOCK = Clock()
kb.time = CLOCK


class FakeClient:
    def __init__(self, **_):
        self.calls = []

    async def execute_service(self, svc, data):
        self.calls.append((svc.name, data))

    def send_voice_assistant_event(self, *_):
        pass

    def media_player_command(self, key, **kw):
        self.calls.append(("media_player", kw))


class FakeMQTT:
    def __init__(self, *_, **__):
        self.published = []
        self.on_message = self.on_connect = None

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload))


posts = []


def _post(url, data, headers, timeout):
    headers = {**headers, "X-Play-Topic": DRYRUN_TOPIC}
    resp = requests.post(url, data=data, headers=headers, timeout=timeout)
    posts.append({"headers": headers, "result": resp.json() if resp.ok else {"http": resp.status_code}})
    return resp


kb.CAPTURES_DIR = Path(tempfile.mkdtemp(prefix="kitchen_dryrun_"))
kb.APIClient = FakeClient
kb.mqtt = SimpleNamespace(Client=FakeMQTT, CallbackAPIVersion=kb.mqtt.CallbackAPIVersion)
kb.requests = SimpleNamespace(post=_post)


# ── audio helpers ───────────────────────────────────────────────────────────

rng = np.random.default_rng(0)


def noise(seconds, rms=40.0):
    return (rng.standard_normal(int(seconds * kb.SAMPLE_RATE)) * rms).astype(np.int16)


def tts(text):
    r = requests.post(TTS_URL, json={"text": text}, timeout=30)
    r.raise_for_status()
    with wave.open(io.BytesIO(r.content)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        x = resample_poly(x, kb.SAMPLE_RATE, w.getframerate())
    voiced = x[np.abs(x) > 200]
    x *= TARGET_RMS / np.sqrt(np.mean(voiced ** 2))
    return np.clip(x + noise(len(x) / kb.SAMPLE_RATE), -32768, 32767).astype(np.int16)


def load_wav(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def cat(*parts):
    return np.concatenate(parts)


# ── harness ─────────────────────────────────────────────────────────────────


def new_bridge(cfg):
    b = kb.KitchenBridge(cfg)
    b._loop = asyncio.get_running_loop()
    b._services = {n: SimpleNamespace(name=n) for n in ("start_va", "led_state", "led_level")}
    b._button_wired = True
    return b


async def settle():
    await asyncio.gather(*(t for t in asyncio.all_tasks() if t is not asyncio.current_task()))


async def feed(b, pcm):
    raw = pcm.tobytes()
    for i in range(0, len(raw), CHUNK_BYTES):
        chunk = raw[i:i + CHUNK_BYTES]
        CLOCK.t += len(chunk) / (kb.SAMPLE_RATE * kb.SAMPLE_WIDTH)
        await b.handle_audio(chunk)
        await asyncio.sleep(0)
    await settle()


def leds(b, since=0):
    return [NAMES.get(d["state"], d["state"]) for n, d in b.client.calls[since:] if n == "led_state"]


def topics(b, since=0):
    return [t for t, _ in b.mqtt.published[since:]]


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def last_post(n_before):
    return posts[-1] if len(posts) > n_before else None


async def scenario_conversation(cfg, P):
    print("\n== Wake word turn -> mute while playing -> follow-up -> follow-up timeout ==")
    b = new_bridge(cfg)
    on_pi = not b._reply_on_device

    n = len(posts)
    await feed(b, cat(noise(4), P["alexa"], noise(0.4), P["time"], noise(1.5)))
    p = last_post(n)
    check("wake word fires and a turn is POSTed", p is not None)
    if not p:
        return b
    r = p["result"]
    print(f"      transcript={r.get('transcript')!r}\n      response={r.get('response')!r}")
    check("transcript is the command", "time" in r.get("transcript", "").lower())
    check("server replied", bool(r.get("response")))
    check("sent as a wake turn", p["headers"]["X-Follow-Up"] == "0")
    if on_pi:
        check("wake cue sent to antigua/cue (captured, not sent)", "antigua/cue" in topics(b))
        if not r.get("streaming"):
            check("deterministic reply handed to antigua/play (captured, not sent)",
                  "antigua/play" in topics(b))
    check("LEDs go listening -> thinking -> speaking",
          leds(b)[-3:] == ["LED_LISTENING", "LED_THINKING", "LED_SPEAKING"], str(leds(b)))
    check("mic muted while the reply plays", b._playing)

    if on_pi:
        n = len(posts)
        await feed(b, cat(noise(1), P["alexa"], noise(0.4), P["time"], noise(1.5)))
        check("wake word ignored while muted (no self-trigger)", len(posts) == n)

    conv = b.conversation_id
    b._on_playback_done()  # what antigua/done does
    check("antigua/done opens the follow-up window", b.state == kb.STATE_FOLLOWUP)
    n, mark = len(posts), len(b.client.calls)
    await feed(b, cat(noise(0.6), P["date"], noise(1.5)))
    p = last_post(n)
    check("plain speech (no wake word) opens a follow-up turn", p is not None)
    if p:
        r = p["result"]
        print(f"      transcript={r.get('transcript')!r}\n      response={r.get('response')!r}")
        check("follow-up flagged X-Follow-Up: 1", p["headers"]["X-Follow-Up"] == "1")
        check("same conversation continues", p["headers"]["X-Conversation-ID"] == conv)
        check("follow-up got a reply", bool(r.get("response")))

    b._on_playback_done()
    await feed(b, noise(kb.FOLLOW_UP_TIMEOUT_S + 1.5))
    check("silent follow-up window times out and ends the conversation",
          b.state == kb.STATE_LISTENING and not b.conversation_id)
    check("LED back to idle", leds(b)[-1:] == ["LED_IDLE"], str(leds(b, mark)))
    return b


async def scenario_llm(cfg, P):
    print("\n== Streaming LLM turn ==")
    b = new_bridge(cfg)
    n = len(posts)
    await feed(b, cat(noise(4), P["alexa"], noise(0.4), P["fact"], noise(1.5)))
    p = last_post(n)
    r = (p or {}).get("result", {})
    print(f"      transcript={r.get('transcript')!r}\n      response={r.get('response')!r}")
    check("LLM reply streamed (chunks went to the dry-run topic)", bool(r.get("streaming")))
    check("bridge doesn't republish a streamed reply to antigua/play",
          "antigua/play" not in topics(b))
    check("mic muted until antigua/done", b._playing)


async def scenario_negatives(cfg, P):
    print("\n== No false triggers ==")
    b = new_bridge(cfg)
    n = len(posts)
    await feed(b, noise(10, rms=300))
    check("10s of loud noise: no wake, no POST", len(posts) == n and b.state == kb.STATE_LISTENING)
    await feed(b, cat(noise(1), P["time"], noise(1.5)))
    check("command without the wake word: no POST", len(posts) == n)

    b._on_button("long_press")
    await feed(b, cat(noise(4), P["alexa"], noise(0.4), P["time"], noise(1.5)))
    check("do-not-disturb (long press) ignores the wake word", len(posts) == n)
    b._on_button("long_press")
    await feed(b, cat(noise(4), P["alexa"], noise(0.4), P["time"], noise(1.5)))
    check("DND off: wake word works again", len(posts) == n + 1)


async def scenario_real_captures(cfg):
    print("\n== Real reSpeaker captures via push-to-talk ==")
    for name, expect in REAL_CAPTURES.items():
        path = REAL_DIR / name
        if not path.exists():
            print(f"  [SKIP] {name} missing")
            continue
        b = new_bridge(cfg)
        # A capture starts with the bridge's preroll (the wake word's tail),
        # which live never goes through VAD — feed it before the button press
        # so it lands in the preroll buffer again instead of ending the turn.
        pcm, split = load_wav(path), PREROLL_SAMPLES
        await feed(b, cat(noise(4), pcm[:split]))
        n = len(posts)
        b._on_button("single_press")
        await feed(b, cat(pcm[split:], noise(1.5)))
        p = last_post(n)
        t = (p or {}).get("result", {}).get("transcript", "")
        check(f"{name}: transcribed", expect in t.lower(), repr(t))


async def main():
    global TARGET_RMS
    cfg = yaml.safe_load(kb.CONFIG_PATH.read_text())
    if "--device" in sys.argv:
        cfg["reply_output"] = "device"
    ref = load_wav(REAL_DIR / "utterance_1790107910.wav").astype(np.float32)
    TARGET_RMS = float(np.sqrt(np.mean(ref[np.abs(ref) > 200] ** 2)))
    print(f"reply_output={cfg.get('reply_output', 'pi')}  server={kb.SERVER_URL}  "
          f"play topic forced to {DRYRUN_TOPIC}  voiced-RMS target={TARGET_RMS:.0f}")

    P = {k: tts(v) for k, v in {
        "alexa": "Alexa.",
        "time": "What time is it?",
        "date": "And what's today's date?",
        "fact": "Tell me a fun fact about octopuses.",
    }.items()}

    await scenario_conversation(cfg, P)
    await scenario_llm(cfg, P)
    await scenario_negatives(cfg, P)
    await scenario_real_captures(cfg)

    leaked = [p for p in posts if p["headers"].get("X-Play-Topic") != DRYRUN_TOPIC]
    check("every POST routed to the dry-run topic", not leaked)
    print(f"\n{sum(results)}/{len(results)} passed, {len(posts)} server turns")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
