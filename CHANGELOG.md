# Changelog

Highlights by month. Git history has the details. New entries go at the top.

## Unreleased

- **Conversation mode** (2026-10-04): "Alexa, let's chat" starts a
  back-and-forth with no wake word between turns (20s window, soft green
  LED, no beeps). Every plain-speech turn must pass an addressee check (echo
  of her last line, then a one-word call to the model) or the chat ends
  silently with a soft cue, so the TV and side conversations don't get
  answered. Replies are shorter and chattier, the chat keeps 20 messages of
  history, and "Alexa" mid-reply cuts her off (`antigua/stop`, stricter 0.9
  threshold). Ends on "that's all"/"bye", silence, or speech not for her.
  Details in `ANTIGUA_SKILLS/conversation/README.md`.

- **Less speculative work on noegpu01, faster long replies** (2026-10-01):
  the clock TTS pre-warm (three Kokoro syntheses a minute, ~2,950 a day
  against 0–35 real turns and 17 time questions in two weeks) is gone;
  "what time is it" synthesizes on demand in ~0.3s. The TTS cache now keeps
  a phrase 7 days from its last use (200 MB cap, least recently used out
  first) instead of an hour, so fixed replies stay instant. Weather fetches
  one refresh per location at a time — the prewarm and the answer it
  pre-speaks were each fetching, and the pre-spoken reply was built from the
  forecast one cycle behind, so it rarely matched. Long fixed replies
  (recipe intros and steps) go out a chunk at a time: a short first chunk
  plays in ~0.5s instead of waiting 5s for the whole thing.

- **Recipe questions answered from the recipe** (2026-09-30): mid-recipe
  free-form questions — "can I use a hand mixer?", "why a water bath?",
  "can I make this the day before?" — are answered by the LLM grounded in
  the recipe being cooked (scaled amounts, swaps, steps, current step),
  capped at `recipes.qa_max_tokens` (110, new in `server.example.yaml`).
  The hook only claims turns that name something in the recipe or use
  cooking vocabulary, and never takes timers, weather or news. New
  deterministic answers first: "can I skip X?" / "do I need X?" (the
  optional/essential rule, nothing recorded) and "how do I know when it's
  done?" (the step's doneness cue). The hard rule is enforced in code:
  `grounding.recipe_claims_ok` drops any sentence stating an amount, time,
  temperature, pan size or unit the recipe doesn't state — digits or
  spelled out ("five more minutes" fails) — and an all-filtered answer
  becomes "The recipe doesn't say." Long Q&A stretches don't expire the
  session. Design and decisions:
  `docs/recipe_questions_and_scaling_plan.md` (Phase B).
- **Recipe scaling** (2026-09-30): "chicken noodle soup for 4 people" starts
  the recipe already scaled, and mid-recipe "make it for 4", "I'm cooking for
  6 people", "double it", "halve it", "back to the original" rescale the
  amounts — pure arithmetic on the published quantities, so ingredient
  lines, "how much butter?" and step amounts all read at the new size, and
  she offers to read the new list. Whole things round ("about 2 bay
  leaves"), eggs that split get a yolk tip ("use 2 eggs plus 1 yolk"), and
  tiny amounts become "a pinch". Times, temperatures and pan sizes are never
  scaled — she says they're from the original recipe and warns when the pan
  may need to be bigger or smaller. A "for 4" request follows into "another
  recipe"; a plain factor resets. "How many does it serve?" reports the
  current size. Deterministic, no LLM. Decisions and design:
  `docs/recipe_questions_and_scaling_plan.md`.
- **Recipes, step by step** (2026-09-30): new `recipe` route. "How do I make
  cheesecake" finds a real recipe online (schema.org Recipe data on recipe
  sites, never written by the LLM), names its source, reads the ingredients
  and asks "Do you have everything?". Missing ingredients get substitutes
  from a curated table, one at a time; an essential one with no substitute
  can go on the shopping list. Then it reads one step at a time and waits for
  "next", "go back", "repeat that", "what was step 3", with answers from the
  recipe to "how much sugar?" / "how long in the oven?" and a timer offer on
  timed steps. "Another recipe" moves to the next one found. The session
  covers the whole house and survives restarts. New `antigua_core/recipe.py`,
  `recipe_session.py`, `recipe_subs.py`, `intents/recipe.py`, a `recipes:`
  config block and `tests/test_recipe.py`. Plan: `docs/recipe_skill_plan.md`.
- **People, history & events** (2026-09-30): new `knowledge` route. "Who
  was Frida Kahlo" / "tell me about the Cuban Missile Crisis" gets a 3–4
  sentence overview grounded in the Wikipedia article (es.wikipedia for
  Spanish questions). The article and its Wikidata facts stay on hand for 15
  minutes, so "Antigua, was she married?" / "where was she from?" / "tell me
  more" are answered from the passages that cover them. New
  `antigua_core/knowledge.py`, `intents/knowledge.py`, a `knowledge:` config
  block and `tests/test_knowledge.py`.
- **PinedaDisplay** (2026-09-29): new `pineda` route for the airplaypi
  kiosk. Change or randomize the theme, restart the display, reboot the Pi
  (after a spoken yes), say who wrote the quote on screen or search and
  explain it, speak the Spanish phrase (Spanish, English, Spanish), and say
  when the photo on screen was taken. Running timers are mirrored into the
  display's mini card. New `antigua_core/pineda.py`, `intents/pineda.py`
  and a `pineda:` config block.
- **Apple News Today** (2026-09-28): "Play Apple News Today" plays the day's
  episode through Music Assistant. Before it's out she plays the latest one,
  and on weekends she offers Friday's. New `antigua_core/podcast.py`.
  NPR's **Up First** added the same way (Saturdays included, the Sunday Story
  skipped); shows now live in a `podcasts:` config block.
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
