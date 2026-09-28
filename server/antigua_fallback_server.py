#!/usr/bin/env python3
"""Antigua Fallback Server — runs on the backup box

Pipeline server activated when the primary is offline. Runs the same
antigua_core pipeline as the primary — classify(), every skill route, the
memory/timer/news/search stack — with a smaller backend injected:

  - STT:  faster-whisper tiny (CPU) instead of whisper.cpp
  - LLM:  qwen3.5:2b (Vulkan) — think=False, NO /no_think suffix (causes
          artifacts), non-streaming (whole reply arrives as one chunk, split
          by sentence). ~2s warm; 0.8b was no faster warm and padded replies.
  - TTS:  http://localhost:5500/tts (same box, no LAN hop)
  - Data: <deploy dir>/data/
  - Port: 9394 | Extra endpoints: POST /activate, POST /deactivate

While the primary is up this box is TTS-only: nothing is loaded here. A
watcher polls the primary's /health and activates on its own (STT + LLM
warm and pinned) after PRIMARY_DOWN_AFTER misses, then releases both once
the primary is back. It also mirrors the primary's /timers while it's up,
rings those alarms while it's down (TimerManager.adopt), and drops them
when it returns — the primary skips anything that came due meanwhile. The satellite's /activate and /deactivate still work
as hints. Deploy with server/scripts/deploy_fallback.sh.

Roku TV and Govee lights work here too: this box runs its own copies of the
MCP servers (server/mcp/venv, same `mcp:` block in server.yaml), started on
activation and stopped on stand-down so they don't sit idle beside TTS.

Declared capability differences (not drift — the fallback genuinely cannot
do these):
  - SearXNG lives on the primary; searches work when only the antigua-server
    process is down, and degrade to LLM-only answers when the box is.
"""

import json
import logging
import re
import socket
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
import requests
import yaml
from faster_whisper import WhisperModel

from antigua_core import pipeline
from antigua_core import server_common
from antigua_core import settings as core_settings
from antigua_core.caches import NewsCache, NWSAlertsCache, SearXNGSkill, WeatherCache
from antigua_core.classify import extract_weather_location
from antigua_core.pipeline import format_active_timers
from antigua_core.router import llm_route_search
from antigua_core.home_control import HomeControl
from antigua_core.mcp_client import McpHub, read_env_file
from antigua_core.music import MusicControl
from antigua_core.stores import ConversationStore, ListStore, MemoryStore, TimerManager
from antigua_core.tts_text import clean_for_tts

_tts_session = requests.Session()

# ── Config ──────────────────────────────────────────────────────────────────

# Shared config (server.yaml deployed from the primary alongside this file)
CONFIG_PATH = core_settings.local_or_example(Path(__file__).parent / "config" / "server.yaml")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("antigua-fallback")


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


cfg = load_config()

# ── Fallback-specific overrides (ignore whatever server.yaml says for these) ──

FALLBACK_PORT = 9394
OLLAMA_MODEL = "qwen3.5:2b"
OLLAMA_NUM_CTX = 8192
REMOTE_TTS_URL = "http://localhost:5500/tts"          # same box on the backup
PRIMARY_HOST = "10.77.0.1"                            # the primary over wg0
PRIMARY_HEALTH_URL = f"http://{PRIMARY_HOST}:9393/health"
PRIMARY_TIMERS_URL = f"http://{PRIMARY_HOST}:9393/timers"
PRIMARY_POLL_S = 5
PRIMARY_DOWN_AFTER = 2        # consecutive misses before taking over
PRIMARY_UP_AFTER = 3          # consecutive hits before standing down

FALLBACK_BASE = core_settings.BASE_DIR   # deploy_fallback.sh mirrors the repo layout
AUDIO_OUT_DIR = FALLBACK_BASE / "audio_out"
AUDIO_OUT_DIR.mkdir(parents=True, exist_ok=True)
STATIC_AUDIO_DIR = Path(__file__).parent / "static_audio"   # wake cue etc.

OLLAMA_HOST = cfg.get("ollama", {}).get("host", "http://localhost:11434")
OLLAMA_MAX_TOKENS = cfg.get("ollama", {}).get("max_tokens", 65)
OLLAMA_TEMP = cfg.get("ollama", {}).get("temperature", 0.4)
OLLAMA_KEEP_ALIVE = -1       # pinned while active; _deactivate() unloads it

_sp_path = core_settings.local_or_example(Path(__file__).parent / "config" / "system_prompt.txt")
SYSTEM_PROMPT = _sp_path.read_text().strip()

AUDIO_TTL = cfg["server"].get("audio_ttl_seconds", 300)

MQTT_BROKER = cfg["mqtt"]["broker"]
MQTT_PORT_NUM = cfg["mqtt"]["port"]

SEARCH_ROUTER_TIMEOUT = cfg.get("search", {}).get("router_timeout_seconds", 3)
# Off here: a cold router prompt costs ~3s on this box (it timed out at the
# 3s limit in testing) and evicts the cached system prompt. Explicit
# "search for…" phrasing still routes through the regex layers.
SEARCH_ROUTER_ENABLED = False
NEWS_SOURCES = cfg.get("news", {}).get("sources", [])

# Core settings from the shared yaml, then the paths and URLs that differ here.
core_settings.configure(cfg)
core_settings.MEMORY_STORE_PATH = FALLBACK_BASE / "data" / "memories.json"
core_settings.TIMER_STORE_PATH = FALLBACK_BASE / "data" / "timers.json"
core_settings.LISTS_STORE_PATH = FALLBACK_BASE / "data" / "lists.json"
# Last copy of the primary's timers/alarms, refreshed every poll while it's up
# and adopted on takeover. On disk so a restart mid-outage still has them.
PRIMARY_TIMERS_PATH = FALLBACK_BASE / "data" / "primary_timers.json"
# server.yaml says localhost:8080 — that means the primary. From this box the
# instance is across the LAN; unreachable searches degrade to LLM-only.
core_settings.SEARCH_URL = f"http://{PRIMARY_HOST}:8080"

# ── MQTT ─────────────────────────────────────────────────────────────────────

mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="antigua-fallback")


def mqtt_connect():
    server_common.mqtt_connect(mqtt_client, MQTT_BROKER, MQTT_PORT_NUM)


def mqtt_publish(topic, payload):
    if not server_common.display_allows(topic):
        return
    try:
        mqtt_client.publish(topic, json.dumps(payload), qos=1)
    except Exception:
        pass


core_settings.mqtt_publish = mqtt_publish

# ── Core store / cache instances ─────────────────────────────────────────────

weather_cache = WeatherCache()
nws_alerts_cache = NWSAlertsCache()
news_cache = NewsCache()
searxng = SearXNGSkill()
conversations = ConversationStore()
memory_store = MemoryStore()
list_store = ListStore()
timers = TimerManager(
    on_fire=lambda label, kind="timer", message=None: _on_timer_fire(label, kind, message)
)


def _on_timer_fire(label: str, kind: str = "timer", message: str | None = None):
    pipeline.note_alarm_fired(label)
    if pipeline.B.home is not None:
        Thread(target=pipeline.B.home.govee_flash, args=(kind,), daemon=True).start()
    text = server_common.timer_fire_text(label, kind, message)
    wav_path = synthesize(text)
    if wav_path:
        audio_url = f"http://{get_local_ip()}:{FALLBACK_PORT}/audio/{Path(wav_path).name}"
        mqtt_publish(
            "antigua/alarm",
            {"text": text, "audio_url": audio_url, "label": label, "kind": kind},
        )


# ── STT (faster-whisper) ─────────────────────────────────────────────────────

_whisper_model = None
_whisper_lock = Lock()


def _get_whisper():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            log.info("Loading faster-whisper tiny model...")
            _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
            log.info("faster-whisper tiny ready")
        return _whisper_model


def transcribe(audio_path, hotwords=None, prefer=None):   # English only here
    t0 = time.time()
    model = _get_whisper()
    segments, _ = model.transcribe(audio_path, language="en", hotwords=hotwords)
    text = " ".join(s.text for s in segments).strip()
    elapsed = round(time.time() - t0, 2)
    if not text:
        return {"text": "", "confidence": 0.0, "time_s": elapsed}
    return {"text": text, "confidence": 0.9, "time_s": elapsed}


# ── LLM (Ollama, small fallback model) ─────────────────────────────────────────

_llm_warm = False


def _build_messages(transcript, conversation_id="", extra_context=None):
    messages = []
    if conversation_id:
        messages = conversations.get_messages(conversation_id)

    log.info("LLM context: conv=%s history=%d msgs", conversation_id or "new", len(messages))

    now_str = server_common.llm_now_str()

    if extra_context is not None:
        system_with_context = SYSTEM_PROMPT + f"\nCurrent date and time: {now_str}." + f"\n{extra_context}"
    else:
        weather_location = extract_weather_location(transcript)
        weather_str = weather_cache.format_for_prompt(weather_location)
        system_with_context = (
            SYSTEM_PROMPT
            + f"\nCurrent date and time: {now_str}."
            + f"\n{weather_str}"
            + "\nYou have access to current weather and 3-day forecast data shown above."
            + " If asked about another city's weather, say you only have local data unless one is provided."
            + f"\n{format_active_timers()}"
        )

    alerts_str = nws_alerts_cache.format_for_prompt()
    if alerts_str:
        system_with_context += f"\n{alerts_str}"

    if not messages or messages[0]["role"] != "system":
        messages.insert(0, {"role": "system", "content": system_with_context})
    else:
        messages[0]["content"] = system_with_context

    # Small qwen: clean user message — no /no_think suffix (causes literal meta-commentary)
    messages.append({"role": "user", "content": transcript})
    return messages


def _llm_payload(messages, max_tokens_override=None, temperature_override=None, stream=False):
    return {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": stream,
        "think": False,
        "options": {
            "num_predict": max_tokens_override if max_tokens_override else OLLAMA_MAX_TOKENS,
            "temperature": OLLAMA_TEMP if temperature_override is None else temperature_override,
            "num_ctx": OLLAMA_NUM_CTX,
        },
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }


def _remember(conversation_id, transcript, reply):
    if conversation_id:
        conversations.add_message(conversation_id, "user", transcript)
        conversations.add_message(conversation_id, "assistant", reply)


def _strip_think(text):
    if text.startswith("<think>"):
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return text


def prime_llm():
    """Evaluate the real system prompt once so the first turn after takeover
    reuses its KV prefix (~1.9k tokens, ~4s cold here) — also fills the
    weather cache the prompt pulls from."""
    payload = _llm_payload(_build_messages("Hello"), max_tokens_override=1)
    requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=60).raise_for_status()


_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])["\'’”)]*\s+')


def ask_llm_stream(transcript, conversation_id="", extra_context=None,
                   max_tokens_override=None, temperature_override=None,
                   grounding_context=None, language="en"):
    """Stream from Ollama and yield whole sentences as they complete, so the
    pipeline starts TTS on the first sentence while the rest generates.
    Grounding context is folded into the prompt; the small model gets no
    separate grounding pass."""
    t0 = time.time()
    messages = _build_messages(transcript, conversation_id, extra_context or grounding_context)
    payload = _llm_payload(messages, max_tokens_override, temperature_override, stream=True)
    buf = full = ""
    try:
        with requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=90, stream=True) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                piece = json.loads(line).get("message", {}).get("content", "")
                buf += piece
                full += piece
                *done, buf = _SENTENCE_SPLIT.split(buf)
                for sentence in filter(None, (d.strip() for d in done)):
                    yield sentence
    except requests.Timeout:
        log.warning("LLM stream timeout")
        yield "Hmm, let me think about that and get back to you."
        return
    except requests.RequestException:
        log.exception("LLM request failed")
        yield "Sorry, I'm having a little trouble right now."
        return
    tail = _strip_think(buf.strip())
    if tail:
        yield tail
    full = _strip_think(full.strip())
    log.info("LLM stream [%.2fs] %d chars", time.time() - t0, len(full))
    _remember(conversation_id, transcript, full)


def route_search_with_llm(transcript: str):
    """Ask the fallback model whether this needs the web. Returns a query, or None."""
    return llm_route_search(
        transcript,
        ollama_host=OLLAMA_HOST,
        model=OLLAMA_MODEL,
        timeout=SEARCH_ROUTER_TIMEOUT,
        keep_alive=OLLAMA_KEEP_ALIVE,
        num_ctx=OLLAMA_NUM_CTX,
        enabled=SEARCH_ROUTER_ENABLED,
    )


# ── TTS ──────────────────────────────────────────────────────────────────────


def synthesize(text: str, lang: str = "en") -> str:
    """Synthesize text via Kokoro TTS on localhost. Returns path to wav file."""
    text = clean_for_tts(text, lang)
    t0 = time.time()
    fname = f"{uuid.uuid4().hex[:12]}.wav"
    out_path = AUDIO_OUT_DIR / fname
    try:
        resp = _tts_session.post(REMOTE_TTS_URL, json={"text": text, "lang": lang}, timeout=20)
        resp.raise_for_status()
        out_path.write_bytes(resp.content)
        log.info("TTS: %.2fs -> %s", time.time() - t0, out_path.name)
        return str(out_path)
    except requests.ConnectionError:
        log.error("TTS unreachable: %s", REMOTE_TTS_URL)
    except requests.Timeout:
        log.error("TTS timeout after 20s")
    except requests.HTTPError as exc:
        log.error("TTS HTTP error: %s", exc)
    return ""


# ── Network helpers ───────────────────────────────────────────────────────────


def get_local_ip():
    """Where the Pi fetches our reply audio: the address used to reach its
    broker — this host's wg0 address (network/README.md)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((MQTT_BROKER, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "10.77.0.3"


# ── Pipeline (shared core, fallback backend) ─────────────────────────────────

mcp_hub = McpHub.from_config(cfg.get("mcp"), core_settings.BASE_DIR)

# Music Assistant (Apple Music). The token lives in the same gitignored env
# file as the MCP secrets; without it the music route says it can't play.
_ma_token = read_env_file(core_settings.BASE_DIR / cfg.get("mcp", {}).get(
    "env_file", "server/config/mcp.env")).get("MUSIC_ASSISTANT_TOKEN")
music = MusicControl(cfg, _ma_token) if _ma_token and cfg.get("music", {}).get("enabled", True) else None

pipeline.init(pipeline.Backend(
    transcribe=transcribe,
    synthesize=synthesize,
    ask_llm_stream=ask_llm_stream,
    audio_url_base=lambda: f"http://{get_local_ip()}:{FALLBACK_PORT}",
    memory_store=memory_store,
    list_store=list_store,
    timers=timers,
    weather_cache=weather_cache,
    news_cache=news_cache,
    searxng=searxng,
    route_search_with_llm=route_search_with_llm,
    home=HomeControl(mcp_hub, cfg),
    music=music,
))

run_pipeline = pipeline.run_pipeline


# ── Background Cleanup ────────────────────────────────────────────────────────


def cleanup_loop():
    while True:
        time.sleep(60)
        try:
            removed = server_common.prune_audio(AUDIO_OUT_DIR, AUDIO_TTL)
            if removed:
                log.debug("Cleaned %d old audio files", removed)
            conversations.cleanup()
            pipeline.cleanup_pending()
        except Exception:
            log.exception("Cleanup error")


# ── HTTP Server ───────────────────────────────────────────────────────────────


class FallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("HTTP: %s", fmt % args)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {
                "status": "ok",
                "model": OLLAMA_MODEL,
                "mode": "fallback",
                "llm_warm": _llm_warm,
                "active": _active,
                "mcp": mcp_hub.status(),
            })
        elif parsed.path.startswith("/audio/"):
            filename = parsed.path[len("/audio/"):]
            filepath = AUDIO_OUT_DIR / filename
            if not filepath.exists():
                filepath = STATIC_AUDIO_DIR / filename
            if filepath.exists():
                data = filepath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json(404, {"error": "audio not found"})
        elif parsed.path == "/memories":
            self._json(200, {"memories": memory_store.active()})
        elif parsed.path == "/lists":
            self._json(200, {"lists": list_store.all_lists()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/pipeline":
            self._handle_pipeline()
        elif parsed.path == "/activate":
            self._handle_activate()
        elif parsed.path == "/deactivate":
            self._handle_deactivate()
        else:
            self._json(404, {"error": "not found"})

    def _conv_id(self):
        return self.headers.get("X-Conversation-ID", "")

    def _handle_pipeline(self):
        # A turn can beat the watcher here (e.g. the primary bridge fails over
        # the moment antigua-server dies). Mark us active so the watcher is
        # the one that later unloads whatever this request loads.
        Thread(target=_activate, args=("pipeline request",), daemon=True).start()
        length = int(self.headers.get("Content-Length", 0))
        audio = self.rfile.read(length)
        tmp = AUDIO_OUT_DIR / f"_in_{uuid.uuid4().hex[:8]}.wav"
        tmp.write_bytes(audio)

        conv_id = self._conv_id()
        follow_up = self.headers.get("X-Follow-Up", "0") == "1"
        result = run_pipeline(str(tmp), conversation_id=conv_id, follow_up=follow_up)
        tmp.unlink(missing_ok=True)

        audio_url = ""
        if result.get("audio_file"):
            audio_url = f"http://{get_local_ip()}:{FALLBACK_PORT}/audio/{Path(result['audio_file']).name}"

        mqtt_publish("antigua/response", {"text": result.get("response", ""), "audio_url": audio_url})
        mqtt_publish("antigua/status", {"state": "ready"})

        self._json(200, {
            "transcript": result.get("transcript", ""),
            "response": result.get("response", ""),
            "audio_url": audio_url,
            "action": result.get("action", ""),
            "stt_time": result.get("stt_time", 0),
            "llm_time": result.get("llm_time", 0),
            "conversation_id": result.get("conversation_id", ""),
            "streaming": result.get("streaming", False),
            "end_conversation": result.get("end_conversation", False),
        })

    def _handle_activate(self):
        Thread(target=_activate, args=("satellite",), daemon=True).start()
        self._json(200, {"status": "activating"})

    def _handle_deactivate(self):
        Thread(target=_deactivate, args=("satellite",), daemon=True).start()
        self._json(200, {"status": "deactivating"})

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


# ── Activation (primary down → load STT + LLM; primary back → release) ──────

_active = False
_active_lock = Lock()


def _ollama_load(keep_alive):
    """keep_alive=-1 loads and pins the model; 0 unloads it."""
    requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json={"model": OLLAMA_MODEL, "prompt": "", "keep_alive": keep_alive,
              "options": {"num_ctx": OLLAMA_NUM_CTX}},
        timeout=60,
    ).raise_for_status()


def _activate(reason: str):
    global _active, _llm_warm
    with _active_lock:
        if _active:
            return
        _active = True
    log.warning("ACTIVATING fallback (%s) — loading STT + %s", reason, OLLAMA_MODEL)
    mqtt_publish("antigua/status", {"state": "fallback_active"})
    snap = _load_primary_timers()
    timers.adopt(snap["timers"], primary_alive_until=snap["alive_until"])
    mcp_hub.warm()
    pipeline.B.home.warm()
    if music is not None:
        music.refresh_names()   # names STT may mishear, for re-hearing
    try:
        _get_whisper()
        _ollama_load(-1)
        prime_llm()
        _llm_warm = True
        log.info("Fallback ready: STT + LLM warm")
    except Exception as e:
        log.warning("Fallback warm-up failed (will load on first request): %s", e)


def _deactivate(reason: str):
    global _active, _llm_warm, _whisper_model
    with _active_lock:
        if not _active:
            return
        _active = False
    log.info("Standing down (%s) — releasing STT + LLM, back to TTS-only", reason)
    mqtt_publish("antigua/status", {"state": "primary_restored"})
    timers.drop_adopted()
    mcp_hub.close()
    try:
        _ollama_load(0)
    except Exception as e:
        log.warning("LLM unload failed: %s", e)
    _llm_warm = False
    with _whisper_lock:
        _whisper_model = None


def _primary_up() -> bool:
    try:
        return requests.get(PRIMARY_HEALTH_URL, timeout=3).status_code == 200
    except Exception:
        return False


def _load_primary_timers() -> dict:
    try:
        return json.loads(PRIMARY_TIMERS_PATH.read_text())
    except Exception:
        return {"timers": [], "alive_until": 0}


def _mirror_primary_timers():
    """Copy the primary's timers; alive_until records that it was answering
    now, i.e. anything due before this has been rung by it."""
    try:
        resp = requests.get(PRIMARY_TIMERS_URL, timeout=3)
        resp.raise_for_status()
        snap = {"timers": resp.json()["timers"], "alive_until": time.time()}
    except Exception as e:
        log.debug("Timer mirror failed: %s", e)
        return
    tmp = PRIMARY_TIMERS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap))
    tmp.replace(PRIMARY_TIMERS_PATH)


def primary_watch_loop():
    """Take over on our own — don't depend on the satellite noticing."""
    misses = hits = 0
    while True:
        if _primary_up():
            if not _active:
                _mirror_primary_timers()
            hits, misses = hits + 1, 0
            if _active and hits >= PRIMARY_UP_AFTER:
                _deactivate("primary healthy")
        else:
            misses, hits = misses + 1, 0
            if not _active and misses >= PRIMARY_DOWN_AFTER:
                _activate("primary /health unreachable")
        time.sleep(PRIMARY_POLL_S)


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    log.info("Antigua Fallback Server starting (port %d, model %s)...", FALLBACK_PORT, OLLAMA_MODEL)
    mqtt_connect()
    Thread(target=cleanup_loop, daemon=True).start()

    timers._load()

    Thread(target=nws_alerts_cache.get, daemon=True).start()
    for src in [s["name"] for s in NEWS_SOURCES]:
        Thread(target=news_cache.get, args=(src,), daemon=True).start()

    Thread(target=primary_watch_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", FALLBACK_PORT), FallbackHandler)
    log.info("HTTP on 0.0.0.0:%d", FALLBACK_PORT)
    log.info("Mode:    TTS-only until %s is down", PRIMARY_HEALTH_URL)
    log.info("STT:     faster-whisper tiny (CPU)")
    log.info("Ollama:  %s @ %s", OLLAMA_MODEL, OLLAMA_HOST)
    log.info("TTS:     %s", REMOTE_TTS_URL)
    log.info("Memory:  %s", core_settings.MEMORY_STORE_PATH)
    log.info("Search:  %s (via the primary)", core_settings.SEARCH_URL)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        server.server_close()


if __name__ == "__main__":
    main()
