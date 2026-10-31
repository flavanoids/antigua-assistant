#!/usr/bin/env python3
"""Antigua Fallback Server — runs on the backup box

Pipeline server activated when the primary is offline. Runs the same
antigua_core pipeline as the primary — classify(), every skill route, the
memory/timer/news/search stack — with a smaller backend injected:

  - STT:  faster-whisper small.en (CPU) instead of whisper.cpp
  - LLM:  antigua-4b-text, fallback.model, whole on the backup's GPU (Vulkan) —
          think=False, NO /no_think suffix (causes artifacts), streamed and
          split by sentence. ~0.55s to the first token warm, 24 tok/s.
  - No speaker ID.
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

import hashlib
import json
import logging
import os
import re
import socket
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
import requests
import yaml
from faster_whisper import WhisperModel

from antigua_core import household_memory, morning, pipeline
from antigua_core import security as security_skill
from antigua_core import server_common, turn_timing
from antigua_core import settings as core_settings
from antigua_core.caches import NewsCache, NWSAlertsCache, SearXNGSkill, WeatherCache
from antigua_core.classify import extract_weather_location
from antigua_core.knowledge import WikiKnowledge
from antigua_core.commute import CommuteProvider
from antigua_core import calc_currency
from antigua_core.pineda import TimerMirror
from antigua_core.pineda import connect as pineda_connect
from antigua_core.recipe import RecipeFinder
from antigua_core import sports as sports_skill
from antigua_core import weather as weather_skill
from antigua_core.pipeline import format_active_timers
from antigua_core.music_router import llm_route_music
from antigua_core.router import llm_route_search
from antigua_core.home_control import HomeControl
from antigua_core.mcp_client import TOOL_LOG, McpHub, read_env_file
from antigua_core.music import MusicControl
from antigua_core.alarm_http import AlarmRoutes
from antigua_core.stores import ConversationStore, ListStore, MemoryStore, TimerManager
from antigua_core.tts_text import clean_for_tts

_tts_session = requests.Session()

# ── Config ──────────────────────────────────────────────────────────────────

# Shared config (server.yaml deployed from the primary alongside this file)
CONFIG_PATH = core_settings.local_or_example(Path(__file__).parent / "config" / "server.yaml")

logging.basicConfig(
    level=logging.INFO,
    # Milliseconds, like the kitchen bridge's log, so a turn's stages line up.
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",   # dated: gpu_lend.sh reclaim counts only the lend's turns
)
log = logging.getLogger("antigua-fallback")


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


cfg = load_config()

# ── Fallback-specific overrides (ignore whatever server.yaml says for these) ──

FALLBACK_PORT = 9394
# fallback.model in server.yaml (server/scripts/use_model.sh --fallback sets
# it): a pinned copy of a model whose Modelfile sets num_gpu 99 and num_ctx, so
# every request (gates, routers, chat) keeps it whole on a small GPU. Ollama's
# default split ran the 4B at 9.7 tok/s on a 4 GB card instead of 24.
_FB = cfg.get("fallback") or {}
OLLAMA_MODEL = _FB.get("model") or "antigua-4b-text"
OLLAMA_NUM_CTX = int(_FB.get("num_ctx") or 8192)
FW_MODEL = "small.en"
# The backup GPU's runtime PM: amdgpu suspends it after 5 s idle, and waking it
# stalls the next request ~2 s. "on" while standing in, "auto" after.
GPU_PM_CONTROL = Path("/sys/bus/pci/devices/0000:01:00.0/power/control")
REMOTE_TTS_URL = "http://localhost:5500/tts"          # same box on the backup
PRIMARY_HOST = "10.77.0.1"                            # the primary over wg0
PRIMARY_HEALTH_URL = f"http://{PRIMARY_HOST}:9393/health"
PRIMARY_TIMERS_URL = f"http://{PRIMARY_HOST}:9393/timers"
PRIMARY_HANDOFF_URL = f"http://{PRIMARY_HOST}:9393/handoff"
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
# The primary's SearXNG dies with it, so this box runs its own
# (searxng/docker-compose.yml, installed by deploy_fallback.sh) on the same
# localhost:8080 server.yaml names. Search and recipes stay up in an outage.
core_settings.SEARCH_URL = "http://localhost:8080"

# ── MQTT ─────────────────────────────────────────────────────────────────────

mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="antigua-fallback")


def _on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    client.subscribe([("antigua/alarm_ack", 1), ("antigua/unducked", 1)])   # resubscribe on every reconnect


def _on_mqtt_message(client, userdata, msg):
    # The satellite stopped ringing one: "stop" after it means the music again.
    if msg.topic == "antigua/alarm_ack":
        try:
            pipeline.note_alarm_ack(json.loads(msg.payload).get("id", ""))
        except (ValueError, AttributeError):
            pass
    # The kitchen duck lifted: rewind a podcast that started under it.
    elif msg.topic == "antigua/unducked" and music is not None:
        try:
            since = float(json.loads(msg.payload).get("since"))
        except (ValueError, AttributeError, TypeError):
            return
        Thread(target=music.on_unducked, args=(since,), daemon=True, name="music-rewind").start()


def mqtt_connect():
    mqtt_client.on_connect = _on_mqtt_connect
    mqtt_client.on_message = _on_mqtt_message
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
knowledge = WikiKnowledge() if core_settings.KNOWLEDGE_ENABLED else None
# The same internet-backed skills as the primary — none of them need its GPU.
weather_provider = weather_skill.WeatherProvider(home=weather_skill.Location(
    lat=core_settings.WEATHER_HOME_LAT, lon=core_settings.WEATHER_HOME_LON,
    tz=core_settings.WEATHER_HOME_TZ, kind="home"))
rates_provider = calc_currency.RatesProvider() if core_settings.CALC_CURRENCY_ENABLED else None
sports_provider = sports_skill.SportsProvider() if core_settings.SPORTS_ENABLED else None
recipes = (RecipeFinder(core_settings.RECIPE_CACHE_DAYS)
           if core_settings.RECIPE_ENABLED and core_settings.SEARCH_ENABLED else None)
# pineda.url is localhost:8090 on both boxes: each runs a pineda-web, and the
# kiosk fails over to this one's when the primary is down.
pineda = (pineda_connect(core_settings.PINEDA_URL, token_file=core_settings.PINEDA_TOKEN_FILE,
                         device=core_settings.PINEDA_DEVICE)
          if core_settings.PINEDA_URL else None)
conversations = ConversationStore()
memory_store = MemoryStore()
list_store = ListStore()
timers = TimerManager(
    on_fire=lambda label, kind="timer", message=None: _on_timer_fire(label, kind, message),
    on_change=lambda: timer_mirror and timer_mirror.push(),
)
# While standing in, the alarms go to both pineda-webs: this box's (the kiosk's
# while noegpu01 is down) and the primary's (when only Antigua is down there).
timer_mirror = (TimerMirror(
    [pineda, pineda_connect(f"http://{PRIMARY_HOST}:8090", token_file=core_settings.PINEDA_TOKEN_FILE)],
    timers.list_active,
    in_charge=lambda: _active) if pineda else None)

# The Eufy cameras, the door chime and the back-door good morning, as on the
# primary (antigua_core/security.py), but only while standing in: started on
# activation and stopped on stand-down, so the two never both announce.
security = None
_sec_cfg = cfg.get("security") or {}
if _sec_cfg.get("ha_url"):
    _ha_token = security_skill.read_token(_sec_cfg.get("token_file", "/etc/antigua/ha.env"))
    if _ha_token:
        security = security_skill.Security(
            security_skill.HomeAssistant(_sec_cfg["ha_url"], _ha_token),
            security_skill.Config.from_dict(_sec_cfg),
            security_skill.EventLog(core_settings.DATA_DIR / "security_events.db"),
            pineda=pineda,
            announce=lambda text: _on_security_announce(text),
            chime=lambda: _play_static("door_chime.wav"),
        )
        if (cfg.get("morning") or {}).get("door"):
            _morning = morning.MorningGreeting(morning.Config.from_dict(cfg["morning"]),
                                               announce=lambda text: _on_security_announce(text))
            security.listen(_morning.cfg.door, _morning.on_state)


def _on_security_announce(text: str) -> None:
    wav_path = synthesize(text)
    if wav_path:
        mqtt_publish("antigua/play", {
            "audio_url": f"http://{get_local_ip()}:{FALLBACK_PORT}/audio/{Path(wav_path).name}"})
        log.info("Security: announced %r", text)


def _play_static(name: str) -> None:
    mqtt_publish("antigua/play", {"audio_url": f"http://{get_local_ip()}:{FALLBACK_PORT}/audio/{name}"})


def _on_timer_fire(label: str, kind: str = "timer", message: str | None = None):
    fire_id = uuid.uuid4().hex[:8]
    pipeline.note_alarm_fired(label, kind, message, fire_id)
    if pipeline.B.home is not None:
        Thread(target=pipeline.B.home.govee_flash, args=(kind,), daemon=True).start()
    text = server_common.timer_fire_text(label, kind, message)
    wav_path = synthesize(text)
    if wav_path:
        audio_url = f"http://{get_local_ip()}:{FALLBACK_PORT}/audio/{Path(wav_path).name}"
        mqtt_publish(
            "antigua/alarm",
            {"text": text, "audio_url": audio_url, "label": label, "kind": kind, "id": fire_id},
        )


# ── STT (faster-whisper) ─────────────────────────────────────────────────────

_whisper_model = None
_whisper_lock = Lock()


def _get_whisper():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            # small.en, not tiny: 2.0 s for a 5.7 s clip on this CPU, and
            # the bigger model for the far-field kitchen mic (2026-10-25).
            log.info("Loading faster-whisper %s model...", FW_MODEL)
            _whisper_model = WhisperModel(FW_MODEL, device="cpu", compute_type="int8")
            log.info("faster-whisper %s ready", FW_MODEL)
        return _whisper_model


# Whisper's stock outros for noise it can't place; tiny turned a clatter into
# "Thanks for watching!" and the LLM answered it.
_PHANTOM_RE = re.compile(
    r"^\W*(thanks? (you )?for watching|see (ya|you)( next time)?|"
    r"(please )?(like and )?subscribe|bye( bye)?)\W*$", re.I)


def transcribe(audio_path, hotwords=None, prefer=None):   # English only here
    t0 = time.time()
    model = _get_whisper()
    segments, _ = model.transcribe(
        audio_path, language="en", hotwords=hotwords,
        initial_prompt=cfg.get("whisper", {}).get("prompt") or None,
        # As on the primary: no temperature fallback, which retries noise
        # into confident-sounding sentences.
        temperature=0.0,
    )
    # A segment that's both likely silence and a low-confidence guess is noise.
    text = " ".join(s.text for s in segments
                    if not (s.no_speech_prob > 0.3 and s.avg_logprob < -1.0)).strip()
    if _PHANTOM_RE.match(text):
        log.info("STT dropped phantom %r", text)
        text = ""
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

    system_with_context = _system_content(transcript, extra_context)
    if not messages or messages[0]["role"] != "system":
        messages.insert(0, {"role": "system", "content": system_with_context})
    else:
        messages[0]["content"] = system_with_context

    # Small qwen: clean user message — no /no_think suffix (causes literal meta-commentary)
    messages.append({"role": "user", "content": transcript})
    return messages


def _system_content(transcript, extra_context=None):
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
    return system_with_context + household_memory.store().rules_block()


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


def write_json(prompt: str) -> str:
    """One non-streamed answer in JSON mode (research.py plans and answers
    with it). Same model, context and keep-alive as the chat calls."""
    resp = requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_predict": 1200, "num_ctx": OLLAMA_NUM_CTX},
            "keep_alive": OLLAMA_KEEP_ALIVE,
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json().get("response") or ""


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
    global _primed_system
    messages = _build_messages("Hello")
    requests.post(f"{OLLAMA_HOST}/api/chat", json=_llm_payload(messages, max_tokens_override=1),
                  timeout=60).raise_for_status()
    _primed_system = messages[0]["content"]


# The system message changes with the hour, the weather, timers and alerts;
# the first chat after that re-reads all of it (~174 tok/s here: 5 s for the
# 500 tokens after the weather line). Re-priming in the background moves that
# off the next turn. Never within a couple of minutes of a chat turn: priming
# replaces the cache that conversation's follow-ups would reuse.
_primed_system = ""
_last_chat_at = 0.0
REPRIME_QUIET_S = 120


def _reprime_if_stale():
    if (_llm_warm and time.time() - _last_chat_at > REPRIME_QUIET_S
            and _system_content("Hello") != _primed_system):
        t0 = time.time()
        prime_llm()
        log.info("Re-primed the LLM prompt cache (%.1fs)", time.time() - t0)


def _note_llm_stats(msg):
    """Ollama's own numbers for the reply, on the TURN line. prompt_tok is the
    whole prompt, cached or not; prompt_s is the time spent on the uncached
    tail (Ollama's log says how many tokens that was), and on this hybrid model
    it includes restoring and saving a context checkpoint."""
    turn_timing.note("prompt_tok", msg.get("prompt_eval_count", 0))
    turn_timing.note("prompt_s", f"{msg.get('prompt_eval_duration', 0) / 1e9:.2f}")
    if msg.get("eval_duration"):
        turn_timing.note("gen_tps", f"{msg.get('eval_count', 0) / (msg['eval_duration'] / 1e9):.1f}")


_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])["\'’”)]*\s+')


def ask_llm_stream(transcript, conversation_id="", extra_context=None,
                   max_tokens_override=None, temperature_override=None,
                   grounding_context=None, grounding_mode=None, language="en",
                   turn_hint=None):
    """Stream from Ollama and yield whole sentences as they complete, so the
    pipeline starts TTS on the first sentence while the rest generates.
    Grounding context is folded into the prompt; the small model gets no
    separate grounding pass. `grounding_mode` is accepted for signature
    parity and ignored: there is never a recipe session here."""
    global _last_chat_at
    t0 = _last_chat_at = time.time()
    messages = _build_messages(transcript, conversation_id, extra_context or grounding_context)
    if turn_hint:  # persona.py's stage direction; history keeps the bare transcript
        messages[-1]["content"] += "".join(f"\n({h})" for h in turn_hint.split("\n") if h)
    payload = _llm_payload(messages, max_tokens_override, temperature_override, stream=True)
    buf = full = ""
    try:
        # (connect, first token): this box is slower than the primary,
        # but the turn must still end inside the kitchen bridge's 60s.
        with requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=(2, 40), stream=True) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("done"):
                    _note_llm_stats(msg)
                piece = msg.get("message", {}).get("content", "")
                buf += piece
                full += piece
                *done, buf = _SENTENCE_SPLIT.split(buf)
                for sentence in filter(None, (d.strip() for d in done)):
                    yield sentence
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


def route_music_with_llm(transcript: str):
    """Restate a music request no pattern knew as a command parse_music does."""
    return llm_route_music(
        transcript,
        ollama_host=OLLAMA_HOST,
        model=OLLAMA_MODEL,
        timeout=SEARCH_ROUTER_TIMEOUT,
        keep_alive=OLLAMA_KEEP_ALIVE,
        num_ctx=OLLAMA_NUM_CTX,
        enabled=bool(cfg.get("music", {}).get("llm_fallback", True)),
    )


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


# As on the primary: replies are cached by content, so fixed ones (TV, timer
# and music confirmations) skip Kokoro, which is 2.4x slower here. A hit
# refreshes the file; prune_audio drops it a week after its last use.
_TTS_CACHE_TTL = 7 * 86400  # seconds
_TTS_CACHE_MAX_BYTES = 200 * 1024 * 1024


def synthesize(text: str, lang: str = "en") -> str:
    """Synthesize text via Kokoro TTS on localhost. Returns path to wav file."""
    text = clean_for_tts(text, lang)
    key = text if lang == "en" else f"{lang}:{text}"
    out_path = AUDIO_OUT_DIR / f"cache_{hashlib.sha256(key.encode()).hexdigest()[:16]}.wav"
    if out_path.exists() and time.time() - out_path.stat().st_mtime < _TTS_CACHE_TTL:
        os.utime(out_path)
        log.info("TTS cache hit: %s", out_path.name)
        return str(out_path)
    t0 = time.time()
    tmp = AUDIO_OUT_DIR / f"{uuid.uuid4().hex[:12]}.wav"
    try:
        resp = _tts_session.post(REMOTE_TTS_URL, json={"text": text, "lang": lang}, timeout=20)
        resp.raise_for_status()
        tmp.write_bytes(resp.content)
        tmp.replace(out_path)   # whole files only: a concurrent hit never reads a partial one
        log.info("TTS: %.2fs -> %s", time.time() - t0, out_path.name)
        return str(out_path)
    except requests.ConnectionError:
        log.error("TTS unreachable: %s", REMOTE_TTS_URL)
    except requests.Timeout:
        log.error("TTS timeout after 20s")
    except requests.HTTPError as exc:
        log.error("TTS HTTP error: %s", exc)
    return ""


# Jellyfin shares this box: a slow turn under a transcode shows as high load.
turn_timing.extras = lambda: {"host": socket.gethostname(), "load": f"{os.getloadavg()[0]:.1f}"}


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

TOOL_LOG.open(core_settings.DATA_DIR / "tool_calls.db")
mcp_hub = McpHub.from_config(cfg.get("mcp"), core_settings.BASE_DIR)

# Music Assistant (Apple Music). The token lives in the same gitignored env
# file as the MCP secrets; without it the music route says it can't play.
_ma_token = read_env_file(core_settings.BASE_DIR / cfg.get("mcp", {}).get(
    "env_file", "server/config/mcp.env")).get("MUSIC_ASSISTANT_TOKEN")
music = MusicControl(cfg, _ma_token) if _ma_token and cfg.get("music", {}).get("enabled", True) else None

# Drive times from home: places.yaml (home + favorites), Photon for places,
# TomTom (TOMTOM_API_KEY in the mcp env file) or else OSRM for routes, and
# Houston TranStar for live traffic.
commute = (CommuteProvider(read_env_file(core_settings.BASE_DIR / cfg.get("mcp", {}).get(
    "env_file", "server/config/mcp.env")).get("TOMTOM_API_KEY"))
           if core_settings.COMMUTE_ENABLED else None)


def remember_exchange(conversation_id, transcript, reply):
    conversations.add_message(conversation_id, "user", transcript)
    conversations.add_message(conversation_id, "assistant", reply)


# The primary owns the household memory file; here it's only read.
household_memory.set_store(household_memory.HouseholdMemory(read_only=True))

pipeline.init(pipeline.Backend(
    transcribe=transcribe,
    synthesize=synthesize,
    ask_llm_stream=ask_llm_stream,
    audio_url_base=lambda: f"http://{get_local_ip()}:{FALLBACK_PORT}",
    memory_store=memory_store,
    list_store=list_store,
    timers=timers,
    weather_cache=weather_cache,
    weather_provider=weather_provider,
    rates_provider=rates_provider,
    sports_provider=sports_provider,
    news_cache=news_cache,
    searxng=searxng,
    route_search_with_llm=route_search_with_llm,
    route_music_with_llm=route_music_with_llm,
    home=HomeControl(mcp_hub, cfg),
    music=music,
    commute=commute,
    pineda=pineda,
    security=security,
    knowledge=knowledge,
    recipes=recipes,
    remember_exchange=remember_exchange,
    write_json=write_json,
    static_audio_dir=STATIC_AUDIO_DIR,
))

run_pipeline = pipeline.run_pipeline


# ── Background Cleanup ────────────────────────────────────────────────────────


def cleanup_loop():
    while True:
        time.sleep(60)
        try:
            removed = server_common.prune_audio(AUDIO_OUT_DIR, AUDIO_TTL, _TTS_CACHE_TTL,
                                                _TTS_CACHE_MAX_BYTES)
            if removed:
                log.debug("Cleaned %d old audio files", removed)
            conversations.cleanup()
            pipeline.cleanup_pending()
            _reprime_if_stale()
        except Exception:
            log.exception("Cleanup error")


# ── HTTP Server ───────────────────────────────────────────────────────────────


class FallbackHandler(AlarmRoutes, BaseHTTPRequestHandler):
    alarm_timers = timers

    def alarms_ready(self) -> bool:
        return _active   # otherwise the primary has them

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
            filepath = server_common.audio_file(parsed.path[len("/audio/"):],
                                                AUDIO_OUT_DIR, STATIC_AUDIO_DIR)
            if filepath is not None:
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
        elif self.route_alarms("GET"):
            pass
        else:
            self._json(404, {"error": "not found"})

    def do_PATCH(self):
        if not self.route_alarms("PATCH"):
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if not self.route_alarms("DELETE"):
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/pipeline":
            self._handle_pipeline()
        elif parsed.path == "/pipeline_text":
            self._handle_pipeline_text()
        elif parsed.path == "/activate":
            self._handle_activate()
        elif parsed.path == "/deactivate":
            self._handle_deactivate()
        elif self.route_alarms("POST"):
            pass
        else:
            self._json(404, {"error": "not found"})

    def _conv_id(self):
        return self.headers.get("X-Conversation-ID", "")

    def _handle_pipeline(self):
        # A turn can beat the watcher here (e.g. the primary bridge fails over
        # the moment antigua-server dies). Mark us active so the watcher is
        # the one that later unloads whatever this request loads.
        Thread(target=_activate, args=("pipeline request",), daemon=True).start()
        audio = server_common.read_body(self, server_common.MAX_AUDIO_BYTES)
        if audio is None:
            return
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
            "expects_reply": result.get("expects_reply", False),
            "starts_playback": result.get("starts_playback", False),
        })

    def _handle_pipeline_text(self):
        """QA seam, as on the primary: the full classify + dispatch on a text
        string, skipping STT. Silent by default (no antigua/play)."""
        body = server_common.read_body(self, server_common.MAX_JSON_BYTES)
        if body is None:
            return
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return
        text = (data.get("text") or "").strip()
        if not text:
            self._json(400, {"error": "empty text"})
            return
        t0 = time.time()
        result = pipeline.dispatch_text(
            text,
            conversation_id=data.get("conversation_id", "") or self._conv_id(),
            follow_up=bool(data.get("follow_up", False)),
            quiet=bool(data.get("quiet", True)),
        )
        self._json(200, {
            "response": result.get("response", ""),
            "action": result.get("action", ""),
            "conversation_id": result.get("conversation_id", ""),
            "expects_reply": result.get("expects_reply", False),
            "total_s": round(time.time() - t0, 2),
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


def _hold_gpu_awake(awake: bool):
    try:
        GPU_PM_CONTROL.write_text("on" if awake else "auto")
    except OSError as e:
        log.warning("GPU runtime PM %s: %s", GPU_PM_CONTROL, e)


def _prewarm_skills():
    """The home forecast and exchange rates, so the first questions after a
    takeover don't wait on Open-Meteo / the ECB."""
    try:
        weather_provider.prewarm(core_settings.WEATHER_PREWARM_CITIES)
        if rates_provider is not None:
            rates_provider.prewarm()
    except Exception as e:
        log.warning("Skill prewarm failed: %s", e)


def _activate(reason: str):
    global _active, _llm_warm
    with _active_lock:
        if _active:
            return
        _active = True
    log.warning("ACTIVATING fallback (%s) — loading STT + %s", reason, OLLAMA_MODEL)
    mqtt_publish("antigua/status", {"state": "fallback_active"})
    # The primary's latest memories and lists (copied here by its cron), not
    # whatever this process loaded at boot.
    memory_store.reload()
    list_store.reload()
    snap = _load_primary_timers()
    timers.adopt(snap["timers"], primary_alive_until=snap["alive_until"])
    _taken.update(timers=timers.revision, memories=_digest(memory_store.export()),
                  lists=_digest(list_store.all_lists()))
    if timer_mirror:
        timer_mirror.push()
    mcp_hub.warm()
    pipeline.B.home.warm()
    if security is not None:
        security.start()
    if music is not None:
        music.refresh_names()   # names STT may mishear, for re-hearing
    Thread(target=_prewarm_skills, daemon=True, name="prewarm").start()
    _hold_gpu_awake(True)
    try:
        _get_whisper()
        _ollama_load(-1)
        prime_llm()
        _llm_warm = True
        log.info("Fallback ready: STT + LLM warm")
    except Exception as e:
        log.warning("Fallback warm-up failed (will load on first request): %s", e)


# What this box had when it took over, to tell on the way out whether it has
# anything to hand back.
_taken: dict = {}


def _digest(data) -> str:
    return json.dumps(data, sort_keys=True)


def _hand_back() -> bool:
    """Give the primary what changed here while it was down: the whole alarm
    list (set, changed, rung or deleted by voice or the phone), and memories
    and lists if those changed. False if it can't be reached — stay active."""
    body = {}
    if timers.revision != _taken.get("timers"):
        body["timers"] = timers.export_all()
    if _digest(memory_store.export()) != _taken.get("memories"):
        body["memories"] = memory_store.export()
    if _digest(list_store.all_lists()) != _taken.get("lists"):
        body["lists"] = list_store.all_lists()
    if not body:
        return True
    try:
        requests.post(PRIMARY_HANDOFF_URL, json=body, timeout=5).raise_for_status()
    except Exception as e:
        log.warning("Hand-back to the primary failed, staying active: %s", e)
        return False
    log.warning("Handed back to the primary: %s", ", ".join(body))
    if "timers" in body:
        _handed_timers.set()
    return True


_handed_timers = Event()


def _deactivate(reason: str):
    global _active, _llm_warm, _whisper_model
    with _active_lock:
        if not _active:
            return
    _handed_timers.clear()
    if not _hand_back():
        return
    with _active_lock:
        _active = False
    if _handed_timers.is_set():
        # The primary has them all now, including ones set here; keeping
        # them would ring them twice.
        timers.cancel_all()
    log.info("Standing down (%s) — releasing STT + LLM, back to TTS-only", reason)
    mqtt_publish("antigua/status", {"state": "primary_restored"})
    timers.drop_adopted()
    mcp_hub.close()
    if security is not None:
        security.stop()
    try:
        _ollama_load(0)
    except Exception as e:
        log.warning("LLM unload failed: %s", e)
    _hold_gpu_awake(False)
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
    if timer_mirror:
        timer_mirror.keep_fresh()

    Thread(target=nws_alerts_cache.get, daemon=True).start()
    for src in [s["name"] for s in NEWS_SOURCES]:
        Thread(target=news_cache.get, args=(src,), daemon=True).start()

    Thread(target=primary_watch_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", FALLBACK_PORT), FallbackHandler)
    log.info("HTTP on 0.0.0.0:%d", FALLBACK_PORT)
    log.info("Mode:    TTS-only until %s is down", PRIMARY_HEALTH_URL)
    log.info("STT:     faster-whisper %s (CPU)", FW_MODEL)
    log.info("Ollama:  %s @ %s", OLLAMA_MODEL, OLLAMA_HOST)
    log.info("TTS:     %s", REMOTE_TTS_URL)
    log.info("Memory:  %s", core_settings.MEMORY_STORE_PATH)
    log.info("Search:  %s (local SearXNG)", core_settings.SEARCH_URL)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        server.server_close()


if __name__ == "__main__":
    main()
