# Antigua

A self-hosted voice assistant for the home. You say the wake word in the
kitchen, and Antigua listens, answers, and plays the reply through the house
speakers. Speech recognition, the language model and the voice all run on
your own hardware.

Named after Antigua Guatemala, the city, not the island.

## What it does

- **Answers questions** with a small local LLM, using a self-hosted web search
  for anything current.
- **Timers, alarms and reminders**, including named timers, recurring alarms
  ("every weekday at 7"), snooze, and "which one?" when there are several.
- **Weather** for home or anywhere: rain timing, "do I need a jacket",
  weekends. It also announces severe-weather alerts unprompted.
- **Music** from Apple Music on any AirPlay speaker, by artist, album, song,
  lyrics or year, with music ducked while you talk.
- **TV and lights**: power, volume, apps and inputs through an Apple TV and a
  Roku; Govee lights with groups and scenes.
- **Calculator and conversions**, including currency; **sports scores**;
  **news** headlines by source or topic.
- **Shopping lists** and **household memory** ("remember I took my
  medicine", "did I take my pills today?").
- **Spanish**: speak Spanish and she answers in Spanish.

The full list of things you can say is in [SKILLS.md](SKILLS.md).

Anything that controls something (timers, TV, lights, music, lists) is parsed
and executed by deterministic code, not the LLM. The model only talks, so a 4B
model can't switch your TV off by misreading a sentence.

## How it works

```text
reSpeaker Lite ─► kitchen bridge ─► STT (whisper.cpp, GPU) ─► skill or LLM (Ollama)
  (Wi-Fi mic)      wake word + VAD                                 │
                                                        Kokoro TTS ◄┘
                                                             │ MQTT
                                   Raspberry Pi satellite ◄──┘ ─► speakers (+ AirPlay)
```

| Role | Runs |
|---|---|
| Primary (GPU box) | STT, LLM, TTS, the pipeline, the mic bridge, SearXNG, Music Assistant |
| Satellite (Raspberry Pi) | MQTT broker, reply and alarm playback, AirPlay |
| Backup (optional, CPU box) | Backup TTS; takes over the whole pipeline if the primary goes down |
| Kitchen mic | Seeed reSpeaker Lite running ESPHome |

The three machines talk over a WireGuard mesh, and Antigua's ports are
firewalled off the LAN. Details: [docs/architecture.md](docs/architecture.md).

## What stays local and what goes online

Local: wake word, recordings, speech recognition, the LLM, the voice, your
memories and lists. Audio and transcripts never leave the house, and
recordings and logs are deleted after 7 days.

Online, only for skills that need live data: weather (Open-Meteo, NWS), news
(RSS feeds, Google News), sports (ESPN), currency rates (open.er-api.com), web
search (your SearXNG instance, which queries Bing and DuckDuckGo), Apple Music,
and Govee's cloud for lights that can't be controlled over the LAN.

## Getting started

You'll need:

- a Linux box with a GPU that Ollama and whisper.cpp (Vulkan) can use (8 GB
  VRAM is enough),
- a Raspberry Pi with a DAC and speakers,
- a Seeed reSpeaker Lite.

A second small box for failover is optional.

```bash
git clone <this repo> antigua && cd antigua
git config core.hooksPath .githooks
cp server/config/server.example.yaml server/config/server.yaml
cp server/config/system_prompt.example.txt server/config/system_prompt.txt
# edit both, then follow docs/deployment.md
```

[docs/deployment.md](docs/deployment.md) walks through each role.

## Documentation

| Doc | For |
|---|---|
| [SKILLS.md](SKILLS.md) | What you can say |
| [docs/architecture.md](docs/architecture.md) | Roles, a turn end to end, routes, MQTT and HTTP, failover, data |
| [docs/deployment.md](docs/deployment.md) | Setting up each machine, config files, operating it |
| [docs/decisions.md](docs/decisions.md) | Why things are the way they are |
| [ANTIGUA_SKILLS/](ANTIGUA_SKILLS/README.md) | Per-skill internals, and how to add a skill |
| [kitchen-mic/](kitchen-mic/README.md), [network/](network/README.md), [server/mcp/](server/mcp/README.md) | Component docs |
| [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md) | Working on it, and what changed |

## Status

Antigua is a personal project running in one house. It works well there, but
it isn't packaged, and parts of it assume that house's hardware (a HiFiBerry
DAC on the Pi, an AMD GPU, an Apple TV). Expect to adapt things.

## License

[MIT](LICENSE).
