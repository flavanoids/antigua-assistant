# Kitchen mic

Antigua's microphone is a Seeed **reSpeaker Lite** (ESP32-S3 with an XMOS
audio front end) running ESPHome, placed where people talk and away from the
speakers. It replaced a USB mic on the Pi, which sat next to the speakers and
kept hearing Antigua's own replies.

The device just streams audio. Everything else happens in
`bridge/kitchen_bridge.py` on the primary, which plays the role Home Assistant
would normally play for an ESPHome voice satellite:

1. It connects as an `aioesphomeapi` client (Noise-encrypted) and calls the
   device's `start_va` action once, so the device streams continuously and
   its on-device `micro_wake_word` never runs
   (`continuous_external_trigger: true`).
2. It runs openWakeWord's `alexa` model on the stream (threshold 0.75, 3
   confirming frames). This is the same model and settings used for every
   mic.
3. On a wake, it publishes `antigua/cue` (chime) and `antigua/listening`
   (ducks the music), records until Silero VAD hears the end of speech, and
   POSTs the WAV to `server_url` (`/pipeline`).
4. It keeps the mic muted until the satellite reports `antigua/done`, then
   opens an 8-second window where the wake word continues the same
   conversation. Plain speech in that window is ignored on purpose; see
   [docs/decisions.md](../docs/decisions.md).
5. A watchdog reconnects if the audio stream goes silent for 30 s. The
   aioesphomeapi stream can stall without raising an error.

**LED and button.** The single LED shows idle, listening (brightness follows
your voice), thinking, speaking, follow-up window, error and do-not-disturb.
Pressing the button is push-to-talk. Long-press do-not-disturb exists but is
disabled (`BUTTON_DND_ENABLED`): an unwired button fired phantom long presses
overnight. The USR button needs its USR→D2 jumper soldered; until the bridge
sees a release, it ignores button events.

**Reply output.** With `reply_output: pi` (the default), replies play on the
satellite. With `device`, the server streams them to `antigua/kitchen_play`
and the bridge plays them on the reSpeaker's own speaker output. That gives
the XMOS echo canceller a reference signal, so the mic doesn't need muting
and the wake word can interrupt a reply.

**Failover.** If `server_url` refuses the connection, turns go to
`fallback_server_url`. A second copy of the bridge on the backup box, with
`standby_for_host` set, stays off the device while the primary answers and
takes it over when the primary doesn't. ESPHome allows one voice client at a
time, so the two copies never listen together.

## Layout

| Path | What |
|---|---|
| `esphome/kitchen-mic.yaml` | Device config. Pulls the community [formatBCE reSpeaker Lite base config](https://github.com/formatBCE/Respeaker-Lite-ESPHome-integration) for the XMOS/I2C bring-up, and overrides the LED, button and `led_state`/`led_level` actions the bridge drives. Also enables WAV playback, which the base config leaves out. |
| `esphome/secrets.yaml.example` | Wi-Fi and API key template. Copy it to `secrets.yaml` (git-ignored). |
| `bridge/kitchen_bridge.py` | The bridge |
| `bridge/config.example.yaml` | Bridge config template. Copy it to `config.yaml` (git-ignored); `noise_psk` must equal the device's `api_encryption_key`. |
| `bridge/kitchen-bridge.service`, `bridge/kitchen-bridge-standby.service` | Units for the primary and the backup (render with `server/scripts/render_unit.py`) |
| `bridge/captures-prune.{service,timer}` | Deletes captures older than 7 days |
| `bridge/receiver.py` | Bare diagnostic client: logs device info and writes each utterance to `captures/`, with no Antigua wiring |
| `bridge/tools/` | `dryrun.py` (end-to-end check with recorded clips, no speaker output), `wakeword_test.py`, `startva_test.py` |

## Setup

See [docs/deployment.md](../docs/deployment.md#kitchen-mic-respeaker-lite).
In short: fill in `secrets.yaml`, then `esphome run esphome/kitchen-mic.yaml`,
then set up the bridge venv and `config.yaml`, then install
`kitchen-bridge.service`.

**Hardware notes**

- The XMOS pipeline-stage registers used by the Home Assistant Voice PE
  (0x30/0x40) return I2C errors on reSpeaker Lite firmware 1.1.0, so don't
  bother with them.
- The bridge logs the device's voice-to-noise ratio sensor at each wake,
  which is useful when tuning placement.
