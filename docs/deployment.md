# Deployment

How to stand up each role, from a fresh clone. Read
[architecture.md](architecture.md) first for what the roles are. Paths below
assume the repo is cloned to `$ANTIGUA` on each box (the author uses a home
directory; `/opt/antigua` works too).

Real config never goes in git. Each component has a tracked template; copy it,
fill it in, and the copy is git-ignored:

| Template | Copy to | Holds |
|---|---|---|
| `server/config/server.example.yaml` | `server/config/server.yaml` | Everything server-side: household, location, model, TTS URLs, devices, music, news, search |
| `server/config/system_prompt.example.txt` | `server/config/system_prompt.txt` | Antigua's persona |
| `config/satellite.yaml.example` | `config/satellite.yaml` | Satellite: server hosts, volume |
| `kitchen-mic/bridge/config.example.yaml` | `kitchen-mic/bridge/config.yaml` | Bridge: device, MQTT, failover |
| `kitchen-mic/esphome/secrets.yaml.example` | `kitchen-mic/esphome/secrets.yaml` | Wi-Fi and API key for the reSpeaker |
| `server/scripts/deploy.env.example` | `server/scripts/deploy.env` | Backup host and paths for `deploy_fallback.sh` |
| `network/hosts.conf.example` | `network/hosts.conf` | Host table for `wg-mesh.sh` |
| `searxng/searxng/settings.yml.example` | `searxng/searxng/settings.yml` | SearXNG settings and its `secret_key` |
| — | `server/config/mcp.env` | Secrets: `GOVEE_API_KEY`, `MUSIC_ASSISTANT_TOKEN` (chmod 600) |
| — | `server/music/.env` | `MA_DATA_DIR` for Music Assistant |

If `server.yaml` or `system_prompt.txt` is missing, the servers log a warning
and use the example file, so a fresh clone starts, but with placeholder values.

Enable the repo's pre-commit hook in every clone you commit from:

```bash
git config core.hooksPath .githooks
# optional: extended regexes for your own names/IPs/paths, one per line
$EDITOR .githooks/private-patterns
```

## Systemd units

Units under `server/`, `kitchen-mic/bridge/` and `network/` contain
placeholders (`@ANTIGUA_DIR@`, `@WHISPER_DIR@`, `@OLLAMA_MODEL@`). Render and
install them with:

```bash
server/scripts/render_unit.py server/antigua-server.service \
    | sudo tee /etc/systemd/system/antigua-server.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now antigua-server
```

`--dir` overrides the repo path and `--whisper-dir` the whisper.cpp path
(default: `whisper.cpp` next to the repo).

## Primary

Needs a GPU that Ollama and whisper.cpp (Vulkan) can use. The author runs an
AMD RX 6650 XT with 8 GB.

1. **Python.**
   `python3 -m venv venv && venv/bin/pip install -r server/requirements.txt -r server/requirements-tts.txt`.
   Also install `espeak-ng` (the Kokoro phonemizer) and, for music and search,
   Docker.
2. **Config.** Copy `server.example.yaml` and `system_prompt.example.txt` as
   above. At minimum, set `household`, the `weather` home point and
   `search.home_city`; `mqtt.broker` is the satellite's address.
3. **LLM.** Install [Ollama](https://ollama.com), then
   `ollama pull <ollama.model>`. Install `antigua-model-warmup.service` so the
   model is in VRAM before the first request.
4. **STT.** Build whisper.cpp with Vulkan next to the repo
   (`cmake -B build -DGGML_VULKAN=ON && cmake --build build -j`), download
   `large-v3-turbo-q8_0` (`bash models/download-ggml-model.sh large-v3-turbo-q8_0`),
   and install `antigua-whisper.service`. Without it, the server falls back to
   faster-whisper on the CPU.
5. **TTS.** Put `kokoro-v1.0.onnx` and `voices-v1.0.bin` (from the
   [kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases))
   in `models/`, then install `antigua-tts.service`. Check it with
   `curl -X POST localhost:5500/tts -H 'Content-Type: application/json' -d '{"text":"hi"}' -o /tmp/t.wav`.
6. **Search** (optional). Copy `searxng/searxng/settings.yml.example` to
   `settings.yml` and set `secret_key`, run `cd searxng && docker compose up -d`, then set
   `search.enabled: true`. See [the search skill](../ANTIGUA_SKILLS/search/README.md).
7. **Home control** (optional). Run `server/mcp/setup.sh`, put secrets in
   `mcp.env`, and pair the Apple TV. See [server/mcp/README.md](../server/mcp/README.md)
   and [the TV skill](../ANTIGUA_SKILLS/living_room_tv/README.md).
8. **Music** (optional). Create `server/music/.env`, then
   `docker compose -f server/music/docker-compose.yml up -d`. See
   [the music skill](../ANTIGUA_SKILLS/music/README.md).
9. **Server.** Install `antigua-server.service`. `curl localhost:9393/health`
   shows the model and MCP status.
10. **Retention.** Install `server/antigua.logrotate` as
    `/etc/logrotate.d/antigua` (render it like a unit), plus
    `kitchen-mic/bridge/captures-prune.{service,timer}`.

## Kitchen mic (reSpeaker Lite)

1. Copy `kitchen-mic/esphome/secrets.yaml.example` to `secrets.yaml` and fill
   it in. Generate the API key with the command in that file.
2. Flash the device: `esphome run kitchen-mic/esphome/kitchen-mic.yaml` (USB
   the first time, OTA after that).
3. On the primary, set up the bridge:

   ```bash
   cd kitchen-mic/bridge
   python3 -m venv venv && venv/bin/pip install -r requirements.txt
   cp config.example.yaml config.yaml   # noise_psk = the api_encryption_key
   ```

   Set `continuous_external_trigger: true`, the MQTT broker (the satellite)
   and `wake_cue_url`.
4. Install `kitchen-mic/bridge/kitchen-bridge.service`. The bridge log shows
   `Listening for 'alexa'...` once it's connected.

More detail: [kitchen-mic/README.md](../kitchen-mic/README.md).

## Satellite (Raspberry Pi)

Needs PulseAudio (`paplay`), Mosquitto and, for AirPlay, shairport-sync using
its PulseAudio backend.

1. Clone the repo, then
   `python3 -m venv venv && venv/bin/pip install -r requirements-satellite.txt`.
2. Copy `config/satellite.yaml.example` to `config/satellite.yaml` and set
   `server.host` / `fallback_host` (the WireGuard addresses once the mesh is
   up).
3. Optional: put an alarm bell at `audio_in/alarm_clock.ogg`. Without one,
   alarms speak but don't ring.
4. If you aren't using a HiFiBerry DAC+, set `satellite.output_device` to
   your PulseAudio sink (`pactl list short sinks`).
5. Install the unit as the desktop user, so `paplay` reaches that user's
   PulseAudio:
   `server/scripts/render_unit.py antigua-satellite.service | sudo tee /etc/systemd/system/antigua-satellite.service`
   (`--user` picks a different user).

To update, pull the repo on the Pi and `sudo systemctl restart antigua-satellite`.

## Backup (optional)

Everything is pushed from the primary:

```bash
cp server/scripts/deploy.env.example server/scripts/deploy.env   # fill in
server/scripts/deploy_fallback.sh
```

This mirrors `antigua_core`, the fallback server, config, MCP venvs and the
standby bridge to `FALLBACK_DIR`, renders the units with that path, and
restarts them. Re-run it whenever `antigua_core`, `server.yaml` or the bridge
changes. On the primary, add a cron job that copies memories and lists every
5 minutes:

```cron
*/5 * * * * rsync -az $ANTIGUA/data/memories.json $ANTIGUA/data/lists.json <backup>:<FALLBACK_DIR>/data/
```

Then point `tts.fallback_url` (on the primary) and `server.fallback_host` (on
the satellite) at the backup.

## Network

Once all three boxes work over the LAN, move Antigua's traffic onto the
WireGuard mesh:

```bash
cp network/hosts.conf.example network/hosts.conf   # fill in
GUARD_MODE=observe network/wg-mesh.sh             # log LAN hits first
network/wg-mesh.sh                                # then enforce
```

Then switch the configs to the `wg0` addresses. See
[network/README.md](../network/README.md).

## Operating it

| Task | Command |
|---|---|
| Logs | `tail -f logs/server.log`, `logs/kitchen-bridge.log`, `logs/tts_server.log`, `logs/whisper_server.log`; on the Pi, `journalctl -fu antigua-satellite` |
| Restart after a code change | `sudo systemctl restart antigua-server` (and `kitchen-bridge` / `antigua-tts` if you touched those) |
| Ask the LLM without speaking | `curl -X POST localhost:9393/ask -d '{"text":"tell me a fun fact"}'` (LLM only, skips skills, plays nothing) |
| Run a full turn from text | `curl -X POST localhost:9393/pipeline_text -d '{"text":"what is 12 times 13"}'` (skills included; the reply plays on the speakers) |
| Run the tests | `for t in tests/test_*.py; do venv/bin/python $t || echo "FAIL $t"; done` |
| Lint | `ruff check .` |
| Alerts | `server/scripts/health_alerts.py` from cron every 5 min: TTS fallback use, disk space. It sends through a `hermes send` command; swap in your own notifier |
