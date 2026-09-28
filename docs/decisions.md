# Design decisions

Why Antigua is built the way it is. Each entry is a choice that looks
arbitrary without the story behind it. Keep them short and add to the list
when you make a decision that someone might otherwise undo. Dates are when the
decision was made or last revisited.

## Voice input

**Wake word: openWakeWord's built-in `alexa` model, threshold 0.75.**
It's reliable and needs no training. A custom "Hey Antigua" model is wanted
but deferred: openWakeWord trains on synthetic TTS speech, and that needs a
PyTorch training setup the primary doesn't have. In an analysis of 95 turns
(Sep 24–27, 2026), raising the threshold from 0.6 to 0.75 removed most false
wakes without losing any real command. Recordings that hit the 15-second cap
are dropped, because those are almost always TV or conversation.

**Wake word detection runs on the server, not on the mic** (2026-09-21). The
reSpeaker could run its own `micro_wake_word` model, but the bridge instead
calls `start_va` once and runs Antigua's shared model on the continuous
stream. That keeps a single wake word, a single threshold and a single code
path for every mic.

**The Pi's USB mic was retired; the reSpeaker replaced it** (2026-09-21). A
mic sitting next to the speakers heard Antigua's own replies. The reSpeaker
sits where people actually talk, and the Pi became output-only.

**Speech-triggered follow-ups are off; only the wake word continues a
conversation.** Twice (2026-09-01 and 2026-09-23), an "any speech reopens the
mic" window let Antigua hear her own reply and answer herself in a loop. Once
this switched every light on. The bridge now also mutes the mic from the
moment a recording is sent until `antigua/done`. Re-enabling speech follow-ups
needs acoustic echo cancellation for the satellite's speakers first. The
reSpeaker's `reply_output: device` mode gives its echo canceller a reference
signal.

**No Whisper `prompt`.** A prompt listing household names turned mic echo and
noise into a name, which the LLM took as being addressed.

**STT: whisper.cpp large-v3-turbo on the GPU (Vulkan)** (2026-09-25). About
0.3 s per turn, and it hears "pause" and artist names over music, which
`small.en` got wrong. faster-whisper `small.en` stays as the CPU fallback;
`medium.en` is a second opinion only for short, unmatched transcripts while
music plays.

## LLM

**A small local model (4B Qwen via Ollama) that isn't trusted with actions.**
Every action (timers, TV, lights, music, lists, memories) is parsed by regex
in `classify.py` and executed in code. The LLM never calls tools, so it can't
misfire the TV. It handles conversation and questions, with search results or
headlines as context when needed.

**Deterministic skills come before the LLM.** Weather, timers, calculator,
sports, time and date answer from code with canned phrasing. The 4B model
stalled or made things up on simple questions ("do I need a jacket") that
household members expect to just work.

**`num_ctx` is 8192 on every Ollama call.** The Ollama service default of
131k spilled 22% of the model to CPU (35 instead of 65 tokens/s). Any call
with a different `num_ctx` forces a model reload, so the chat, streaming,
search router and warmup calls all pass the same value.

**The system prompt carries a coarse time ("around 3 PM, afternoon"), not the
minute.** A per-minute timestamp made the roughly 1,100-token system message
unique every turn, so Ollama re-evaluated all of it. With the hour instead,
prefill went from a 0.63 s median to 0.20 s. Exact time questions never reach
the LLM anyway.

**The model is pinned in VRAM** (`keep_alive: -1`, plus a warmup unit at
boot). Speed matters more than idle VRAM.

**Grounding guard.** `grounding.py` drops LLM sentences that state names or
numbers absent from the context it was given, and weather claims when no
weather data was provided. The prompt alone didn't stop the 4B model from
inventing weather in greetings.

**The system prompt forbids made-up house facts and shared memories.** The
model readily confabulated ("I remember when you…"). If you edit the persona,
keep the `WEB SEARCH RESULTS` section: without it the model refused live
questions even with results in its context.

## Voice output

**Kokoro TTS with a fixed three-voice blend:** 0.45 `af_bella` + 0.30
`af_heart` + 0.25 `ef_dora`. Bella and Heart give warmth, and Dora (Kokoro's
Latin-American Spanish voice) gives the English a light accent that matches
the name. Spanish replies flip the proportions. The blend is baked into
`kokoro_tts_server_flask.py`.

**The primary TTS runs on the primary box; the backup's copy is the
fallback** (2026-09-24). A reply took about 1.3 s locally against about 4 s on
the backup's CPU.

**"Antigua" is respelled "Antigwa" before synthesis.** Otherwise Kokoro says
the Caribbean island. Household names that it mispronounces get a `say_as`.

**Numbers are spelled out before TTS.** `calc` and other skills build their
phrasing with `num2words`, and `tests/test_calc.py` fails if any digit
survives `clean_for_tts()`.

## Playback and the satellite

**PulseAudio on the Pi.** ALSA exclusive access conflicted between Antigua's
playback and shairport-sync (AirPlay). PulseAudio lets both share the DAC.

**Antigua has her own volume** (`satellite.antigua_volume_pct`), set as a
per-stream `paplay --volume`, independent of the AirPlay volume. A voice "turn
it up" never changes Antigua's level: it goes to the music player if music is
playing, otherwise to the TV.

**Alarms ring briefly** (`alarm_ring_seconds`, 5 s by default). The wake word
is unreliable while a bell plays near the mic.

**Govee alarm flashes use device scenes, not server-side pulsing.** Pulsing
brightness from the server got the cloud-only TV light bar rate-limited (HTTP
429). A scene animates on the device and costs about four cloud calls per
alarm.

**Wall-display integration is off by default** (`display.enabled`). The
display-only MQTT topics are dropped unless it's on. Playback and alarm topics
are never gated.

## Integrations

**Home control goes through third-party MCP servers.** The Apple TV handles
power, volume, apps and playback over HDMI-CEC; the Roku handles only input
switching, which the Apple TV can't do. Govee runs through its MCP server with
LAN control where the device allows it. Intent parsing stays in `classify.py`;
MCP is just the execution layer, reached through a small stdio client
(`mcp_client.py`) with no SDK dependency.

**Music: Apple Music via Music Assistant**, using MA's native API rather than
its MCP plugin, whose album data lacks `album_type`. "Newest album" needs that
field to skip singles.

**Currency rates come from open.er-api.com, not Frankfurter.** Frankfurter
returned HTTP 403 from this network.

**Web search goes through a self-hosted SearXNG.** Google web results are
blocked upstream on this instance, so it uses Bing and DuckDuckGo; Google News
still works for news queries.

## Network, failover, privacy

**What goes online.** The core loop (wake, STT, LLM, TTS) is local. Skills
that need live data call the internet: weather (Open-Meteo, NWS), news (RSS,
Google News), sports (ESPN), currency (open.er-api.com), search (SearXNG to
Bing and DuckDuckGo), music (Apple Music), and Govee's cloud for some lights.
Mic audio and transcripts never leave the house.

**A WireGuard mesh plus a port guard** (2026-09). LAN devices (TVs, plugs,
bulbs) can't reach Antigua's MQTT topics, inject playback, fetch reply audio
or POST to `/pipeline`. Only the peers' `/32` addresses route over the tunnel,
so AirPlay, internet access and Docker are untouched.

**The backup box is idle until needed.** It serves TTS all the time but loads
no model and starts no MCP servers until `/activate`, which keeps a
small-memory box responsive. Timers aren't synced to it, because both servers
would fire them.

**Recordings and logs are kept for 7 days** (2026-09-28). The mic streams
continuously, and false wakes capture private conversation. Captures are
pruned hourly and logs rotated daily. Nothing under `data/`, `logs/` or
captures is ever committed, and the pre-commit hook enforces it.

**Real config is git-ignored** (2026-09-28). Household names, location, LAN
addresses and device IDs live in local files; the tracked `*.example.*`
templates carry generic values, and a fresh clone falls back to them.
