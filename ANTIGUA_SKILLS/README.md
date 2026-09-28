# Antigua Skills

Each subfolder documents one skill: what it does, how it's triggered, where the code lives, and how to extend or modify it.

---

## Skill Index

| Folder | Route(s) | LLM involved |
|---|---|---|
| [`weather/`](weather/) | `weather` | No: deterministic phrasing from Open-Meteo |
| [`timers/`](timers/) | `timer_*`, `alarm_set`, `reminder_set`, `snooze` | No |
| [`time_date/`](time_date/) | `time_date` | No |
| [`calc/`](calc/) | `calc` | No |
| [`remember/`](remember/) | `memory_*`, `medicine_query` | Only for general memory questions |
| [`lists/`](lists/) | `list_*` | No |
| [`music/`](music/) | `music` | No |
| [`living_room_tv/`](living_room_tv/) | `tv` | No (Apple TV + Roku via MCP) |
| [`govee_lights/`](govee_lights/) | `govee` | No (via MCP) |
| [`volume/`](volume/) | `volume` | No |
| [`news/`](news/) | `news` | Yes: reads the fetched headlines |
| [`search/`](search/) | `search`, plus the router on `llm` | Yes: answers from SearXNG results |
| [`substitution/`](substitution/) | `substitution` | Yes |
| [`conversation/`](conversation/) | `llm` | Yes |
| [`sleep_words/`](sleep_words/) | — (kitchen bridge) | No |

Sports (`sports`) has no README yet; see [SKILLS.md](../SKILLS.md#sports-scores)
and `server/antigua_core/sports.py`.


---

## Pipeline Stages (where skills can hook in)

Since the Phase 2 refactor (July 2026) the pipeline lives in
`server/antigua_core/` and is shared by the primary and fallback servers.
`run_pipeline` (in `antigua_core/pipeline.py`) dispatches on
`classify(transcript)` (in `antigua_core/classify.py`) through a
route→handler map — the branch order is `ROUTE_ORDER`, and every route is
covered by `tests/fixtures/routing.yaml`.

```text
Audio in
  └─ STT (Backend.transcribe)
       └─ STT corrections (correct_transcript)
            └─ classify(transcript) → route
                 ├─ [BYPASS] Weather, Timers, Time/Date, Calc, Sports, Lists, Memory,
                 │           Music, TV, Govee, Volume  ← handler replies in code, no LLM
                 ├─ [INJECT]  Memory query, News, Substitution, Search  ← context into the LLM tail
                 └─ LLM tail (Backend.ask_llm_stream), grounding guard
                      └─ TTS (first sentence alone, rest as one utterance)
                           └─ MQTT antigua/play → satellite plays audio

Kitchen bridge (kitchen-mic/bridge/kitchen_bridge.py):
  └─ wake word, VAD, sleep words, follow-up window
Satellite (antigua_satellite.py):
  └─ playback queue, alarms and their dismissal, ducking
```

---

## Skills backed by an MCP server

The TV (Apple TV + Roku) and Govee run on third-party MCP servers rather than hand-written API
code. Intent parsing stays deterministic (regex in `antigua_core/intents/`); MCP is only
the execution layer, called through `antigua_core/mcp_client.py`. To wire in
another MCP server, see [`server/mcp/README.md`](../server/mcp/README.md).

---

## Template for a New Skill

1. Create a folder: `ANTIGUA_SKILLS/my_skill/`
2. Copy the template below into `ANTIGUA_SKILLS/my_skill/README.md`
3. Implement in `server/antigua_core/`: add the `parse_X()` predicate to `intents/<skill>.py` (re-export it from `classify.py`), a route to `ROUTE_ORDER` + `classify()` in `classify.py`, and a handler to `_HANDLERS` in `pipeline.py`. Add fixtures (including neighbouring skills' phrases it might steal) to `tests/fixtures/routing.yaml` and run `python3 tests/test_routing.py` and `python3 tests/test_pipeline.py`
4. New config keys go in `antigua_core/settings.py` (`configure()`), in your local `server.yaml`, **and** in `server/config/server.example.yaml` with a generic value.
5. If the skill needs the satellite, implement it in `antigua_satellite.py` at the repo root (the file the Pi runs), then pull on the Pi and restart `antigua-satellite`. The Pi's `config/satellite.yaml` is local; `config/satellite.yaml.example` is the template.
6. If the backup server should have it, re-run `server/scripts/deploy_fallback.sh`.

### Skill README template

```markdown
# Skill Name

**Status:** Active | In Progress | Planned
**Pipeline stage:** LLM bypass | Context injection | Bridge | Satellite

## What It Does
One or two sentences.

## How Users Trigger It
- "Example phrase 1"
- "Example phrase 2"

## Detection
Function: `parse_X(transcript)` in `server/antigua_core/intents/<skill>.py`
Pattern: regex or keyword match

## Response
How the response is generated (Python directly / LLM / both).

## Code Location
- Detection: `parse_X()` in `antigua_core/intents/<skill>.py`
- Pipeline hook: `_handle_X()` in `antigua_core/pipeline.py` (`_HANDLERS` map)
- Supporting class/cache: `XCache` or `XManager` if applicable

## Config Knobs
What can be changed in server.yaml or satellite.yaml without touching code.

## MQTT Events
Any topics published or consumed specific to this skill.

## Limitations
What it can't do.

## How to Extend
What files to touch and what pattern to follow.
```
