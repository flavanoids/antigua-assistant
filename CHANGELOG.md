# Changelog

Highlights by month. Git history has the details. New entries go at the top.

## Unreleased

- **Offline awareness** (2026-10-16), after a DNS/internet outage that
  afternoon:
  - **Is the internet down, and why**: `netstatus.py` checks every 30 s, one
    layer at a time (network port, router, provider, DNS, web), and keeps an
    outage log. "Is the internet down?", "why?", "when did it go out?"
    get real answers instead of the model guessing.
  - **Offline mode**: news, podcasts, scores, prices, traffic, store hours and
    Apple Music playback answer at once ("I can't ... right now, the
    internet's been down since 4:02") instead of timing out; weather uses
    the last forecast; search and Wikipedia questions go to the model, told
    it can't look things up. She says when it's been out two minutes and
    when it's back (not overnight). State is retained on `antigua/net`.
  - **System stats**, read-only (`sysinfo.py`): load, memory, disks,
    CPU/GPU temperature, GPU use, uptime, and whether the services and the
    other boxes answer. No restarts, reboots or deletes.
  - `server/scripts/offline_drill.sh` blocks this box's internet (auto-restore
    cap) and runs a set of questions to check it all.

- **Doorbell announces people, not motion** (2026-10-16): wind and cars set
  off the doorbell's motion sensor 300 times a week, and every one said
  "somebody's at the front door". Cameras now speak only on `announce_on`
  (default `[person]`); motion is still logged and can still show the video.
  The doorbell is set to Humans only in the Eufy app so it reports people.

- **iPhones and the HomePod** (2026-10-16), set up in `docs/ios_shortcut.md`:
  - **Ask Antigua** is an iOS Shortcut that works from Siri, the Watch or a
    tap. It goes through a separate LAN-only port (9395) that needs a
    household key, so Antigua's own port stays wg0-only.
  - **Who's home** comes from the household's phones on the Wi-Fi (ARP
    scans). Arrival reminders ("remind me to ... when I get home") are spoken
    in the kitchen when that phone comes back.
  - **Announcements** play on the bedroom HomePod through Music Assistant
    ("tell the bedroom dinner's ready"), with a chime, and any music resumes
    afterwards.

- **Dashboard is LAN-only, enforced** (2026-10-16): a security review found
  a leftover Cloudflare Tunnel hostname publishing PinedaDisplay (photos,
  calendar, config backup, and the alarm and control endpoints that reach
  Antigua) to the internet with no login. The hostname is gone, and
  `pineda-web-guard` on the primary and backup now keeps `:8090` answering
  only the LAN, wg0 and localhost, and blocks cloudflared from reaching it
  even if an ingress rule comes back.

- **Security review fixes** (2026-10-16):
  - **Ollama** went from 0.32.14 to 0.40.5 on both servers, past the
    pull-API CVEs (CVE-2026-103663, -85180, -108836). On the primary it now
    listens on localhost only.
  - **urllib3 and Werkzeug** got security floors (2.8.0, 3.1.9) in the
    requirements and on all three boxes.
  - **Transcripts** in `tool_calls.db` keep 7 days instead of 30, and camera
    and lock events now expire after 30. Both prune while running.
  - **Request bodies** to both servers are capped (32 MB audio, 1 MB JSON).
    A bad `Content-Length` gets a 413 instead of hanging a thread.
  - **The TTS fallback, standby dashboard and Music Assistant** are reached
    over wg0, not cleartext LAN.
  - **Every service is sandboxed** by `server/30-hardening.conf` while still
    running as root: umask 077, read-only system dirs, private `/tmp`, no
    setuid. Data, logs and captures are private on disk.
  - **Only listed accounts on each host can reach MQTT** (an egress rule in
    the Antigua guard).
  - **SearXNG** is pinned to a dated image.
  - **CI is green again:** two lint errors, plus a pipeline test that failed
    on Fridays and Saturdays because it assumed which alarm fires first.

  Later the same day:
  - Password and root SSH logins are off on the Pi.
  - The reSpeaker's Noise key is rotated on the device and both bridges (the
    procedure is in deployment.md).
  - The raw speaker enrollment clips are deleted.
  - A weekly security drift check (`server/scripts/security_check.py`) is
    in place.
  - Disk encryption was considered and declined (decisions.md).

  - Antigua uses a non-admin Home Assistant user, limited to its own
    entities. Push updates moved to `state_changed` events (HA refuses
    `subscribe_trigger` to non-admin users), and the Eufy bridge restart now
    presses an `input_button` that an HA automation acts on
    (`security.eufy_restart_button`).

  Still open: Cloudflare Access on the remaining SSH tunnel (deferred).

- **Everyday basics, and no more pretending** (2026-10-15): a stopwatch,
  spelling, coin/dice/random numbers, "repeat that", "what can you do", bare
  "mute", "volume to 3"; indoor temperature, humidity and AC state from the
  AC controller; store hours from OpenStreetMap; stock, index and crypto
  prices; air quality and pollen; "show me a recipe for X". A reminder
  without a time asks when instead of the model saying "Got it". A command
  no skill can do gets "I can't do that yet", and the system prompt no
  longer tells the model it sets timers (it had been confirming things it
  never did). A weekday "good morning" plus an affirmation plays when the
  back door says you're back in from letting the dogs out.

- **Cameras and the front door** (2026-10-07): "show me the front door" puts
  the Eufy doorbell or garage camera's livestream on PinedaDisplay (HA
  pushes it into go2rtc) and closes it when the card goes away, so a battery
  camera isn't left streaming. A doorbell ring puts the camera up on its own;
  motion and person detections are only logged. "Lock the door" locks, and
  lock state, when, activity and battery questions are answered from
  Home Assistant plus a local SQLite event log that survives HA's recorder
  purge. Voice can never unlock: a mishearing or the TV saying her name
  must not open the door. Primary only. Details in
  `ANTIGUA_SKILLS/security/README.md`.

- **Mic pickup measured, clip by clip** (2026-10-08): two bridge tools for
  tuning from real data. `clip_test.py` captures one clip at a time from
  anywhere in the room and scores it through the exact live wake pipeline
  (peak, consecutive-frame runs over a threshold grid, RMS, transcript), so
  threshold changes can be read off the table instead of guessed;
  `capture_samples.py` batch-captures real utterances for wake-word
  training/validation, auto-split on speech and always keeping the raw
  session. First findings: the 0.75/3 confirm-frame pair is well calibrated
  (every position wakes except turned away at 20 ft, whose score profile is
  identical to TV noise); the reSpeaker's noise floor is hot (~1500 RMS in a
  dead-silent house), so gain isn't the lever for far-field pickup. A
  "hey antigua" custom model is now training on the primary.

- **Conversation mode** (2026-10-04): "Alexa, let's chat" starts a
  back-and-forth with no wake word between turns (20s window, soft green
  LED, no beeps). Every plain-speech turn must pass an addressee check (echo
  of her last line, then a one-word call to the model) or the chat ends
  silently with a soft cue, so the TV and side conversations don't get
  answered. Replies are shorter and chattier, the chat keeps 20 messages of
  history, and "Alexa" mid-reply cuts her off (`antigua/stop`, stricter 0.9
  threshold). Ends on "that's all"/"bye", silence, or speech not for her.
  Details in `ANTIGUA_SKILLS/conversation/README.md`.

- **Less speculative work on primary-server, faster long replies** (2026-10-01):
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
