"""Kitchen-mic bridge — runs on the primary.

Connects to the reSpeaker Lite (kitchen-mic.local) as an aioesphomeapi client,
using continuous_external_trigger: calls start_va once to bypass the device's
on-device micro_wake_word entirely, then runs Antigua's own openWakeWord
"alexa" model against the continuous PCM stream — same wake word as every
other mic in the house (Decision A, confirmed 2026-09-21). On detection, it
records the follow-up utterance using Silero VAD for endpointing (mirroring
antigua_satellite.py's WakeWordDetector + VoiceRecorder on the Pi) and POSTs
the WAV to antigua_server.py's existing /pipeline endpoint on localhost —
no changes to antigua_server.py itself, and the bridge runs in its own venv
(kitchen-mic/bridge/venv/) so it can't affect the production venv/process.

Response playback needs no code here: antigua_core/pipeline.py already
publishes every real response to MQTT topic "antigua/play", and
antigua_satellite.py (audio-output-only since 2026-09-21) already subscribes
and plays it through the Pi speakers — confirmed working end to end
2026-09-21. A connection watchdog (below) guards against the aioesphomeapi
audio stream silently going stale, mirroring the Pi's old MicStream watchdog.

Follow-up/multi-turn mode: after a response, a short window opens where
plain speech (no wake word needed) or the wake word continues the same
conversation_id — mirrors antigua_satellite.py's pre-retirement follow-up
logic. The tricky part is that this bridge doesn't play audio itself (the
Pi does, over MQTT antigua/play), so it has no direct signal for when
playback actually finishes. It subscribes to MQTT antigua/done — republished
by antigua_satellite.py's play-queue worker once a response's audio has
fully finished playing — and only arms the follow-up window then, so it
isn't listening for a follow-up while Antigua is still mid-sentence.

Failover (2026-09-24): if server_url refuses the connection (antigua-server
down, host up), the turn goes to fallback_server_url (the backup) instead. A
second copy of this bridge runs on the backup with standby_for_host set: it
leaves the reSpeaker alone while that host answers, takes the device over
once it stops answering, and hands it back when it returns. ESPHome lets
only one client own the voice stream, so the two copies never both listen;
the no-audio watchdog retries until the other side has let go.
"""

import asyncio
import json
import logging
import socket
import time
import uuid
import wave
from collections import deque
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
import requests
import yaml
from aioesphomeapi import APIClient
from aioesphomeapi.model import (
    MediaPlayerCommand,
    MediaPlayerState,
    VoiceAssistantEventType,
)
from openwakeword.model import Model as WakeWordModel
from openwakeword import VAD

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kitchen-bridge")

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.yaml"
CAPTURES_DIR = HERE / "captures"
CAPTURES_DIR.mkdir(exist_ok=True)

# ── Audio / detection constants — mirrors the retired mic code in antigua_satellite.py
# (same wake word, same thresholds) so kitchen behavior matches every other
# mic in the house (docs/decisions.md, 2026-09-21).
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
WAKE_WORD = "alexa"
# 0.75 (2026-09-28): 95 kitchen turns since 09-24 — every real command scored
# >= 0.75, while 8 of 37 false wakes (TV, talk nearby) scored 0.62-0.70.
WAKE_WORD_THRESHOLD = 0.75
WAKE_WORD_CONFIRM_FRAMES = 3
ENERGY_GATE_RMS = 150
DEBOUNCE_S = 3.0
OWW_CHUNK_BYTES = 1280 * SAMPLE_WIDTH  # 80ms, matches openWakeWord's expected frame size
# openWakeWord keeps a rolling window of audio features, and the bridge stops
# feeding it while recording/muted (and skips frames under the energy gate).
# Left alone, the window still holds the last "Alexa", so the next loud sound
# of any kind scored 1.0 and re-woke Antigua — the "Oh"/"Yep" turns answered
# with "didn't catch that" (2026-09-23). Flushing silence through it clears it.
OWW_FLUSH = np.zeros(SAMPLE_RATE * 2, dtype=np.int16)

VAD_CHUNK_SAMPLES = 480  # 30ms
VAD_CHUNK_BYTES = VAD_CHUNK_SAMPLES * SAMPLE_WIDTH
VAD_SILENCE_LIMIT_MS = 400
VAD_SILENCE_THRESHOLD = 0.3
# What counts as the command having started (2026-09-25): a single 30ms blip
# ~0.2s after the wake word (the wake cue or the tail of "Alexa") used to
# count, so the user's natural pause after "Alexa" read as end of speech and
# the recording ended at 0.7s with only "Bye!" in it. Now speech must last
# SPEECH_START_CHUNKS frames in a row, and nothing in the first
# WAKE_TAIL_IGNORE_S of a wake-word turn counts.
SPEECH_START_CHUNKS = 3
WAKE_TAIL_IGNORE_S = 0.3
MAX_RECORDING_S = 15
MIN_UTTERANCE_S = 0.3
NO_SPEECH_TIMEOUT_S = 3.0  # give up if no speech at all follows the wake word/preroll
# VAD-miss fallback (2026-09-22): a wake-word turn whose VAD never crossed the
# speech threshold used to be dropped silently — "Alexa, what time is it?"
# got no reply at all. If the window had real energy, send it to STT anyway;
# an empty transcript gets the server's "didn't catch that" reply instead of
# silence. Follow-up turns still drop silently (TV-loop guard).
FALLBACK_LOUD_RMS = ENERGY_GATE_RMS * 2
FALLBACK_MIN_LOUD_S = 0.6  # after the preroll; the ~0.6s wake cue decays well under this at the mic

WATCHDOG_CHECK_S = 5     # how often to check for a stalled stream
WATCHDOG_STALL_S = 30    # no audio at all for this long -> force reconnect

SERVER_URL = "http://localhost:9393/pipeline"

# Standby mode (config standby_for_host): probe that host's sshd, not
# antigua-server — a host that is up keeps its own bridge, which fails over
# per turn via fallback_server_url, so standby should only take the device
# when the whole host is gone.
STANDBY_PROBE_PORT = 22
STANDBY_POLL_S = 5
STANDBY_DOWN_AFTER = 3   # consecutive misses before taking the device
STANDBY_UP_AFTER = 2     # consecutive hits before handing it back

# Follow-up/multi-turn — mirrors antigua_satellite.py's pre-retirement constants
FOLLOW_UP_MODE = True
FOLLOW_UP_TIMEOUT_S = 8          # how long the follow-up window stays open
FOLLOW_UP_ARM_DELAY_MS = 400     # grace period after antigua/done before listening
FOLLOWUP_VAD_TRIGGER_THRESHOLD = 0.5  # speech-prob bar to open a follow-up turn
# Plain speech (no wake word) opening a follow-up turn — off since 2026-09-24.
# On the night of 2026-09-23 nearly every nonsense reply came from it: side
# conversations, a video and Antigua's own trailing audio got transcribed and
# answered. The window still accepts the wake word to continue the conversation.
FOLLOW_UP_SPEECH_TRIGGER = False
SLEEP_WORDS = frozenset(["thank you", "thanks", "stop", "stop listening", "goodbye", "that's all"])
MAX_PLAYING_MUTE_S = 20  # safety ceiling in case antigua/done is ever lost
# TV-voice-hijack guard (2026-09-21): a false wake near the soundbar used to
# loop indefinitely — every response re-armed an 8s follow-up window that the
# TV's dialogue kept filling. Capping consecutive follow-up turns ends any
# such loop after at most MAX_FOLLOWUP_TURNS responses; conversation resumes
# normally with an explicit wake word.
MAX_FOLLOWUP_TURNS = 2

# LED + user button (2026-09-23). led_state values must match the led_state
# action in esphome/kitchen-mic.yaml; the device runs the animations, the
# bridge only says which state and, while recording, how loud the voice is.
LED_IDLE, LED_LISTENING, LED_THINKING, LED_SPEAKING, LED_FOLLOWUP, LED_ERROR, LED_DND = range(7)
# Long press toggles do-not-disturb. Off until the reSpeaker is reflashed with
# the GPIO3 pull-up (esphome/kitchen-mic.yaml, 2026-09-24): the floating pin
# fired 38 phantom long presses overnight 09-23 and left DND stuck on 06:05-09:27,
# silently ignoring every "Alexa" that morning. Flip back to True after the reflash.
BUTTON_DND_ENABLED = False
LED_LEVEL_INTERVAL_S = 0.08  # one level update per openWakeWord-sized frame
LED_LEVEL_FLOOR_DB = -50.0   # dBFS mapped to level 0; floor + span maps to 1
LED_LEVEL_SPAN_DB = 40.0

# reply_output: device — the server streams this mic's reply chunks here
# (X-Play-Topic) instead of antigua/play, and the bridge plays them on the
# reSpeaker so its AEC can cancel them.
DEVICE_REPLY_TOPIC = "antigua/kitchen_play"
DEVICE_PLAY_START_TIMEOUT_S = 5
DEVICE_PLAY_MAX_S = 120

STATE_LISTENING = "listening"
STATE_RECORDING = "recording"
STATE_FOLLOWUP = "followup"


class KitchenBridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client = APIClient(
            address=cfg["device"]["host"],
            port=cfg["device"].get("port", 6053),
            password=None,
            noise_psk=cfg["device"].get("noise_psk"),
        )
        self.oww = WakeWordModel(vad_threshold=0.0)
        self.vad = VAD()

        self.state = STATE_LISTENING
        self._oww_buf = bytearray()
        self._oww_preroll = deque(maxlen=3)  # last few raw chunks, prepended on trigger
        self._confirm_count = 0
        self._last_activation = 0.0

        self._vad_buf = bytearray()
        self._utterance_frames = bytearray()
        self._silent_chunks = 0
        self._max_silent_chunks = VAD_SILENCE_LIMIT_MS // 30
        self._has_speech = False
        self._recording_start = 0.0
        self._follow_up_turn = False
        self._max_speech_prob = 0.0
        self._preroll_bytes = 0

        self._last_audio_t = 0.0

        # Wake cue: played on the Pi's speakers the moment the wake word fires,
        # since in external-trigger mode the device itself never knows it woke.
        self.wake_cue_url = cfg.get("wake_cue_url", "")
        self.server_url = cfg.get("server_url", SERVER_URL)
        self.fallback_server_url = cfg.get("fallback_server_url", "")
        self.standby_for_host = cfg.get("standby_for_host", "")

        # Follow-up window state
        self.conversation_id = ""
        self._followup_vad_buf = bytearray()
        self._followup_armed_at = 0.0
        self._followup_deadline = 0.0
        self._followup_turns = 0  # consecutive VAD-opened follow-ups, capped (see MAX_FOLLOWUP_TURNS)

        # Mute wake-word/VAD processing while Antigua's own response is
        # playing on the Pi — mirrors antigua_satellite.py's old `_is_playing`
        # gate. The reSpeaker's on-device AEC only cancels its own speaker
        # output; it has no reference signal for audio coming out of the Pi's
        # separate speakers, so without this the mic hears its own answer and
        # re-triggers the wake word mid-playback.
        self._playing = False
        self._playing_since = 0.0
        # The mute starts when a turn is sent, not when its reply comes back:
        # streamed LLM replies start playing on the Pi while the request is
        # still open, and an unmuted mic heard them, woke on them and answered
        # itself — on 2026-09-23 that turned every light in the house on.
        # antigua/done seen while a turn is in flight belongs to an earlier
        # reply (or the middle of this one) and must not unmute.
        self._awaiting_reply = False

        self._services = {}      # device API actions by name (start_va, led_state, ...)
        self._last_level_t = 0.0
        self._dnd = False        # long press: ignore the wake word until pressed again
        # Without the USR->D2 jumper GPIO3 reads stuck-pressed and fires a
        # phantom long_press at boot, so gestures only count once the button
        # has been seen released.
        self._button_wired = False
        self._button_key = None
        self._button_event_key = None
        self._vnr_key = None
        self._vnr = None         # latest XMOS voice-to-noise estimate (0-100)

        self._reply_on_device = cfg.get("reply_output", "pi") == "device"
        self._media_player_key = None
        self._device_queue: asyncio.Queue = asyncio.Queue()
        self._mp_started = asyncio.Event()
        self._mp_idle = asyncio.Event()
        self._player_task = None

        self._loop = None
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="kitchen-bridge")
        self.mqtt.on_message = self._on_mqtt_message
        # Subscribe on every (re)connect: the session is clean, so a broker
        # restart (e.g. the Pi rebooting) drops subscriptions, and paho's
        # auto-reconnect won't restore them — antigua/done would go silent.
        self.mqtt.on_connect = self._on_mqtt_connect

    def _on_mqtt_connect(self, client, userdata, flags, rc, props):
        client.subscribe("antigua/done")
        if self._reply_on_device:
            client.subscribe(DEVICE_REPLY_TOPIC)

    def _connect_mqtt(self):
        mqtt_cfg = self.cfg.get("mqtt", {})
        host = mqtt_cfg.get("broker", "localhost")
        port = mqtt_cfg.get("port", 1883)
        self.mqtt.connect(host, port, keepalive=60)
        self.mqtt.loop_start()
        log.info(f"MQTT connected to {host}:{port}, watching antigua/done")

    def _on_mqtt_message(self, client, userdata, msg):
        if msg.topic == "antigua/done":
            if not self._reply_on_device:
                self._loop.call_soon_threadsafe(self._on_playback_done)
        elif msg.topic == DEVICE_REPLY_TOPIC:
            url = json.loads(msg.payload).get("audio_url", "")
            if url:
                self._loop.call_soon_threadsafe(self._device_queue.put_nowait, url)

    def _on_playback_done(self):
        if self._awaiting_reply:
            return
        self._playing = False
        if FOLLOW_UP_MODE and self.state != STATE_RECORDING:
            self._arm_followup()

    def _arm_followup(self):
        if not self.conversation_id:
            self._led_idle()
            return  # nothing to follow up on (e.g. woken but no response sent yet)
        if self._followup_turns >= MAX_FOLLOWUP_TURNS:
            log.info(f"=== FOLLOW-UP CAP ({MAX_FOLLOWUP_TURNS}) REACHED — WAKE WORD REQUIRED TO CONTINUE ===")
            self._end_conversation()
            return
        now = time.time()
        self.state = STATE_FOLLOWUP
        self._followup_vad_buf.clear()
        self._oww_buf.clear()
        self._oww_preroll.clear()
        self._confirm_count = 0
        self._followup_armed_at = now + FOLLOW_UP_ARM_DELAY_MS / 1000
        self._followup_deadline = now + FOLLOW_UP_ARM_DELAY_MS / 1000 + FOLLOW_UP_TIMEOUT_S
        log.info(f"=== FOLLOW-UP WINDOW OPEN ({FOLLOW_UP_TIMEOUT_S}s, speech or wake word) ===")
        self._led(LED_FOLLOWUP)

    def _end_conversation(self):
        # Deliberately does NOT touch self._playing — a sleep word or a
        # garbage-triggered end can still have a response playing out loud
        # (e.g. "No problem"), and un-muting early would let the mic hear
        # that and misfire a new wake word mid-sentence. Only antigua/done
        # (or the safety timeout) clears the mute.
        self.state = STATE_LISTENING
        self.conversation_id = ""
        self._followup_turns = 0
        self._oww_buf.clear()
        self._oww_preroll.clear()
        self._confirm_count = 0
        self._followup_vad_buf.clear()
        if self._playing:
            self._led(LED_SPEAKING)
        else:
            self._led_idle()

    # ── LED / button ────────────────────────────────────────────────────────

    def _led(self, state: int):
        if svc := self._services.get("led_state"):
            asyncio.create_task(self._call_action(svc, {"state": state}))

    def _led_idle(self):
        self._led(LED_DND if self._dnd else LED_IDLE)
        # Music stays ducked for the whole exchange (reply + follow-up
        # window), not just while recording; every path back to idle lands
        # here, so this is where it's restored.
        self._publish_listening(False)

    def _led_voice_level(self, data: bytes):
        now = time.time()
        svc = self._services.get("led_level")
        if not svc or now - self._last_level_t < LED_LEVEL_INTERVAL_S:
            return
        self._last_level_t = now
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        db = 20 * np.log10(np.sqrt(np.mean(samples ** 2)) / 32768 + 1e-9)
        level = float(np.clip((db - LED_LEVEL_FLOOR_DB) / LED_LEVEL_SPAN_DB, 0.0, 1.0))
        asyncio.create_task(self._call_action(svc, {"level": level}))

    async def _call_action(self, svc, data: dict):
        try:
            await self.client.execute_service(svc, data)
        except Exception as e:
            log.debug(f"{svc.name} failed: {e}")

    # ── reSpeaker reply output (reply_output: device) ───────────────────────

    async def _device_player(self):
        """Play queued reply chunks on the reSpeaker one at a time; when the
        queue drains, that's the equivalent of the Pi's antigua/done."""
        while True:
            url = await self._device_queue.get()
            self._mp_started.clear()
            self._mp_idle.clear()
            self.client.media_player_command(self._media_player_key, media_url=url, announcement=True)
            try:
                await asyncio.wait_for(self._mp_started.wait(), DEVICE_PLAY_START_TIMEOUT_S)
                await asyncio.wait_for(self._mp_idle.wait(), DEVICE_PLAY_MAX_S)
            except TimeoutError:
                log.warning(f"reSpeaker playback didn't start/finish: {url}")
            if self._device_queue.empty():
                self._on_playback_done()

    def _stop_device_playback(self):
        while not self._device_queue.empty():
            self._device_queue.get_nowait()
        self.client.media_player_command(self._media_player_key, command=MediaPlayerCommand.STOP)
        self._playing = False

    def _play_reply(self, url: str):
        if self._reply_on_device:
            self._device_queue.put_nowait(url)
        else:
            self.mqtt.publish("antigua/play", json.dumps({"audio_url": url}), qos=1)

    def _on_device_state(self, state):
        if state.key == self._media_player_key:
            if state.state in (MediaPlayerState.ANNOUNCING, MediaPlayerState.PLAYING):
                self._mp_started.set()
            elif state.state == MediaPlayerState.IDLE:
                self._mp_idle.set()
        elif state.key == self._vnr_key:
            self._vnr = state.state
        elif state.key == self._button_key:
            if not state.state and not self._button_wired:
                self._button_wired = True
                log.info("User button released — button gestures enabled")
        elif state.key == self._button_event_key and self._button_wired:
            self._on_button(state.event_type)

    def _on_button(self, gesture: str):
        log.info(f"Button: {gesture}")
        if gesture == "single_press":
            # Push-to-talk: same as hearing the wake word, even in DND.
            if self.state != STATE_RECORDING and not self._playing:
                self._start_recording(follow_up_turn=False)
        elif gesture == "long_press":
            if not BUTTON_DND_ENABLED:
                log.info("Long press ignored — button DND disabled (see BUTTON_DND_ENABLED)")
                return
            self._dnd = not self._dnd
            if not self._dnd:
                self._reset_wake_word()
            log.info(f"Do-not-disturb {'ON — wake word ignored' if self._dnd else 'OFF'}")
            if self.state != STATE_RECORDING:
                self._end_conversation()

    async def connect(self):
        await self.client.connect(login=True)
        info = await self.client.device_info()
        log.info(f"Connected to {info.name} (esphome {info.esphome_version})")

    async def handle_start(self, conversation_id, flags, audio_settings, wake_word_phrase):
        self.client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START, {})
        return 0

    async def handle_audio(self, data: bytes, data2=None):
        self._last_audio_t = time.time()
        # With replies on the reSpeaker its AEC cancels them, so keep listening
        # (the wake word interrupts); on the Pi's speakers the mic must be muted.
        if self._playing and not self._reply_on_device and self.state != STATE_RECORDING:
            if time.time() - self._playing_since > MAX_PLAYING_MUTE_S:
                log.warning("Playing-mute safety timeout — no antigua/done seen, unmuting")
                self._playing = False
                if self.state == STATE_LISTENING:
                    self._led_idle()
            else:
                return  # muted: avoid the mic re-triggering on Antigua's own voice
        if self.state == STATE_LISTENING:
            self._feed_wake_word(data)  # still runs in DND, so ignored wakes get logged
        elif self.state == STATE_RECORDING:
            self._feed_recording(data)
        else:
            self._feed_followup(data)

    async def handle_stop(self, abort: bool):
        self.client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END, {})
        # continuous_external_trigger: the device shouldn't stop on its own, but
        # if it ever does (reconnect, internal timeout), immediately re-arm so
        # the stream never silently goes dead.
        log.warning(f"Device-side pipeline stop (abort={abort}) — re-arming start_va")
        await self._start_va()

    def _feed_wake_word(self, data: bytes):
        self._oww_buf.extend(data)
        while len(self._oww_buf) >= OWW_CHUNK_BYTES:
            chunk = bytes(self._oww_buf[:OWW_CHUNK_BYTES])
            del self._oww_buf[:OWW_CHUNK_BYTES]
            self._oww_preroll.append(chunk)

            samples = np.frombuffer(chunk, dtype=np.int16)
            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
            if rms < ENERGY_GATE_RMS:
                self._confirm_count = 0
                continue

            try:
                prediction = self.oww.predict(samples)
            except Exception as e:
                log.warning(f"openWakeWord predict error: {e}")
                continue

            score = prediction.get(WAKE_WORD, 0.0)

            if score >= WAKE_WORD_THRESHOLD:
                self._confirm_count += 1
            else:
                self._confirm_count = 0

            if self._confirm_count >= WAKE_WORD_CONFIRM_FRAMES:
                now = time.time()
                if now - self._last_activation < DEBOUNCE_S:
                    self._confirm_count = 0
                    continue
                self._last_activation = now
                if self._dnd:
                    log.info(f"Wake word '{WAKE_WORD}' heard (score={score:.2f}) but ignored — do-not-disturb is ON")
                    self._reset_wake_word()
                    return
                was_followup = self.state == STATE_FOLLOWUP
                vnr = f", vnr={self._vnr:.0f}" if self._vnr is not None else ""
                log.info(f"Wake word '{WAKE_WORD}' detected (score={score:.2f}{vnr})"
                         + (" [continuation]" if was_followup else ""))
                self._start_recording(follow_up_turn=False)
                return  # remaining bytes in _oww_buf get reprocessed next call

    def _feed_followup(self, data: bytes):
        now = time.time()
        if now >= self._followup_deadline:
            log.info("=== FOLLOW-UP WINDOW TIMED OUT — ENDING CONVERSATION ===")
            self._end_conversation()
            return
        if now < self._followup_armed_at:
            return  # grace period right after antigua/done, ignore playback tail

        # Wake word still works as an explicit continuation trigger.
        self._feed_wake_word(data)
        if self.state != STATE_FOLLOWUP or not FOLLOW_UP_SPEECH_TRIGGER:
            return  # wake word fired _start_recording already, or speech can't open a turn

        self._followup_vad_buf.extend(data)
        while len(self._followup_vad_buf) >= VAD_CHUNK_BYTES:
            chunk = bytes(self._followup_vad_buf[:VAD_CHUNK_BYTES])
            del self._followup_vad_buf[:VAD_CHUNK_BYTES]
            samples = np.frombuffer(chunk, dtype=np.int16)
            speech_prob = float(self.vad.predict(samples, frame_size=VAD_CHUNK_SAMPLES))
            if speech_prob > FOLLOWUP_VAD_TRIGGER_THRESHOLD:
                log.info(f"=== FOLLOW-UP SPEECH DETECTED (p={speech_prob:.2f}) ===")
                self._start_recording(follow_up_turn=True)
                return

    def _reset_wake_word(self):
        self.oww.predict(OWW_FLUSH)
        self.oww.reset()
        self._confirm_count = 0

    def _publish_listening(self, active: bool):
        # antigua-server ducks any playing music while this is true, so the
        # user doesn't have to talk over it, and restores it when false.
        # True at each recording start; false only once the bridge is idle.
        self.mqtt.publish("antigua/listening", json.dumps({"active": active}), qos=1)

    def _start_recording(self, follow_up_turn: bool):
        self.state = STATE_RECORDING
        self._publish_listening(True)
        self._reset_wake_word()
        self._follow_up_turn = follow_up_turn
        if follow_up_turn:
            self._followup_turns += 1
        else:
            self._followup_turns = 0  # explicit wake word resets the cap
        self._confirm_count = 0
        self._utterance_frames = bytearray()
        for chunk in self._oww_preroll:
            self._utterance_frames.extend(chunk)  # ~240ms preroll so the command isn't clipped
        self._preroll_bytes = len(self._utterance_frames)
        self._oww_preroll.clear()
        self._vad_buf = bytearray()
        self._silent_chunks = 0
        self._speech_run = 0
        self._has_speech = False
        self._max_speech_prob = 0.0
        self._recording_start = time.time()
        self.vad.reset_states()
        self._led(LED_LISTENING)
        if self._reply_on_device and self._playing:
            log.info("Interrupting reply on the reSpeaker")
            self._stop_device_playback()
        if not follow_up_turn and self.wake_cue_url:
            if self._reply_on_device:
                self.client.media_player_command(
                    self._media_player_key, media_url=self.wake_cue_url, announcement=True)
            else:
                self.mqtt.publish("antigua/cue", json.dumps({"audio_url": self.wake_cue_url}), qos=1)

    def _feed_recording(self, data: bytes):
        self._utterance_frames.extend(data)
        self._led_voice_level(data)
        self._vad_buf.extend(data)

        elapsed = time.time() - self._recording_start
        if elapsed >= MAX_RECORDING_S:
            # Nobody talks to Antigua for 15s straight: every capture that hit
            # the cap (6 of 6, 09-24..09-27) was TV or conversation after a
            # false wake. Drop it unsent and unsaved; real commands ran <= 7s.
            log.info(f"Max recording ({MAX_RECORDING_S}s) reached — overheard speech, discarding")
            self._abort_recording()
            return

        while len(self._vad_buf) >= VAD_CHUNK_BYTES:
            vad_chunk = bytes(self._vad_buf[:VAD_CHUNK_BYTES])
            del self._vad_buf[:VAD_CHUNK_BYTES]
            samples = np.frombuffer(vad_chunk, dtype=np.int16)
            speech_prob = float(self.vad.predict(samples, frame_size=VAD_CHUNK_SAMPLES))
            self._max_speech_prob = max(self._max_speech_prob, speech_prob)
            elapsed = time.time() - self._recording_start
            in_wake_tail = not self._follow_up_turn and elapsed < WAKE_TAIL_IGNORE_S
            if speech_prob > VAD_SILENCE_THRESHOLD and not in_wake_tail:
                self._speech_run += 1
                if self._speech_run >= SPEECH_START_CHUNKS:
                    self._has_speech = True
                self._silent_chunks = 0
            else:
                self._speech_run = 0
                if self._has_speech:
                    self._silent_chunks += 1

            # Silence only ends the recording once real speech has actually
            # been seen — a bare wake word is often followed by a brief
            # natural pause before the command, which used to get read as
            # "done talking" and clipped the whole command.
            if self._has_speech and self._silent_chunks >= self._max_silent_chunks:
                log.info(f"Silence after {elapsed:.1f}s")
                self._finish_recording()
                return
            elif not self._has_speech and elapsed >= NO_SPEECH_TIMEOUT_S:
                loud_s = self._loud_seconds(self._utterance_frames[self._preroll_bytes:])
                missed = CAPTURES_DIR / f"missed_{int(time.time())}.wav"
                self._write_wav(missed, bytes(self._utterance_frames))
                log.info(f"No speech within {NO_SPEECH_TIMEOUT_S:.0f}s "
                         f"(max vad={self._max_speech_prob:.2f}, loud={loud_s:.1f}s) -> {missed}")
                if not self._follow_up_turn and loud_s >= FALLBACK_MIN_LOUD_S:
                    log.info("VAD missed but audio was loud — sending to STT anyway")
                    self._finish_recording()
                else:
                    self._abort_recording()
                return

    @staticmethod
    def _loud_seconds(raw: bytes) -> float:
        samples = np.frombuffer(bytes(raw), dtype=np.int16)
        n = len(samples) // VAD_CHUNK_SAMPLES
        if not n:
            return 0.0
        frames = samples[: n * VAD_CHUNK_SAMPLES].reshape(n, VAD_CHUNK_SAMPLES).astype(np.float32)
        rms = np.sqrt(np.mean(frames ** 2, axis=1))
        return int(np.sum(rms >= FALLBACK_LOUD_RMS)) * VAD_CHUNK_SAMPLES / SAMPLE_RATE

    @staticmethod
    def _write_wav(path: Path, raw: bytes):
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(raw)

    def _abort_recording(self):
        """No speech ever detected in this recording window — discard without
        POSTing to the server (mirrors antigua_satellite.py's old behavior)."""
        follow_up_turn = self._follow_up_turn
        self.state = STATE_LISTENING
        self._oww_buf.clear()
        self._utterance_frames = bytearray()
        if follow_up_turn:
            self._end_conversation()
        else:
            self._led_idle()

    def _finish_recording(self):
        follow_up_turn = self._follow_up_turn
        self.state = STATE_LISTENING
        self._oww_buf.clear()
        raw = bytes(self._utterance_frames)
        self._utterance_frames = bytearray()

        duration_s = len(raw) / (SAMPLE_RATE * SAMPLE_WIDTH)
        if duration_s < MIN_UTTERANCE_S:
            log.info(f"Utterance too short ({duration_s:.2f}s) — discarding")
            if follow_up_turn:
                self._end_conversation()
            else:
                self._led_idle()
            return

        out_path = CAPTURES_DIR / f"utterance_{int(time.time())}.wav"
        self._write_wav(out_path, raw)
        log.info(f"Recorded {duration_s:.1f}s -> {out_path}")
        self._led(LED_THINKING)
        if not self._reply_on_device:
            self._awaiting_reply = True
            self._playing = True
            self._playing_since = time.time()

        asyncio.create_task(self._send_to_server(out_path, follow_up_turn))

    async def _send_to_server(self, wav_path: Path, follow_up_turn: bool = False):
        if not self.conversation_id:
            self.conversation_id = uuid.uuid4().hex
        conv_id = self.conversation_id
        headers = {
            "Content-Type": "audio/wav",
            "X-Conversation-ID": conv_id,
            "X-Follow-Up": "1" if follow_up_turn else "0",
        }
        if self._reply_on_device:
            headers["X-Play-Topic"] = DEVICE_REPLY_TOPIC
        try:
            audio = wav_path.read_bytes()
            try:
                resp = await asyncio.to_thread(
                    requests.post, self.server_url, data=audio, headers=headers, timeout=60,
                )
            except requests.ConnectionError:
                # Refused/unreachable only — a read timeout may already have
                # been answered, and replaying it would speak twice.
                if not self.fallback_server_url:
                    raise
                log.warning(f"{self.server_url} unreachable — sending turn to fallback")
                resp = await asyncio.to_thread(
                    requests.post, self.fallback_server_url, data=audio, headers=headers, timeout=60,
                )
            resp.raise_for_status()
            result = resp.json()
            transcript = result.get("transcript", "")
            response_text = result.get("response", "")
            log.info(f"Transcript: {transcript!r}")
            log.info(f"Response:   {response_text!r}")
            audio_url = result.get("audio_url", "")

            self._awaiting_reply = False
            if response_text and (audio_url or result.get("streaming")):
                # Mute wake-word/VAD until antigua/done confirms the Pi has
                # finished playing this — see _is_playing note in __init__.
                self._playing = True
                self._playing_since = time.time()
            elif not self._reply_on_device:
                self._playing = False  # nothing will play, so no antigua/done is coming

            if not transcript.strip():
                self._led(LED_ERROR)  # device flashes, then drops back to idle
            elif self._playing:
                self._led(LED_SPEAKING)
            else:
                self._led_idle()

            if audio_url:
                log.info(f"Audio URL:  {audio_url}")
                if not result.get("streaming"):
                    # Deterministic (non-LLM) handlers — time, weather, timers,
                    # sleep-word confirmations, etc. — return a ready audio_url
                    # synchronously instead of publishing chunks over MQTT like
                    # the streaming LLM path does. The bridge has to publish it
                    # itself or the Pi never hears about it (this was the actual
                    # cause of "no response" on deterministic answers).
                    self._play_reply(audio_url)

            if result.get("end_conversation"):
                # VAD-opened follow-up window caught background noise — server
                # says end silently, same as antigua_satellite.py's old behavior.
                log.info("=== FOLLOW-UP FALSE TRIGGER — ENDING SILENTLY ===")
                self._end_conversation()
                return

            clean = transcript.lower().strip().rstrip(".,!?")
            if clean in SLEEP_WORDS or any(clean.endswith(w) for w in SLEEP_WORDS):
                log.info(f"Sleep word detected in {transcript!r} — ending conversation")
                self._end_conversation()
                return

            self.conversation_id = result.get("conversation_id", conv_id)
            # Don't arm the follow-up window yet — antigua/done (published once
            # the Pi finishes playing this response) does that, so we're not
            # listening for a follow-up while Antigua is still talking.
        except Exception as e:
            log.error(f"Pipeline request failed: {e}")
            self._awaiting_reply = False
            if not self._reply_on_device:
                self._playing = False
            self._end_conversation()
            self._led(LED_ERROR)

    async def _start_va(self):
        start_va = self._services.get("start_va")
        if not start_va:
            log.error("start_va service not found on device")
            return
        await self.client.execute_service(start_va, {})
        log.info("start_va called — continuous external trigger armed")

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._connect_mqtt()
        await self.connect()
        entities, services = await self.client.list_entities_services()
        self._services = {s.name: s for s in services}
        by_id = {e.object_id: e.key for e in entities}
        self._button_key = by_id.get("user_button")
        self._button_event_key = by_id.get("button_press")
        self._vnr_key = by_id.get("voice-to-noise_ratio")
        self._media_player_key = by_id.get("media_player")
        if self._reply_on_device:
            if self._media_player_key is None:
                raise RuntimeError("reply_output: device, but the reSpeaker has no media_player entity")
            self._player_task = asyncio.create_task(self._device_player())
            log.info("Replies play on the reSpeaker (AEC active, no playback mute)")
        if "led_state" not in self._services:
            log.warning("Device firmware has no led_state action — LED control disabled until reflashed")
        self.client.subscribe_states(self._on_device_state)
        self.client.subscribe_voice_assistant(
            handle_start=self.handle_start,
            handle_stop=self.handle_stop,
            handle_audio=self.handle_audio,
        )
        await self._start_va()
        self._led_idle()
        log.info(f"Listening for '{WAKE_WORD}'...")

        self._last_audio_t = time.time()  # grace period starts now, not at connect()
        up_hits = 0
        while True:
            await asyncio.sleep(WATCHDOG_CHECK_S)
            if self.standby_for_host:
                up_hits = up_hits + 1 if await asyncio.to_thread(_host_up, self.standby_for_host) else 0
                if up_hits >= STANDBY_UP_AFTER:
                    log.info(f"{self.standby_for_host} is back — handing the reSpeaker back")
                    return
            silence = time.time() - self._last_audio_t
            if silence > WATCHDOG_STALL_S:
                raise RuntimeError(
                    f"No audio from device for {silence:.0f}s — stream likely stalled"
                )


def _host_up(host: str) -> bool:
    try:
        socket.create_connection((host, STANDBY_PROBE_PORT), timeout=2).close()
        return True
    except OSError:
        return False


async def _wait_until_host_down(host: str):
    log.info(f"Standby: leaving the reSpeaker to {host} until it stops answering")
    misses = 0
    while misses < STANDBY_DOWN_AFTER:
        await asyncio.sleep(STANDBY_POLL_S)
        misses = 0 if await asyncio.to_thread(_host_up, host) else misses + 1
    log.warning(f"Standby: {host} unreachable — taking over the reSpeaker")


async def _teardown(bridge):
    if bridge._player_task:
        bridge._player_task.cancel()
    try:
        await bridge.client.disconnect()
    except Exception:
        pass
    try:
        bridge.mqtt.loop_stop()
        bridge.mqtt.disconnect()
    except Exception:
        pass


async def main():
    if not CONFIG_PATH.exists():
        raise SystemExit(f"{CONFIG_PATH} not found — copy config.example.yaml first.")
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    standby_for = cfg.get("standby_for_host", "")
    while True:
        if standby_for:
            await _wait_until_host_down(standby_for)
        bridge = None
        try:
            bridge = KitchenBridge(cfg)
            await bridge.run()  # returns only when a standby hands the device back
        except Exception as e:
            log.error(f"Bridge crashed: {e} — reconnecting in 5s")
            await asyncio.sleep(5)
        finally:
            if bridge is not None:
                await _teardown(bridge)


if __name__ == "__main__":
    asyncio.run(main())
