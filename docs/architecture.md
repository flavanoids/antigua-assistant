# Architecture

How Antigua is put together: which box does what, what happens during one
spoken turn, and how the pieces talk to each other. For setup, see
[deployment.md](deployment.md). For why things are the way they are, see
[decisions.md](decisions.md).

## Roles

Antigua runs on three machines plus a Wi-Fi microphone. Code and docs refer
to them by role.

| Role | Hardware | Runs |
|---|---|---|
| **Primary** | x86 box with an AMD RX 6650 XT (8 GB) | STT, LLM, TTS, the pipeline, the kitchen-mic bridge, SearXNG, Music Assistant, MCP servers |
| **Satellite** | Raspberry Pi 4 + HiFiBerry DAC+ | Mosquitto (MQTT), reply/alarm playback, AirPlay (shairport-sync), primary health checks |
| **Backup** | small x86 box, CPU only | Backup TTS (always on); a complete fallback pipeline and a standby mic bridge that only start when the primary is down |
| **Kitchen mic** | Seeed reSpeaker Lite (ESP32-S3, XMOS) running ESPHome | Streams microphone audio; LED ring and button driven by the bridge |

```text
 reSpeaker Lite ──ESPHome API (Noise-encrypted)──► kitchen bridge ─┐
 (kitchen mic)                                     (primary)        │ POST /pipeline (WAV)
                                                                    ▼
             ┌─────────────────────────── primary ─────────────────────────────┐
             │ whisper.cpp server :8178 (GPU) ─► antigua_server :9393          │
             │ Ollama :11434 (GPU)  ◄───────────  antigua_core.pipeline        │
             │ Kokoro TTS :5500     ◄───────────  (skills, routing, grounding) │
             │ SearXNG :8080 · Music Assistant :8095 · MCP servers (stdio)     │
             └──────────────────────────────┬──────────────────────────────────┘
                                            │ MQTT antigua/play {audio_url}
                                            ▼
             satellite: Mosquitto :1883 ─► antigua_satellite ─► paplay ─► speakers
                        shairport-sync (AirPlay) shares the same output

             backup: Kokoro TTS :5500 (used when the primary's TTS fails)
                     antigua_fallback :9394 + standby bridge (idle until takeover)
```

All Antigua traffic between the three machines runs over a WireGuard mesh, and
a firewall guard keeps Antigua's ports off the LAN. See
[network/README.md](../network/README.md).

## One turn, start to finish

1. **Wake.** The kitchen bridge keeps a continuous audio stream from the
   reSpeaker and runs openWakeWord's `alexa` model on it. When the wake word
   fires, it publishes `antigua/cue` (the satellite plays a short chime) and
   `antigua/listening` (music ducks).
2. **Record.** Silero VAD finds the end of the command. The mic stays muted
   from the moment the recording is sent until the satellite reports
   `antigua/done`, so Antigua never hears herself.
3. **Transcribe.** `antigua_server` sends the WAV to whisper.cpp
   (large-v3-turbo, Vulkan, ~0.3 s). If that server is down, faster-whisper
   `small.en` runs on the CPU. A short transcript that matches no skill while
   music plays gets a second opinion from `medium.en`. Spanish is used only
   when the detector is confident (see
   [spanish_support_plan.md](spanish_support_plan.md)).
4. **Correct.** `classify.correct_transcript()` fixes known mishearings
   ("tired" → "timer", "routers" → "reuters", household names from
   `household.misheard_as`).
5. **Route.** `classify.classify()` walks `ROUTE_ORDER` and picks the first
   skill whose parser matches. Timers, weather, calculator, sports, lists,
   memory, music, TV, lights and volume are all **deterministic**: regex
   parsing, then canned phrasing, with no LLM involved. Anything unmatched goes
   to the `llm` route. When a question sounds like it needs fresh facts, a
   quick LLM router call decides whether to web-search first.
6. **Answer.**
   - Skill routes build the reply in code, sometimes calling an MCP server
     (Apple TV, Roku, Govee) or Music Assistant.
   - The LLM route streams from Ollama (4B Qwen, `num_ctx` 8192, pinned in
     VRAM). The prompt includes the current daypart, local weather, and any
     search results or headlines. `grounding.py` drops sentences that assert
     names or numbers missing from the provided context, and weather claims
     when no weather data was given.
7. **Speak.** `tts_text.clean_for_tts()` turns numbers, units, dates and
   abbreviations into words and fixes pronunciations. Kokoro (a fixed blend of
   three voices) synthesizes the first sentence of an LLM reply on its own, so
   audio starts early, and the rest as one utterance, so the intonation stays
   continuous. If the primary TTS fails, the backup box's copy is used.
   Results are cached by content hash for an hour.
8. **Play.** Each WAV URL goes out on `antigua/play` as soon as it's ready.
   The satellite fetches the WAVs over the tunnel, plays them in order with
   `paplay`, and publishes `antigua/done` when the queue drains.
9. **Follow up.** For 8 seconds after `antigua/done`, saying the wake word
   again continues the same conversation (5-minute context, last 6 messages).
   Plain speech alone does not reopen the mic; that caused echo loops (see
   [decisions.md](decisions.md)).

Timers, alarms and reminders are the exception to request/response. They fire
from the server, which publishes `antigua/alarm`. The satellite rings, speaks
the announcement, and can flash a Govee light scene while it rings.

## Routes

`ROUTE_ORDER` in `server/antigua_core/classify.py` is the source of truth.
Order matters, because earlier routes win. For example, "turn the music up"
must reach `music` before `volume`. Each route has a handler in
`pipeline._HANDLERS` and fixtures in `tests/fixtures/routing.yaml`.

```text
garbage · govee · tv · music · volume · time_date · weather · display
memory_save · memory_forget_content · memory_forget_last · medicine_query
memory_query · list_add · list_query · list_remove · timer_cancel · timer_add
timer_reset · timer_status · snooze · reminder_set · alarm_set · timer_set
sports · calc · news · substitution · search · llm
```

Per-skill details live in [ANTIGUA_SKILLS/](../ANTIGUA_SKILLS/README.md). The
user-facing list of things you can say is [SKILLS.md](../SKILLS.md).

## Code map

| Path | What it is |
|---|---|
| `server/antigua_server.py` | Primary server: config, STT/LLM/TTS backends, HTTP endpoints, MQTT, background warmers |
| `server/antigua_fallback_server.py` | Backup server: same pipeline with CPU STT, a 2B model and local TTS; idle until `/activate` |
| `server/antigua_core/` | Shared by both servers. `pipeline.py` (turn handling), `classify.py` (`ROUTE_ORDER` and `classify()`), `intents/` (one parser module per skill, re-exported by `classify.py`), `settings.py` (config), `server_common.py` (helpers both servers use), `household.py`, `stores.py` (memories, lists, timers, conversations), `caches.py` (weather, news, search), `tts_text.py`, `grounding.py`, `weather.py`, `sports.py`, `calc.py`, `music.py`, `home_control.py`, `mcp_client.py`, `spanish.py`, `speaker_id.py` |
| `server/kokoro_tts_server_flask.py` | Kokoro TTS HTTP server (`POST /tts` → WAV); the voice blend lives here |
| `antigua_satellite.py` | Satellite on the Pi: playback queue, alarms, ducking, failover watcher |
| `kitchen-mic/bridge/kitchen_bridge.py` | reSpeaker ↔ Antigua bridge: wake word, VAD, LEDs, button, failover |
| `kitchen-mic/esphome/` | ESPHome config for the reSpeaker |
| `network/` | WireGuard mesh and port guard |
| `server/config/*.example.*`, `config/satellite.yaml.example` | Config templates; the real files are git-ignored |
| `tests/` | Network-free test suites; each runs standalone (`python3 tests/test_x.py`) |

## HTTP endpoints

Primary, on port 9393:

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Model and MCP server status; the satellite polls this to detect failover |
| GET | `/audio/<file>.wav` | Reply audio, kept for `server.audio_ttl_seconds` |
| GET | `/memories`, `/lists`, `/timers` | Current state (JSON) |
| POST | `/pipeline` | WAV in → full turn (the bridge uses this) |
| POST | `/pipeline_text` | Text in → full turn, skipping STT |
| POST | `/ask` | Text in → LLM reply + audio URL. It skips the skills and publishes nothing to MQTT, so nothing plays |
| POST | `/stt` | WAV in → transcript |

Backup, on port 9394: `/health`, `/memories`, `/lists`, `/pipeline`,
`/activate`, `/deactivate`.

Kokoro TTS, on port 5500: `GET /health`; `POST /tts` with
`{"text", "speed"?, "lang"?: "en"|"es"}` returns 24 kHz mono WAV.

## MQTT topics

The broker is Mosquitto on the satellite. The ✱ topics are published only when
`display.enabled` is true; they feed an optional wall display.

| Topic | From → to | Payload | Purpose |
|---|---|---|---|
| `antigua/play` | server, bridge → satellite | `{"audio_url"}` | Play a reply sentence or cue |
| `antigua/done` | satellite → bridge | `{"state": "complete"}` | Playback finished: unmute the mic, open the follow-up window |
| `antigua/cue` | bridge → satellite | `{"audio_url"}` | Wake chime |
| `antigua/listening` | bridge → satellite, server | `{"active"}` | Duck or restore music during a turn |
| `antigua/alarm` | server → satellite | `{"text", "audio_url", "label", "kind"}` | Ring a timer, alarm or reminder |
| `antigua/alarm_ack` | satellite → server | `{"label"}` | Alarm dismissed |
| `antigua/kitchen_play` | bridge | `{"audio_url"}` | Replies for the reSpeaker's own speaker (`reply_output: device`) |
| `antigua/status` ✱ | server, satellite | `{"state"}` | Pipeline state; also `fallback_active` / `primary_restored` |
| `antigua/transcript` ✱ | server | `{"text", …}` | What was heard |
| `antigua/response` ✱ | server | `{"text", "audio_url"}` | What was said |
| `antigua/wake`, `antigua/audio_level` ✱ | satellite | | Display animation |
| `antigua/memory`, `antigua/command`, `antigua/timer_set` ✱ | server | | Display cards |

## Failover

Two independent paths:

- **The primary's TTS fails** (the process is up but Kokoro is unreachable):
  `synthesize()` retries on `tts.fallback_url` (the backup's Kokoro, same voice
  blend). `server/scripts/health_alerts.py` alerts if this keeps happening.
- **The primary is down.**
  1. The satellite polls `/health` every 20 s. On failure it POSTs
     `/activate` to the backup, which loads its 2B model and starts its MCP
     servers.
  2. The backup's standby bridge sees the primary stop answering and takes
     over the reSpeaker. ESPHome allows only one client, so the two bridges
     never listen at the same time.
  3. If only `antigua-server` is down (the host is up), the primary's bridge
     sends turns straight to the backup's `/pipeline`.
  4. When the primary returns, the satellite sends `/deactivate`, and the
     standby bridge lets go.

Memories and lists are copied to the backup every 5 minutes by a cron `rsync`
on the primary. Timers are not synced, because both servers would fire them.
What the backup can't do: web search is limited when the primary box is
down, since SearXNG runs there; Music Assistant needs the primary box up; and
speaker ID is primary-only.

## Data and retention

| Data | Where | Kept |
|---|---|---|
| Memories | `data/memories.json` | 14 days (`memory.ttl_days`) unless marked permanent |
| Lists, timers | `data/lists.json`, `data/timers.json` | Until removed; timers survive restarts |
| Speaker profiles | `data/speaker_profiles.json` | Until re-enrolled (none enrolled yet) |
| Mic captures | `kitchen-mic/bridge/captures/` | 7 days (`captures-prune.timer`) |
| Logs (they contain transcripts) | `logs/` | 7 days (`antigua.logrotate`) |
| Reply audio | `audio_out/` | Minutes; the TTS cache for an hour |

Nothing in `data/`, `logs/` or `captures/` is ever committed. The pre-commit
hook enforces this.
