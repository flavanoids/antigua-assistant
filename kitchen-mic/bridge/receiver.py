"""Standalone aioesphomeapi client for the kitchen-mic reSpeaker Lite.

Plays the role Home Assistant normally plays for an ESPHome voice-assistant
satellite: connects to the device as a CLIENT and subscribes to its
voice_assistant audio stream. Modeled directly on Home Assistant core's
homeassistant/components/esphome/assist_satellite.py (handle_pipeline_start /
handle_audio / handle_pipeline_stop) and aioesphomeapi's
APIClient.subscribe_voice_assistant().

Not wired into Antigua. For now, on_utterance_audio() just writes a debug
WAV per utterance to captures/, so you can confirm audio actually arrives
before touching antigua_satellite.py or server/ at all.

Usage (after flashing + config.yaml is filled in):
    pip install -r requirements.txt
    python3 receiver.py
"""

import asyncio
import logging
import time
import wave
from pathlib import Path

import yaml
from aioesphomeapi import APIClient
from aioesphomeapi.model import VoiceAssistantAudioSettings, VoiceAssistantFeature

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kitchen-mic-bridge")

CONFIG_PATH = Path(__file__).parent / "config.yaml"
CAPTURES_DIR = Path(__file__).parent / "captures"

SAMPLE_RATE = 16000  # ESPHome voice_assistant default; confirm against
SAMPLE_WIDTH = 2  # device once connected (device_info / audio_settings).
CHANNELS = 1


def on_utterance_audio(pcm_chunks: list[bytes]) -> None:
    """Called once per finished utterance with all its raw PCM chunks.

    TODO: this is the hook point for Antigua wiring. Once proven, either:
      - feed pcm_chunks into the same code path antigua_satellite.py uses
        for openWakeWord + the /pipeline POST to the primary, or
      - if this bridge ends up running ON the primary (see README), call
        straight into whatever that side exposes instead of going back
        through the Pi at all.
    For now it just proves audio is flowing.
    """
    CAPTURES_DIR.mkdir(exist_ok=True)
    out_path = CAPTURES_DIR / f"utterance_{int(time.time())}.wav"
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(pcm_chunks))
    log.info(f"Wrote {out_path} ({sum(len(c) for c in pcm_chunks)} bytes)")


class KitchenMicBridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client = APIClient(
            address=cfg["device"]["host"],
            port=cfg["device"].get("port", 6053),
            password=None,
            noise_psk=cfg["device"].get("noise_psk"),
        )
        self._current_utterance: list[bytes] = []
        self._api_audio = False  # confirmed against device_info() at connect time

    async def connect(self):
        await self.client.connect(login=True)
        info = await self.client.device_info()
        flags = info.voice_assistant_feature_flags
        self._api_audio = bool(flags & VoiceAssistantFeature.API_AUDIO)
        log.info(
            f"Connected to {info.name} (esphome {info.esphome_version}); "
            f"voice_assistant_feature_flags={flags} api_audio={self._api_audio}"
        )
        if not self._api_audio:
            # HA falls back to a UDP audio server in this case (see
            # assist_satellite.py's _start_udp_server). Not implemented here
            # yet — confirm this device actually needs it before building it;
            # most current ESPHome builds report API_AUDIO.
            log.warning(
                "Device does not report API_AUDIO — UDP audio path is not "
                "implemented in this scaffold yet."
            )

    async def handle_start(
        self,
        conversation_id: str,
        flags: int,
        audio_settings: VoiceAssistantAudioSettings,
        wake_word_phrase: str | None,
    ) -> int | None:
        log.info(
            f"Pipeline start requested (wake_word_phrase={wake_word_phrase!r}, "
            f"conversation_id={conversation_id})"
        )
        self._current_utterance = []
        self.client.send_voice_assistant_event(
            event_type=1,  # VOICE_ASSISTANT_RUN_START — see aioesphomeapi.model.VoiceAssistantEventType
            data=None,
        )
        return 0  # port unused on the API_AUDIO path

    async def handle_audio(self, data: bytes, data2: bytes | None = None) -> None:
        self._current_utterance.append(data)

    async def handle_stop(self, abort: bool) -> None:
        log.info(f"Pipeline stop (abort={abort})")
        if not abort and self._current_utterance:
            on_utterance_audio(self._current_utterance)
        self._current_utterance = []

        if self.cfg.get("continuous_external_trigger"):
            # Custom action exposed by the community ESPHome config
            # (api: actions: - action: start_va). Confirm the exact service
            # name via client.list_entities_services() once connected —
            # this assumes the formatBCE base config's naming.
            await self.client.execute_service(
                service=None,  # TODO: resolve UserService object for "start_va"
                data={},
            )

    async def run(self):
        await self.connect()
        self.client.subscribe_voice_assistant(
            handle_start=self.handle_start,
            handle_stop=self.handle_stop,
            handle_audio=self.handle_audio if self._api_audio else None,
        )
        if self.cfg.get("continuous_external_trigger"):
            log.info("continuous_external_trigger enabled — TODO: kick off start_va here too")
        await asyncio.Event().wait()  # run forever


async def main():
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"{CONFIG_PATH} not found — copy config.example.yaml to config.yaml "
            "and fill in the device's host/noise_psk first."
        )
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    bridge = KitchenMicBridge(cfg)
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
