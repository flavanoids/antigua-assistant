# Changelog

Highlights by month. Git history has the details. New entries go at the top.

## Unreleased

- **Getting ready to publish** (2026-09-28):
  - Removed dead TTS code paths (Piper, the PyTorch Kokoro subprocess) and
    unrelated files.
  - Added per-component requirements files and `pyproject.toml` (ruff,
    pytest).
  - Household names, location, LAN addresses and device IDs moved into
    git-ignored config with `.example` templates; a new `household:` config
    block replaces the hardcoded names.
  - Systemd units now use path placeholders (`server/scripts/render_unit.py`).
  - The pre-commit hook blocks personal details.
  - Docs rewritten: README, `docs/architecture.md`, `docs/deployment.md` and
    `docs/decisions.md`. `RULES.md` and the finished plan docs were retired.
- **Structural cleanup** (Phase 4):
  - `classify.py` split into `antigua_core/intents/`, one parser module per
    skill; `classify.py` keeps the routing table.
  - Shared server helpers moved to `antigua_core/server_common.py`. This fixed
    three places the backup had drifted: snoozed-alarm wording, the
    display-topic gate, and MQTT connect retries.
  - Tests use a temp data dir (`ANTIGUA_DATA_DIR`), so they no longer touch
    live memories or timers. `settings.configure()` is idempotent.
  - pytest runs every suite, and GitHub Actions runs ruff and the tests on
    Python 3.11 and 3.13.
  - The satellite's output device is configurable, and its systemd unit is
    tracked.
  - The home city in weather parsing comes from config.
- **Publishing** (Phase 5): MIT license; a neutral example persona; code
  comments and logs refer to machines by role; SearXNG's `secret_key` moved
  out of the repo (the key was rotated).

## 2026-09

- **Kitchen mic.** A reSpeaker Lite over ESPHome replaced the Pi's USB mic.
  The bridge on the primary does wake word, VAD, LEDs and push-to-talk, and
  heals itself after stream stalls. Its mic stays muted until playback ends.
- **Deterministic skills.** Weather was rewritten on Open-Meteo. Timers,
  alarms and reminders are now typed, recurring, snoozable and tell each other
  apart ("which one?"). New: calculator and conversions (with currency),
  sports scores (NFL, MLB, NBA, MLS, F1).
- **Music and home control.** Apple Music via Music Assistant, with ducking
  during a turn. The TV is controlled through an Apple TV (mcp-pyatv) and a
  Roku, and the lights through Govee, all via MCP servers. The TV light bar
  flashes while an alarm rings.
- **Speech.**
  - STT on the GPU (whisper.cpp large-v3-turbo, Vulkan).
  - Spanish conversation and commands.
  - A warmer, blended Kokoro voice with a Latin-American inflection.
  - TTS runs locally on the primary, with the backup's TTS as fallback.
- **Reliability.**
  - The backup box is a real failover: TTS-only until the primary dies, then
    the full pipeline plus a standby mic bridge.
  - Ollama `num_ctx` pinned so the model stays on the GPU.
  - A stable prompt prefix for KV-cache reuse.
  - Alerts when TTS falls back or a disk fills.
- **Safety and privacy.**
  - Fixed two self-reply echo loops; speech-triggered follow-ups are off.
  - Antigua's traffic moved onto a WireGuard mesh behind a port guard.
  - Recordings and logs kept for 7 days.
  - Wall-display MQTT is off by default.
  - Antigua has her own playback volume.

## 2026-08

- Rebuilt after a fresh install: faster-whisper STT and a kokoro-onnx TTS
  server.
- Switched to a Qwen3.5-4B finetune, kept resident in VRAM.
- Broadened the persona and fixed a grounding-filter false positive.

## 2026-07

- Refactored the pipeline into the shared `antigua_core` package (tts_text,
  grounding, caches, stores, classify, pipeline). The fallback server was
  rewritten on top of it, which gives the two servers parity by construction.
- Added a routing snapshot test harness.
- Added shopping and to-do lists, and speaker ID for memory requests.
- Named timers and alarms, Alexa-style confirmations, and a 5-second alarm
  ring.
- Govee lights skill. Web search answers are now grounded in the snippets,
  and Antigua no longer volunteers the weather.

## 2026-06

- A streaming LLM pipeline, so replies start speaking before generation
  finishes.
- SearXNG web search, an ingredient substitution skill, better news, and Roku
  input switching.
- System prompt overhaul; AirPlay ducking.

## 2026-05

- Roku TV skill, faster TTS (streamed playback, content cache, static audio),
  wake-word threshold tuning, and a `/memories` endpoint for the wall display.

## 2026-04

- **First version:**
  - Pi satellite with openWakeWord and Silero VAD.
  - Whisper and Ollama on the primary.
  - Kokoro TTS on a second box.
  - MQTT between them.
- Weather, timers and alarms, news, time and date, the remember skill (with
  medicine tracking), media control, and the wall-display integration.
- Fallback pipeline on the second box for when the primary is offline.
