#!/usr/bin/env python3
"""Antigua Voice Assistant Server — runs on the primary

Receives audio from the Pi satellite, runs STT -> LLM -> TTS pipeline,
maintains conversation context for follow-up questions, publishes results
via MQTT, and serves audio files over HTTP.
"""

import hashlib
import json
import logging
import os
import re
import time
import uuid
import wave
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock, Thread, Timer
from urllib.parse import parse_qs, urlparse

import paho.mqtt.client as mqtt
import requests
import yaml

from antigua_core import server_common
from antigua_core import settings as core_settings
from antigua_core.caches import NewsCache, NWSAlertsCache, SearXNGSkill, WeatherCache
from antigua_core.commute import CommuteProvider
from antigua_core import calc_currency, chat, household_memory, netstatus, sysinfo
from antigua_core.security import in_hours
from antigua_core.knowledge import WikiKnowledge
from antigua_core.recipe import RecipeFinder
from antigua_core import sports as sports_skill
from antigua_core import weather as weather_skill
from antigua_core.classify import (
    ROUTE_ORDER,  # noqa: F401 — re-exported for tests
    classify,  # noqa: F401 — re-exported for tests
    wants_weather_context,
)
from antigua_core import pipeline
from antigua_core.home_control import HomeControl
from antigua_core.mcp_client import TOOL_LOG, McpHub, read_env_file
from antigua_core.music import MusicControl
from antigua_core.pineda import TimerMirror
from antigua_core.pineda import connect as pineda_connect
from antigua_core import morning
from antigua_core import security as security_skill
from antigua_core.grounding import recipe_claims_ok, unsupported_claims, weather_claim_allowed
from antigua_core.pipeline import (
    format_active_timers,
)
from antigua_core.music_router import llm_route_music
from antigua_core.router import llm_route_search
from antigua_core.speaker_id import SpeakerProfiles
from antigua_core.alarm_http import AlarmRoutes
from antigua_core import announce as announce_mod
from antigua_core import phone_http
from antigua_core import presence as presence_mod
from antigua_core.announce import Announcer
from antigua_core.presence import ArrivalReminders, Presence
from antigua_core.stores import ConversationStore, ListStore, MemoryStore, TimerManager
from antigua_core.tts_text import clean_for_tts

_tts_session = requests.Session()  # reuse TCP connections to the Kokoro servers

# ── Config ──────────────────────────────────────────────────────────────────

# server.yaml is local (git-ignored); a fresh clone falls back to
# server.example.yaml. ANTIGUA_CONFIG overrides (the tests use it).
CONFIG_PATH = core_settings.local_or_example(
    os.environ.get("ANTIGUA_CONFIG") or Path(__file__).parent / "config" / "server.yaml"
)
logging.basicConfig(
    level=logging.INFO,
    # Milliseconds, like the kitchen bridge's log, so a turn's stages line up.
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("antigua")


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


cfg = load_config()

if "env" in cfg and "VK_ICD_FILENAMES" in cfg["env"]:
    os.environ["VK_ICD_FILENAMES"] = cfg["env"]["VK_ICD_FILENAMES"]

# STT via faster-whisper (replaces whisper.cpp CLI — original repo gone)
WHISPER_LANG = cfg["whisper"].get("language", "en")
WHISPER_PROMPT = cfg["whisper"].get("prompt", "")
WHISPER_FW_MODEL = cfg["whisper"].get("fw_model", "small.en")
WHISPER_BEAM = int(cfg["whisper"].get("beam_size", 1))
WHISPER_CAREFUL_MODEL = cfg["whisper"].get("fw_model_careful", "medium.en")
# whisper.cpp's whisper-server on the GPU (antigua-whisper.service,
# large-v3-turbo): the primary STT when set; faster-whisper on the CPU is the
# fallback when it's down and handles hotword re-hearing (the server has no
# hotwords, and its prompt didn't bias names).
WHISPER_GPU_URL = cfg["whisper"].get("gpu_url", "")
# Spanish only on clear evidence: short or noisy clips score low ("Pause" over
# music: en 0.72, and 0.16 Turkish), and a wrong flip answers an English
# command in Spanish. A conversation already in Spanish stays there on any
# Spanish-leaning turn.
SPANISH_MIN_PROB = float(cfg["whisper"].get("spanish_min_prob", 0.85))
SPANISH_MIN_WORDS = int(cfg["whisper"].get("spanish_min_words", 3))
# Whisper's language detection is a second encoder pass: 0.59s vs 0.30s, on
# every turn. Off, every turn is transcribed as English; a household
# reply_language preference still answers in Spanish.
WHISPER_DETECT_LANGUAGE = bool(cfg["whisper"].get("detect_language", True))
_fwhisper_careful = None
_fwhisper_model = None
_fwhisper_lock = Lock()


def _get_fwhisper():
    global _fwhisper_model
    with _fwhisper_lock:
        if _fwhisper_model is None:
            from faster_whisper import WhisperModel
            log.info("Loading faster-whisper %s model (CPU, int8)...", WHISPER_FW_MODEL)
            _fwhisper_model = WhisperModel(WHISPER_FW_MODEL, device="cpu", compute_type="int8")
            log.info("faster-whisper %s ready", WHISPER_FW_MODEL)
        return _fwhisper_model

WAKE_WORDS = [w.lower() for w in cfg.get("assistant", {}).get("wake_words", ["alexa"])]
_DISPLAY_COMMANDS = cfg.get("assistant", {}).get("display_commands", [])

_WAKE_PREFIX_RE = (
    re.compile(
        r"^(?:(?:" + "|".join(re.escape(w) for w in WAKE_WORDS) + r")[,.\s]*)+",
        re.IGNORECASE,
    )
    if WAKE_WORDS else None
)

OLLAMA_HOST = cfg["ollama"]["host"]
OLLAMA_MODEL = cfg["ollama"]["model"]
OLLAMA_MAX_TOKENS = cfg["ollama"]["max_tokens"]
OLLAMA_TEMP = cfg["ollama"]["temperature"]
OLLAMA_NUM_CTX = cfg["ollama"].get("num_ctx", 8192)
OLLAMA_KEEP_ALIVE = cfg["ollama"]["keep_alive"]
OLLAMA_INTERACTION_KEEP_ALIVE = cfg["ollama"].get("interaction_keep_alive", "15m")
SYSTEM_PROMT = core_settings.local_or_example(
    cfg["ollama"]["system_prompt_file"]
).read_text().strip()

AUDIO_OUT_DIR = Path(cfg["tts"]["output_dir"])
AUDIO_OUT_DIR.mkdir(parents=True, exist_ok=True)
STATIC_AUDIO_DIR = Path(__file__).parent / "static_audio"

REMOTE_TTS_URL = cfg.get("tts", {}).get("remote_url", "http://127.0.0.1:5500/tts")
# Tried when remote_url fails — the backup's Kokoro serves the same voice blend.
FALLBACK_TTS_URL = cfg.get("tts", {}).get("fallback_url", "")
# Delivery rate for the remote Kokoro voice (1.0 = default). Slower reads more
# naturally; the remote server clamps to a sane range.
TTS_SPEED = cfg.get("tts", {}).get("speed", 0.9)

AUDIO_TTL = cfg["server"]["audio_ttl_seconds"]

MQTT_BROKER = cfg["mqtt"]["broker"]
MQTT_PORT = cfg["mqtt"]["port"]

CONVERSATION_TTL = 300  # 5 minutes
MAX_HISTORY = 6

# Core modules read config through antigua_core.settings; must run before any
# core class is instantiated. mqtt_publish is wired up after it is defined.
core_settings.configure(cfg)


weather_cache = WeatherCache()

# Open-Meteo-backed weather skill: deterministic spoken answers for any location
# and timeframe. weather_cache (wttr.in) is kept only for the legacy LLM-prompt
# injection path and the fallback server.
weather_home = weather_skill.Location(
    lat=core_settings.WEATHER_HOME_LAT,
    lon=core_settings.WEATHER_HOME_LON,
    tz=core_settings.WEATHER_HOME_TZ,
    kind="home",
)
weather_provider = weather_skill.WeatherProvider(home=weather_home)


# Currency conversion for the calc skill — cached ECB reference rates.
rates_provider = calc_currency.RatesProvider() if core_settings.CALC_CURRENCY_ENABLED else None


# NFL / MLB / NBA / MLS / F1 scores — ESPN scoreboard/team data.
sports_provider = sports_skill.SportsProvider() if core_settings.SPORTS_ENABLED else None


nws_alerts_cache = NWSAlertsCache()


news_cache = NewsCache()


searxng = SearXNGSkill()

# People, history and events — Wikipedia articles + Wikidata facts.
knowledge = WikiKnowledge() if core_settings.KNOWLEDGE_ENABLED else None

# Recipes — real ones from recipe sites (JSON-LD), read step by step.
recipes = (RecipeFinder(core_settings.RECIPE_CACHE_DAYS)
           if core_settings.RECIPE_ENABLED and core_settings.SEARCH_ENABLED else None)


# PinedaDisplay Web — the airplaypi kiosk screen (antigua_core/pineda.py).
# Over MCP; the standby's when this box's pineda-web is down (the kiosk's
# nginx fails over to it too).
pineda = (
    pineda_connect(core_settings.PINEDA_URL, core_settings.PINEDA_STANDBY_URL,
                   token_file=core_settings.PINEDA_TOKEN_FILE, device=core_settings.PINEDA_DEVICE)
    if core_settings.PINEDA_URL else None
)

# The Eufy cameras and the front door lock through Home Assistant
# (antigua_core/security.py). The token stays in a root-only env file.
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
        )
        # Weekday "good morning" when the first trip out the back door comes
        # back in (antigua_core/morning.py); rides the same HA connection.
        if (cfg.get("morning") or {}).get("door"):
            _morning = morning.MorningGreeting(morning.Config.from_dict(cfg["morning"]),
                                               announce=lambda text: _on_security_announce(text))
            security.listen(_morning.cfg.door, _morning.on_state)
        security.start()

# Lambdas for late binding — _on_timer_fire, _on_security_announce and
# timer_mirror are defined below.
timers = TimerManager(
    on_fire=lambda label, kind="timer", message=None: _on_timer_fire(label, kind, message),
    on_change=lambda: timer_mirror and timer_mirror.push(),
)
# Running timers show in the display's moon-climate mini card.
# Both pineda-webs get them, so the standby's screen has them if the kiosk
# fails over to it.
timer_mirror = (TimerMirror(
    [pineda_connect(u, token_file=core_settings.PINEDA_TOKEN_FILE)
     for u in (core_settings.PINEDA_URL, core_settings.PINEDA_STANDBY_URL) if u],
    timers.list_active) if pineda else None)


def _speak_unprompted(text: str) -> bool:
    wav_path = synthesize(clean_for_tts(text))
    if wav_path:
        audio_url = (
            f"http://{get_local_ip()}:{cfg['server']['port']}"
            f"/audio/{Path(wav_path).name}"
        )
        mqtt_publish("antigua/play", {"audio_url": audio_url})
    return bool(wav_path)


def _on_security_announce(text: str) -> None:
    if _speak_unprompted(text):
        log.info("Security: announced %r", text)


# The internet going out is said once it has been out a while (a blip isn't
# news), and its return only if the outage was said. Not overnight.
_NET_SAY_AFTER = 120
_NET_SAY_HOURS = "07:00-23:00"


def _on_net_change(state, cause, outage):
    mqtt_client.publish("antigua/net", json.dumps(netstatus.monitor.snapshot()), qos=1, retain=True)
    if state == "offline":
        Timer(_NET_SAY_AFTER, _say_net_down, args=(outage,)).start()
    elif outage and outage.get("said") and in_hours(_NET_SAY_HOURS):
        text = (f"The internet's back. It was out for "
                f"{sysinfo.duration_words(outage['end'] - outage['start'])}.")
        if _speak_unprompted(text):
            log.info("Net: said %r", text)


def _say_net_down(outage):
    mon = netstatus.monitor
    if mon.state != "offline" or not mon.outages or mon.outages[-1] is not outage:
        return                                # back already, or a newer outage
    if not in_hours(_NET_SAY_HOURS):
        return
    text = (f"Heads up, the internet's out. It looks like {netstatus.CAUSES[mon.cause]}. "
            "Timers, alarms, the TV and most lights still work.")
    if _speak_unprompted(text):
        outage["said"] = True
        log.info("Net: said %r", text)


def _on_timer_fire(label: str, kind: str = "timer", message: str | None = None):
    fire_id = uuid.uuid4().hex[:8]
    pipeline.note_alarm_fired(label, kind, message, fire_id)  # for "snooze" / "stop"
    if pipeline.B.home is not None:
        Thread(target=pipeline.B.home.govee_flash, args=(kind,), daemon=True).start()
    text = server_common.timer_fire_text(label, kind, message)
    wav_path = synthesize(text)
    if wav_path:
        audio_url = (
            f"http://{get_local_ip()}:{cfg['server']['port']}"
            f"/audio/{Path(wav_path).name}"
        )
        # The satellite rings it until the wake word, "stop", or its cap; then
        # it publishes antigua/alarm_ack with this id.
        mqtt_publish(
            "antigua/alarm",
            {"text": text, "audio_url": audio_url, "label": label, "kind": kind, "id": fire_id},
        )


memory_store = MemoryStore()
list_store = ListStore()
speaker_profiles = SpeakerProfiles()


def route_music_with_llm(transcript: str):
    """Restate a music request no pattern knew as a command parse_music does."""
    return llm_route_music(
        transcript,
        ollama_host=OLLAMA_HOST,
        model=OLLAMA_MODEL,
        timeout=core_settings.SEARCH_ROUTER_TIMEOUT,
        keep_alive=OLLAMA_INTERACTION_KEEP_ALIVE,
        num_ctx=OLLAMA_NUM_CTX,
        enabled=bool(cfg.get("music", {}).get("llm_fallback", True)),
    )


def route_search_with_llm(transcript: str):
    """Ask the 4b model whether this needs the web. Returns a query, or None."""
    return llm_route_search(
        transcript,
        ollama_host=OLLAMA_HOST,
        model=OLLAMA_MODEL,
        timeout=core_settings.SEARCH_ROUTER_TIMEOUT,
        keep_alive=OLLAMA_INTERACTION_KEEP_ALIVE,
        num_ctx=OLLAMA_NUM_CTX,
        enabled=core_settings.SEARCH_ROUTER_ENABLED,
    )



def write_json(prompt: str) -> str:
    """One non-streamed answer in JSON mode (howto.py writes guides with it).
    Same model, context and keep-alive as the chat calls, so Ollama doesn't
    reload; a guide is a few hundred tokens out."""
    resp = requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_predict": 1200, "num_ctx": OLLAMA_NUM_CTX},
            "keep_alive": OLLAMA_INTERACTION_KEEP_ALIVE,
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json().get("response") or ""


# faster-whisper downloads its model automatically on first use — no local
# model path to validate. The old whisper.cpp startup check is gone.


conversations = ConversationStore()


def check_addressed(heard: str, last: str, after_wake: bool = False) -> bool:
    """Is this speech meant for Antigua? Conversation mode's guard, and the
    check after a fresh wake word on turns no skill claimed."""
    return chat.is_addressed(
        heard, last, after_wake=after_wake,
        ollama_host=OLLAMA_HOST,
        model=OLLAMA_MODEL,
        timeout=core_settings.CHAT_ADDRESSEE_TIMEOUT,
        keep_alive=OLLAMA_INTERACTION_KEEP_ALIVE,
        num_ctx=OLLAMA_NUM_CTX,
    )


def classify_memory(text: str):
    """Sorts a "remember ..." the guard's rules couldn't place; one word out."""
    if core_settings.MEMORY_CLASSIFIER != "local":
        return None
    return household_memory.ask_ollama(
        text, ollama_host=OLLAMA_HOST, model=OLLAMA_MODEL,
        timeout=core_settings.CHAT_ADDRESSEE_TIMEOUT,
        keep_alive=OLLAMA_INTERACTION_KEEP_ALIVE, num_ctx=OLLAMA_NUM_CTX)


# ── MQTT Client ─────────────────────────────────────────────────────────────

mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="antigua-server")


def _on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    # Resubscribe on every (re)connect — paho doesn't restore subscriptions.
    topics = [
        ("antigua/listening", 1),
        ("antigua/stop", 1),
        ("antigua/alarm_ack", 1),
        ("antigua/alarm_sync_request", 1),
    ]
    if security is not None:
        topics.extend([
            ("antigua/security/visitor", 1),
            ("double-take/matches", 1),
            ("frigate/events", 1),
        ])
    client.subscribe(topics)


def _handle_security_mqtt(topic: str, raw_payload: bytes | str) -> None:
    if security is None:
        return
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, (bytes, str)) else raw_payload
    except (ValueError, TypeError):
        log.debug("Security MQTT: non-JSON payload on %s", topic)
        return
    if not isinstance(payload, dict):
        return

    try:
        if topic == "antigua/security/visitor":
            cam = payload.get("camera", "front_door")
            people = payload.get("people")
            if isinstance(people, str):
                people = [people]
            has_unknown = bool(payload.get("has_unknown", False))
            security.announce_visitor(cam, people=people, has_unknown=has_unknown, source="mqtt:antigua")

        elif topic == "double-take/matches":
            cam = payload.get("camera", "front_door")
            name = payload.get("name")
            is_match = payload.get("match", True)
            if is_match and name:
                security.announce_visitor(cam, people=[name], source="mqtt:double-take")
            elif not is_match or not name:
                security.announce_visitor(cam, people=[], has_unknown=True, source="mqtt:double-take")

        elif topic == "frigate/events":
            after = payload.get("after") or {}
            if after.get("label") == "person":
                cam = after.get("camera", "front_door")
                sub_label = after.get("sub_label")
                sub_labels = after.get("sub_labels") or []
                people = []
                if sub_label:
                    people.append(sub_label)
                if isinstance(sub_labels, list):
                    for sl in sub_labels:
                        if isinstance(sl, dict) and "label" in sl:
                            people.append(sl["label"])
                        elif isinstance(sl, str):
                            people.append(sl)
                security.announce_visitor(cam, people=people, source="mqtt:frigate")
    except Exception:
        log.exception("Security MQTT: failed processing %s", topic)


def _on_mqtt_message(client, userdata, msg):
    # Music ducking: the kitchen bridge says when the user starts and stops
    # talking (wake word / follow-up speech → reSpeaker VAD end of speech).
    # The wake word interrupting a reply: stop publishing what's left of it.
    if msg.topic == "antigua/stop":
        pipeline.request_stop()
        return
    if msg.topic == "antigua/alarm_ack":
        try:
            pipeline.note_alarm_ack(json.loads(msg.payload).get("id", ""))
        except (ValueError, AttributeError):
            pass
        return
    if msg.topic == "antigua/alarm_sync_request":
        try:
            timers.publish_schedule()
        except Exception:
            log.exception("Failed to publish alarm schedule on request")
        return
    if security is not None and msg.topic in (
        "antigua/security/visitor",
        "double-take/matches",
        "frigate/events",
    ):
        _handle_security_mqtt(msg.topic, msg.payload)
        return
    if music is None or msg.topic != "antigua/listening":
        return
    try:
        active = bool(json.loads(msg.payload).get("active"))
    except (ValueError, AttributeError):
        return
    music.on_listening(active)


def mqtt_connect():
    mqtt_client.on_connect = _on_mqtt_connect
    mqtt_client.on_message = _on_mqtt_message
    server_common.mqtt_connect(mqtt_client, MQTT_BROKER, MQTT_PORT)


def mqtt_publish(topic, payload):
    # PinedaDisplay state topics are suppressed unless display.enabled is set.
    if not server_common.display_allows(topic):
        return
    mqtt_client.publish(topic, json.dumps(payload), qos=1)


# Core modules (TimerManager etc.) publish through this hook.
core_settings.mqtt_publish = mqtt_publish


# ── STT (whisper.cpp) ───────────────────────────────────────────────────────


# large-v3-turbo writes dialogue dashes ("- Pause.") and sound tags
# ("*whistling*", "[music]") that no skill parser expects.
_NON_SPEECH = re.compile(r"\*[^*]*\*|\[[^\]]*\]|\([^)]*\)|^\s*-\s*|(?<=\s)-\s+")


def _whisper_gpu(audio_path, language, probs=True):
    # verbose_json is what costs the extra encoder pass (language detection)
    with open(audio_path, "rb") as f:
        r = requests.post(WHISPER_GPU_URL, files={"file": f}, timeout=10,
                          data={"response_format": "verbose_json" if probs else "json",
                                "temperature": "0.0", "language": language})
    r.raise_for_status()
    d = r.json()
    return " ".join(_NON_SPEECH.sub(" ", d.get("text", "")).split()), d.get("language_probabilities") or {}


def _transcribe_gpu(audio_path, prefer=None):
    """English or Spanish, nothing else. One request transcribes as English
    and returns every language's probability (verbose_json runs Whisper's
    language detection: +1 encoder pass, ~0.59s vs 0.30s). Only a turn picked
    as Spanish is transcribed again, as Spanish (~0.3s more). "auto" instead
    would risk a Turkish transcript of a garbled clip, and "auto" plus the
    probabilities costs a third pass (0.88s)."""
    t0 = time.time()
    if not WHISPER_DETECT_LANGUAGE:
        text, _ = _whisper_gpu(audio_path, "en", probs=False)
        return {"text": text, "language": "en", "confidence": 0.9 if text else 0.0,
                "time_s": round(time.time() - t0, 2)}
    text, probs = _whisper_gpu(audio_path, "en")
    lang = pipeline.pick_language(probs, text, prefer, SPANISH_MIN_PROB, SPANISH_MIN_WORDS)
    if lang == "es":
        text, _ = _whisper_gpu(audio_path, "es", probs=False)
    return {"text": text, "language": lang, "confidence": 0.9 if text else 0.0,
            "time_s": round(time.time() - t0, 2),
            "lang_probs": {k: round(probs.get(k, 0.0), 2) for k in ("en", "es")}}


def transcribe(audio_path, hotwords=None, prefer=None):
    if WHISPER_GPU_URL and not hotwords:
        try:
            return _transcribe_gpu(audio_path, prefer)
        except Exception as e:
            log.warning("GPU STT failed, using CPU: %s", e)
    t0 = time.time()
    model = _get_fwhisper()
    segments, _ = model.transcribe(
        audio_path,
        language=WHISPER_LANG,
        initial_prompt=WHISPER_PROMPT or None,
        hotwords=hotwords,
        beam_size=WHISPER_BEAM,
        # No temperature fallback: on music-soaked audio it retried into
        # 7s hallucinations ("Play it, play it, play it, ...").
        temperature=0.0,
    )
    text = " ".join(s.text for s in segments).strip()
    elapsed = time.time() - t0
    if not text:
        return {"text": "", "confidence": 0.0, "time_s": round(elapsed, 2)}
    return {"text": text, "confidence": 0.9, "time_s": round(elapsed, 2)}


def _get_fwhisper_careful():
    global _fwhisper_careful
    with _fwhisper_lock:
        if _fwhisper_careful is None:
            from faster_whisper import WhisperModel
            _fwhisper_careful = WhisperModel(WHISPER_CAREFUL_MODEL, device="cpu", compute_type="int8")
            log.info("faster-whisper %s ready (second opinion)", WHISPER_CAREFUL_MODEL)
        return _fwhisper_careful


def transcribe_careful(audio_path):
    t0 = time.time()
    segments, _ = _get_fwhisper_careful().transcribe(
        audio_path, language=WHISPER_LANG, beam_size=WHISPER_BEAM, temperature=0.0)
    text = " ".join(s.text for s in segments).strip()
    return {"text": text, "time_s": round(time.time() - t0, 2)}


_TRANSLATE_PROMPT = (
    "Translate this reply from a home voice assistant into natural, casual Latin "
    "American Spanish, as she would say it out loud. Keep every name, title and "
    "number exactly as written. In weather, a bare number is a temperature in "
    "degrees Fahrenheit: \"It's 89\" is \"Hace 89 grados\", never a clock time. "
    "Output only the translation.\n\n{text}")


def translate_to_spanish(text):
    """Spanish for a skill reply spanish.py has no template for (weather,
    calc, sports...). ~0.4s on the 4B model; None on failure, and the reply
    is spoken in English instead."""
    t0 = time.time()
    try:
        r = requests.post(f"{OLLAMA_HOST}/api/generate", timeout=6, json={
            "model": OLLAMA_MODEL, "prompt": _TRANSLATE_PROMPT.format(text=text),
            "stream": False, "think": False, "keep_alive": OLLAMA_INTERACTION_KEEP_ALIVE,
            "options": {"temperature": 0, "num_predict": 120, "num_ctx": OLLAMA_NUM_CTX}})
        r.raise_for_status()
        out = (r.json().get("response") or "").strip().strip('"')
    except Exception as e:
        log.warning("Spanish translation failed (speaking English): %s", e)
        return None
    log.info("Translated [%.2fs]: %r -> %r", time.time() - t0, text, out)
    return out or None


# ── Speaker ID (SpeechBrain ECAPA-TDNN) ──────────────────────────────────────
# Primary only — the backup doesn't have torch/speechbrain installed, and the
# safety property (identify_speaker absent -> always fall through to the
# disambiguation prompt) means that's a declared capability difference, not
# a bug. ~20MB model, runs CPU-only in a thread parallel to transcribe() so
# it costs approximately zero wall-clock time (see antigua_core/pipeline.py).

SPEAKER_MIN_AUDIO_S = 1.5  # below this, identification is unreliable — ask instead
# Set when torch/speechbrain turn out to be missing (as in the primary's venv as of
# 2026-09-24), so that's logged once instead of as a warning on every turn.
_speaker_deps_missing = False
_speaker_model = None
_speaker_model_lock = Lock()


def _get_speaker_model():
    global _speaker_model
    with _speaker_model_lock:
        if _speaker_model is None:
            from speechbrain.inference.speaker import EncoderClassifier
            log.info("Loading speaker ID model (SpeechBrain ECAPA-TDNN)...")
            _speaker_model = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=str(Path(__file__).parent.parent / "models" / "spkrec-ecapa-voxceleb"),
            )
            log.info("Speaker ID model ready")
        return _speaker_model


def _warm_speaker_model():
    global _speaker_deps_missing
    try:
        _get_speaker_model()
    except ImportError as e:
        _speaker_deps_missing = True
        log.warning("Speaker ID disabled until restart — %s (needs torch, torchaudio, speechbrain)", e)
    except Exception:
        log.warning("Speaker ID model failed to preload; the first turn will retry", exc_info=True)


def _wav_duration_s(audio_path: str) -> float:
    try:
        with wave.open(audio_path, "rb") as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return 0.0


def extract_speaker_embedding(audio_path: str) -> list[float] | None:
    """Return a 192-dim embedding, or None if the clip is too short to trust
    or extraction fails. Never raises — a speaker-ID failure must never take
    down the pipeline."""
    if _wav_duration_s(audio_path) < SPEAKER_MIN_AUDIO_S:
        return None
    try:
        import soundfile as sf
        import torch
        data, sr = sf.read(audio_path, dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)  # stereo -> mono
        sig = torch.from_numpy(data).unsqueeze(0)
        model = _get_speaker_model()
        if sr != 16000:
            import torchaudio
            sig = torchaudio.functional.resample(sig, sr, 16000)
        emb = model.encode_batch(sig).squeeze().tolist()
        return emb
    except ImportError as e:
        global _speaker_deps_missing
        _speaker_deps_missing = True
        log.warning("Speaker ID disabled until restart — %s (needs torch, torchaudio, speechbrain)", e)
        return None
    except Exception as e:
        log.warning("Speaker embedding extraction failed: %s", e)
        return None


def identify_speaker(audio_path: str) -> str | None:
    """Backend.identify_speaker — resolve audio to an enrolled person, or
    None. Safety property: below the similarity floor (or too little audio),
    always None, never a guess. See antigua_core/speaker_id.py."""
    if _speaker_deps_missing or not speaker_profiles.enrolled_people():
        return None  # nobody enrolled (server/scripts/enroll_speaker.py): nothing could match
    embedding = extract_speaker_embedding(audio_path)
    if embedding is None:
        return None
    match = speaker_profiles.identify(embedding)
    if match is None:
        return None
    person, score = match
    log.info("Speaker ID: %s (similarity=%.3f)", person, score)
    return person


# ── LLM (Ollama) ────────────────────────────────────────────────────────────


# A hung or reloading model must not hold the turn past the kitchen bridge's
# 60s read timeout: past that the bridge has given up, and a late reply is
# spoken out of nowhere. LLM_READ_TIMEOUT_S bounds the wait for the first
# token (and any stall after it); LLM_TURN_DEADLINE_S bounds the whole stream.
LLM_STREAM_TIMEOUT = (2, 12)
LLM_TURN_DEADLINE_S = 40
_LLM_TROUBLE = "Sorry, I'm having a little trouble right now."

# "7 p.m. tonight" is not a sentence end; "7 p.m. Then" is.
_SENTENCE_END_RE = re.compile(
    r'(?<=[.!?])(?<![AaPp]\.[Mm]\.)["\'’”)]*(?:\s|$)'
    r'|(?<=[AaPp]\.[Mm]\.)(?:\s+(?=[A-Z])|$)'
)


def ask_llm_stream(transcript, conversation_id="", extra_context=None, max_tokens_override=None,
                   temperature_override=None, grounding_context=None, grounding_mode=None,
                   language="en", turn_hint=None):
    """Stream LLM response, yielding complete sentences as they arrive.

    Caller is responsible for synthesizing and publishing each yielded sentence.
    Conversation history is saved after the last sentence is consumed.
    temperature_override drops creativity for grounded answers (search results),
    where invention is a bug, not personality.
    """
    t0 = time.time()

    messages = []
    if conversation_id:
        messages = conversations.get_messages(conversation_id)

    now_str = server_common.llm_now_str()

    weather_injected = False
    if extra_context is not None:
        system_with_context = SYSTEM_PROMT + f"\nCurrent date and time: {now_str}." + f"\n{extra_context}"
    elif wants_weather_context(transcript, messages):
        weather_injected = True
        weather_str = weather_skill.context_for_prompt(weather_provider, transcript)
        system_with_context = (
            SYSTEM_PROMT
            + f"\nCurrent date and time: {now_str}."
            + f"\n{weather_str}"
            + "\nAnswer weather questions only from the data above; if it is not"
            + " there, say you don't have it."
            + f"\n{format_active_timers()}"
        )
    else:
        system_with_context = (
            SYSTEM_PROMT
            + f"\nCurrent date and time: {now_str}."
            + f"\n{format_active_timers()}"
        )
    alerts_str = nws_alerts_cache.format_for_prompt()
    if alerts_str:
        system_with_context += f"\n{alerts_str}"
    # Rules the family approved by hand in data/household_memory.yaml.
    system_with_context += household_memory.store().rules_block()

    if not messages or messages[0]["role"] != "system":
        messages.insert(0, {"role": "system", "content": system_with_context})
    else:
        messages[0]["content"] = system_with_context

    # Per-turn hint rather than only the prompt's LANGUAGE rule: the 4B model
    # follows a line next to the question far more reliably. Not saved to
    # history (add_message stores the bare transcript), and the system prompt
    # is untouched, so Ollama's prefix cache still hits.
    hint = "\n(They spoke Spanish. Reply in Spanish.)" if language == "es" else ""
    if turn_hint:  # persona.py's stage direction — same slot, same reasons
        # One line per hint (household notes and preferences ride here too).
        hint += "".join(f"\n({h})" for h in turn_hint.split("\n") if h)
    if not netstatus.online():  # so "is the internet down?" style chat doesn't make things up
        hint += f"\n({sysinfo.offline_hint()})"
    messages.append({"role": "user", "content": f"{transcript}{hint} /no_think"})

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "options": {
            "num_predict": max_tokens_override if max_tokens_override else OLLAMA_MAX_TOKENS,
            "temperature": OLLAMA_TEMP if temperature_override is None else temperature_override,
            "num_ctx": OLLAMA_NUM_CTX,
        },
        "think": False,
        "keep_alive": OLLAMA_INTERACTION_KEEP_ALIVE,
    }

    buf = ""
    full_text = ""
    spoke_any = False
    dropped_ungrounded = False

    try:
        resp = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=LLM_STREAM_TIMEOUT,
                             stream=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("LLM stream failed to start: %s", e)
        yield _LLM_TROUBLE
        return

    try:
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            chunk = data.get("message", {}).get("content", "")
            buf += chunk
            full_text += chunk
            if time.time() - t0 > LLM_TURN_DEADLINE_S:
                log.warning("LLM stream passed its %ds deadline — cutting it off", LLM_TURN_DEADLINE_S)
                break

            while True:
                m = _SENTENCE_END_RE.search(buf)
                if not m:
                    break
                sentence = buf[: m.end()].strip()
                buf = buf[m.end():]
                if not sentence:
                    continue
                if not weather_claim_allowed(sentence, weather_injected):
                    log.info("Dropped invented weather claim: %r", sentence)
                    continue
                if grounding_context:
                    bad = unsupported_claims(sentence, grounding_context)
                    if (not bad and grounding_mode == "recipe"
                            and not recipe_claims_ok(sentence, grounding_context)):
                        bad = ["number not in the recipe"]
                    if bad:
                        log.info("Dropped ungrounded claim %s: %r", bad, sentence)
                        dropped_ungrounded = True
                        continue
                spoke_any = True
                yield sentence

            if data.get("done"):
                break
    except Exception as e:
        log.warning("LLM stream read error: %s", e)
        if not spoke_any:
            yield _LLM_TROUBLE
            return

    # Whatever is left in the buffer never hit a sentence end, so the model was
    # cut off by num_predict mid-thought. Speaking it means TTS reads out
    # "...packed with narrow streets, but if" — drop it, unless it is all we
    # have, in which case a clipped answer still beats silence.
    tail = buf.strip()
    if tail:
        if not weather_claim_allowed(tail, weather_injected):
            log.info("Dropped invented weather claim: %r", tail)
        elif grounding_context and (
                unsupported_claims(tail, grounding_context)
                or (grounding_mode == "recipe"
                    and not recipe_claims_ok(tail, grounding_context))):
            log.info("Dropped ungrounded tail: %r", tail)
            dropped_ungrounded = True
        elif _SENTENCE_END_RE.search(tail) or not spoke_any:
            spoke_any = True
            yield tail
        else:
            log.info("Dropped truncated trailing fragment: %r", tail)
            full_text = full_text[: full_text.rstrip().rfind(tail)].rstrip() or full_text

    # Everything the model said was invented — say so rather than go silent.
    if dropped_ungrounded and not spoke_any:
        fallback = ("The recipe doesn't say." if grounding_mode == "recipe"
                    else "I couldn't find a clear answer for that.")
        full_text = fallback
        yield fallback

    full_text = full_text.strip()
    if conversation_id and full_text:
        conversations.add_message(conversation_id, "user", transcript)
        conversations.add_message(conversation_id, "assistant", full_text)

    log.info("LLM stream [%.2fs] %d chars", time.time() - t0, len(full_text))



# ── TTS ─────────────────────────────────────────────────────────────────────

def synthesize_remote(text, url=REMOTE_TTS_URL, lang="en"):
    """Synthesize via a Kokoro TTS server (local antigua-tts or the backup)."""
    t0 = time.time()
    fname = f"{uuid.uuid4().hex[:12]}.wav"
    out_path = AUDIO_OUT_DIR / fname
    try:
        payload = {"text": text}
        if lang != "en":
            payload["lang"] = lang
        if TTS_SPEED:
            payload["speed"] = TTS_SPEED
        resp = _tts_session.post(url, json=payload, timeout=(2, 15))
        resp.raise_for_status()
        out_path.write_bytes(resp.content)
        log.info("TTS (%s): %.2fs -> %s", url, time.time() - t0, out_path.name)
        return str(out_path)
    except requests.ConnectionError:
        log.error("TTS unreachable: %s", url)
    except requests.Timeout:
        log.error("TTS timeout: %s", url)
    except requests.HTTPError as exc:
        log.error("TTS HTTP error from %s: %s", url, exc)
    return ""


# Kept this long after its last use (a hit refreshes it), so fixed replies —
# timer confirmations, recipe steps, calc answers — stay instant. Bounded by
# the 7-day retention policy and by size; prune_audio evicts the oldest.
_TTS_CACHE_TTL = 7 * 86400  # seconds
_TTS_CACHE_MAX_BYTES = 200 * 1024 * 1024


def synthesize(text, lang="en"):
    """Synthesize via the primary Kokoro server, falling back to fallback_url.
    Results are cached by content hash so deterministic responses are instant
    after the first synthesis.
    """
    text = clean_for_tts(text, lang)
    key = text if lang == "en" else f"{lang}:{text}"   # en keys unchanged: cache stays warm
    cache_hash = hashlib.sha256(key.encode()).hexdigest()[:16]
    cache_path = AUDIO_OUT_DIR / f"cache_{cache_hash}.wav"

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < _TTS_CACHE_TTL:
            log.info("TTS cache hit: %s (%.0fs since last use)", cache_path.name, age)
            os.utime(cache_path)
            return str(cache_path)

    result = synthesize_remote(text, lang=lang)
    if not result and FALLBACK_TTS_URL:
        log.warning("TTS failed, falling back to %s", FALLBACK_TTS_URL)
        result = synthesize_remote(text, FALLBACK_TTS_URL, lang)

    if result and Path(result).exists():
        import shutil
        shutil.copy(result, cache_path)
        log.info("TTS cached: %s", cache_path.name)
        return str(cache_path)

    return result


# ── Full Pipeline ────────────────────────────────────────────────────────────
# run_pipeline lives in antigua_core.pipeline; this server contributes the
# Backend: whisper.cpp STT, qwen3.5:4b via Ollama, local Kokoro TTS, and the
# Roku/Govee MCP servers (the fallback on the backup runs its own copies).

TOOL_LOG.open(core_settings.DATA_DIR / "tool_calls.db")
mcp_hub = McpHub.from_config(cfg.get("mcp"), core_settings.BASE_DIR)
home = HomeControl(mcp_hub, cfg)

# Music Assistant (Apple Music). The token lives in the same gitignored env
# file as the MCP secrets; without it the music route says it can't play.
_ma_token = read_env_file(core_settings.BASE_DIR / cfg.get("mcp", {}).get(
    "env_file", "server/config/mcp.env")).get("MUSIC_ASSISTANT_TOKEN")
music = MusicControl(cfg, _ma_token) if _ma_token and cfg.get("music", {}).get("enabled", True) else None
if music:
    # The web display's Now Playing card shows this while a play starts up.
    # Under airplaypi/ (not a gated antigua/ display topic): it's the feed
    # PinedaDisplay Web already reads for the kitchen speaker.
    music.progress = lambda p: mqtt_client.publish("airplaypi/loading", json.dumps(p), qos=1)

# Drive times from home: places.yaml (home + favorites), Photon for places,
# TomTom (TOMTOM_API_KEY in the mcp env file) or else OSRM for routes, and
# Houston TranStar for live traffic.
commute = (CommuteProvider(read_env_file(core_settings.BASE_DIR / cfg.get("mcp", {}).get(
    "env_file", "server/config/mcp.env")).get("TOMTOM_API_KEY"))
           if core_settings.COMMUTE_ENABLED else None)


# Who's home (their phones on the Wi-Fi) and reminders for when they get there.
arrivals = ArrivalReminders()


def _on_arrive(person: str) -> None:
    """Their phone just came back: say what was waiting for them, once
    they've had time to get from the driveway to the kitchen."""
    delay = float((cfg.get("presence") or {}).get("arrival_delay_seconds", 60))

    def speak():
        rows = arrivals.take(person)
        if not rows:
            return
        text = presence_mod.arrival_text(person, rows)
        wav_path = synthesize(clean_for_tts(text))
        if wav_path:
            mqtt_publish("antigua/play", {"audio_url": (
                f"http://{get_local_ip()}:{cfg['server']['port']}/audio/{Path(wav_path).name}")})
            log.info("Arrival: told %s %d reminder(s)", person, len(rows))

    if arrivals.pending(person):
        Timer(delay, speak).start()


presence = Presence(cfg, on_arrive=_on_arrive)

# Other rooms' speakers through Music Assistant ("tell the bedroom ...").
announcer = Announcer(cfg, music.ma if music else None) if (cfg.get("announce") or {}).get("rooms") else None
announce_mod.install(announcer)


def remember_exchange(conversation_id, transcript, reply):
    conversations.add_message(conversation_id, "user", transcript)
    conversations.add_message(conversation_id, "assistant", reply)


pipeline.init(pipeline.Backend(
    transcribe=transcribe,
    synthesize=synthesize,
    ask_llm_stream=ask_llm_stream,
    audio_url_base=lambda: f"http://{get_local_ip()}:{cfg['server']['port']}",
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
    write_json=write_json,
    route_music_with_llm=route_music_with_llm,
    identify_speaker=identify_speaker,
    transcribe_careful=transcribe_careful if WHISPER_CAREFUL_MODEL else None,
    translate_to_spanish=translate_to_spanish,
    static_audio_dir=STATIC_AUDIO_DIR,
    home=home,
    music=music,
    commute=commute,
    pineda=pineda,
    security=security,
    knowledge=knowledge,
    recipes=recipes,
    remember_exchange=remember_exchange,
    check_addressed=check_addressed,
    classify_memory=classify_memory,
    last_reply=lambda cid: next((m["content"] for m in reversed(conversations.get_messages(cid))
                                 if m.get("role") == "assistant"), ""),
    set_history_limit=conversations.set_max_history,
    presence=presence if presence.enabled else None,
    arrivals=arrivals,
    announcer=announcer,
))

run_pipeline = pipeline.run_pipeline


# ── Background Cleanup ────────────────────────────────────────────────────


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
        except Exception:
            log.exception("Cleanup error")


# ── HTTP Server ──────────────────────────────────────────────────────────────


class AntiguaHandler(AlarmRoutes, BaseHTTPRequestHandler):
    alarm_timers = timers

    def log_message(self, fmt, *args):
        log.debug("HTTP: %s", fmt % args)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"status": "ok", "model": OLLAMA_MODEL, "mcp": mcp_hub.status()})
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
            self._handle_memories()
        elif parsed.path == "/lists":
            self._handle_lists()
        elif parsed.path == "/tool_calls":
            # Every MCP tool call, newest first, with the turn that made it.
            q = parse_qs(parsed.query)
            self._json(200, {"calls": TOOL_LOG.recent(
                since=float(q.get("since", ["0"])[0]), limit=min(int(q.get("limit", ["50"])[0]), 500),
                conversation=q.get("conversation", [None])[0])})
        elif parsed.path == "/presence":
            self._json(200, {"people": presence.snapshot()})
        elif parsed.path == "/timers":
            # Mirrored by the fallback on the backup, which rings these if this
            # box is down when they're due.
            self._json(200, {"timers": timers.snapshot()})
        elif self.route_alarms("GET"):
            pass
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/stt":
            self._handle_stt()
        elif parsed.path == "/pipeline":
            self._handle_pipeline()
        elif parsed.path == "/pipeline_text":
            self._handle_pipeline_text()
        elif parsed.path == "/handoff":
            self._handle_handoff()
        elif self.route_alarms("POST"):
            pass
        else:
            self._json(404, {"error": "not found"})

    def do_PATCH(self):
        if not self.route_alarms("PATCH"):
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if not self.route_alarms("DELETE"):
            self._json(404, {"error": "not found"})

    def _handle_handoff(self):
        """The fallback, handing back what it owned while this box was down:
        alarms (replace ours), and memories/lists when it changed them."""
        data = self.read_json()
        if data is None:
            return
        if isinstance(data.get("timers"), list):
            timers.take_handoff(data["timers"])
        if isinstance(data.get("memories"), list):
            memory_store.replace(data["memories"])
        if isinstance(data.get("lists"), dict):
            list_store.replace(data["lists"])
        log.warning("Took back from the fallback: %s", ", ".join(
            k for k in ("timers", "memories", "lists") if k in data) or "nothing")
        self._json(200, {"ok": True})

    def _handle_memories(self):
        """Return active (non-expired) memories as JSON for the display."""
        entries = memory_store.active()
        self._json(200, {"memories": entries})

    def _handle_lists(self):
        """Return every named list as JSON — minimum viable surface until a
        phone-viewable page exists (out of scope for Phase 3)."""
        self._json(200, {"lists": list_store.all_lists()})

    def _conv_id(self):
        return self.headers.get("X-Conversation-ID", "")

    def _handle_stt(self):
        audio = server_common.read_body(self, server_common.MAX_AUDIO_BYTES)
        if audio is None:
            return
        tmp = AUDIO_OUT_DIR / f"_in_{uuid.uuid4().hex[:8]}.wav"
        tmp.write_bytes(audio)
        result = transcribe(str(tmp))
        tmp.unlink(missing_ok=True)
        self._json(200, result)

    def _handle_pipeline(self):
        audio = server_common.read_body(self, server_common.MAX_AUDIO_BYTES)
        if audio is None:
            return
        tmp = AUDIO_OUT_DIR / f"_in_{uuid.uuid4().hex[:8]}.wav"
        tmp.write_bytes(audio)

        conv_id = self._conv_id()
        follow_up = self.headers.get("X-Follow-Up", "0") == "1"
        # Optional: stream reply chunks to another antigua/* topic instead of
        # the Pi's antigua/play (the kitchen bridge's reSpeaker output mode).
        play_topic = self.headers.get("X-Play-Topic", "antigua/play")
        if not play_topic.startswith("antigua/"):
            play_topic = "antigua/play"
        result = run_pipeline(str(tmp), conversation_id=conv_id, follow_up=follow_up,
                              play_topic=play_topic)
        tmp.unlink(missing_ok=True)
        self._respond_pipeline(result)

    def _handle_pipeline_text(self):
        """Debug/QA seam: run the full classify + dispatch on a text string,
        skipping STT. Same code path spoken turns take after transcription."""
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
        result = pipeline.dispatch_text(
            text,
            conversation_id=data.get("conversation_id", "") or self._conv_id(),
            follow_up=bool(data.get("follow_up", False)),
            quiet=bool(data.get("quiet", True)),  # default silent for QA
        )
        self._respond_pipeline(result, quiet=bool(data.get("quiet", True)))

    def _respond_pipeline(self, result, quiet=False):
        audio_url = ""
        if result.get("audio_file"):
            audio_url = (
                f"http://{get_local_ip()}:{cfg['server']['port']}"
                f"/audio/{Path(result['audio_file']).name}"
            )

        if not quiet:
            mqtt_publish(
                "antigua/response",
                {"text": result.get("response", ""), "audio_url": audio_url},
            )
            mqtt_publish("antigua/status", {"state": "ready"})

        self._json(
            200,
            {
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
                "chat_mode": result.get("chat_mode", False),
                "recipe_mode": result.get("recipe_mode", False),
            },
        )

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


def _resolve_local_ip():
    """The address the Pi can fetch reply audio from: whichever one this host
    uses to reach the Pi's broker — its wg0 address (network/README.md)."""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((MQTT_BROKER, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "10.77.0.1"


_LOCAL_IP = _resolve_local_ip()


def get_local_ip():
    return _LOCAL_IP


# ── Main ────────────────────────────────────────────────────────────────────


def _preload_weather_tts():
    """Background thread: keep the home forecast (and any prewarm cities) fresh
    and pre-generate TTS for the plain 'what's the weather' answer so the most
    common query is instant. Content-addressable TTS caching does the rest."""
    while True:
        try:
            weather_provider.prewarm(core_settings.WEATHER_PREWARM_CITIES)
            if rates_provider is not None:
                rates_provider.prewarm()
            reply = weather_skill.answer("what's the weather", weather_provider)
            if reply:
                synthesize(reply)
                log.info("Pre-warmed weather TTS: %s", reply)
        except Exception:
            log.debug("weather prewarm failed", exc_info=True)
        time.sleep(core_settings.WEATHER_TTL)


_announced_alerts: set = set()


def _severe_alert_watch():
    """Speak a newly-issued Severe or Extreme NWS alert for home, unprompted.

    The one case where volunteering weather is clearly right. Minor/Moderate
    advisories stay silent; each alert is announced at most once per process.
    """
    while True:
        try:
            if core_settings.WEATHER_SEVERE_ALERTS:
                for a in nws_alerts_cache.get() or []:
                    if a.get("severity") not in ("Severe", "Extreme"):
                        continue
                    key = (a.get("event", ""), a.get("headline", ""))
                    if key in _announced_alerts:
                        continue
                    _announced_alerts.add(key)
                    msg = f"Heads up — {a.get('headline') or a.get('event')}."
                    wav = synthesize(clean_for_tts(msg))
                    if wav:
                        url = (f"http://{get_local_ip()}:{cfg['server']['port']}"
                               f"/audio/{Path(wav).name}")
                        mqtt_publish("antigua/play", {"audio_url": url})
                        log.warning("Announced severe alert: %s", a.get("event"))
        except Exception:
            log.debug("severe alert watch failed", exc_info=True)
        time.sleep(core_settings.NWS_ALERTS_TTL)


def main():
    log.info("Antigua Server starting...")
    mqtt_connect()
    Thread(target=cleanup_loop, daemon=True).start()

    # Restore persisted timers so they survive restarts
    timers._load()
    if timer_mirror:
        timer_mirror.push()   # the reloaded timers, or clear a list left from before a crash
        timer_mirror.keep_fresh()

    # Pre-warm caches so first request is instant
    mcp_hub.warm()
    home.warm()
    Thread(target=_get_fwhisper, daemon=True).start()   # first request pays ~3s otherwise
    if speaker_profiles.enrolled_people():
        # The first turn after a restart paid ~1.1s for this, mid-turn.
        Thread(target=_warm_speaker_model, daemon=True).start()
    if WHISPER_CAREFUL_MODEL:
        Thread(target=_get_fwhisper_careful, daemon=True).start()
    if music is not None:
        music.refresh_names()   # names STT may mishear, for re-hearing
    Thread(target=nws_alerts_cache.get, daemon=True).start()
    for _src in [s["name"] for s in core_settings.NEWS_SOURCES]:
        Thread(target=news_cache.get, args=(_src,), daemon=True).start()

    # Pre-warm weather TTS so simple weather queries are instant
    Thread(target=_preload_weather_tts, daemon=True).start()

    # Speak newly-issued severe weather alerts for home, unprompted
    Thread(target=_severe_alert_watch, daemon=True).start()

    # Is the internet up, and if not why (antigua_core/netstatus.py). Retained
    # on antigua/net so the display and the satellite can show it.
    netstatus.monitor = netstatus.NetStatus()
    netstatus.monitor.listeners.append(_on_net_change)
    netstatus.monitor.start()

    presence.start()
    # The Ask Antigua shortcut on the household's phones (LAN port + key).
    phone_http.start(cfg, lambda text, conv, person: pipeline.dispatch_text(
        text, conversation_id=conv, follow_up=True, quiet=True, speaker=person,
        source="phone"), presence if presence.enabled else None)

    host = cfg["server"]["host"]
    port = cfg["server"]["port"]
    server = ThreadingHTTPServer((host, port), AntiguaHandler)
    log.info("HTTP on %s:%d", host, port)
    log.info("Whisper: %s (CPU fallback: faster-whisper %s)%s",
             WHISPER_GPU_URL or "no GPU server", WHISPER_FW_MODEL,
             "" if WHISPER_DETECT_LANGUAGE else ", language detection off")
    log.info("Ollama:  %s", OLLAMA_MODEL)
    log.info("TTS:     %s (fallback: %s)", REMOTE_TTS_URL, FALLBACK_TTS_URL or "none")
    log.info(
        "Conversation memory: %ds TTL, %d max msgs",
        CONVERSATION_TTL,
        MAX_HISTORY,
    )
    log.info("PinedaDisplay: %s", "enabled" if core_settings.DISPLAY_ENABLED else "disabled")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        server.server_close()


if __name__ == "__main__":
    main()
