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

Every Antigua service also gets the sandbox drop-in `server/30-hardening.conf`:
private new files (`UMask=0077`), read-only `/usr` `/boot` `/etc`, a private
`/tmp`, no setuid, no kernel or cgroup changes. The services still run as root
(the satellite as its user). Install it per unit:

```bash
for u in antigua-server antigua-tts antigua-whisper kitchen-bridge; do   # primary
    sudo install -D -m 644 server/30-hardening.conf /etc/systemd/system/$u.service.d/30-hardening.conf
done
sudo systemctl daemon-reload   # then restart them
```

On the Pi it's `antigua-satellite`. `deploy_fallback.sh` installs it on the
backup. Keep the data private too:
`chmod -R go-rwx data logs audio_in audio_out kitchen-mic/bridge/captures`,
and `chmod 600` the configs that hold secrets (`server/config/server.yaml`,
`kitchen-mic/bridge/config.yaml`, `config/satellite.yaml`).

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
   in `models/`. For Kokoro's own English pronunciation (misaki) instead of
   espeak, build the TTS venv on Python 3.13 (spaCy has no 3.14 build):
   `uv venv -p 3.13 venv-tts && VIRTUAL_ENV=$PWD/venv-tts uv pip install -r server/requirements-tts-g2p.txt`.
   Then install `antigua-tts.service`, which runs from `venv-tts`; pointed at
   the main venv instead, the server falls back to espeak. Set its CPU
   threads in a drop-in, `/etc/systemd/system/antigua-tts.service.d/40-cpu.conf`
   (`[Service]` + `Environment=KOKORO_THREADS=<n>`): one per performance core.
   On a hybrid Intel CPU also pin it there with `CPUAffinity=`. The author's
   i5-12600K uses 6 and `CPUAffinity=0 2 4 6 8 10` (0.40→0.34 s for a short
   reply); the 4-core backup uses 4 (1.16→0.78 s). Check it with
   `curl -X POST localhost:5500/tts -H 'Content-Type: application/json' -d '{"text":"hi"}' -o /tmp/t.wav`.
6. **Search** (optional). Copy `searxng/searxng/settings.yml.example` to
   `settings.yml` and set `secret_key`, run `cd searxng && docker compose up -d`, then set
   `search.enabled: true`. See [the search skill](../ANTIGUA_SKILLS/search/README.md).
   The image is pinned to a dated build. To update, pick a tag a few days old
   from Docker Hub (they're rolling daily builds), change it in
   `searxng/docker-compose.yml`, `docker compose pull && docker compose up -d`,
   and re-run `deploy_fallback.sh` so the backup matches.
7. **Home control** (optional). Run `server/mcp/setup.sh`, put secrets in
   `mcp.env`, and pair the Apple TV. See [server/mcp/README.md](../server/mcp/README.md)
   and [the TV skill](../ANTIGUA_SKILLS/living_room_tv/README.md).
8. **Music** (optional). Create `server/music/.env`, then
   `docker compose -f server/music/docker-compose.yml up -d`. See
   [the music skill](../ANTIGUA_SKILLS/music/README.md).
9. **Server.** Install `antigua-server.service`. `curl localhost:9393/health`
   shows the model and MCP status.
10. **Retention.** Install `server/antigua.logrotate` as
    `/etc/logrotate.d/antigua` (render it like a unit; includes a `maxsize 50M`
    safety ceiling and 7-day retention), plus
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

**Rotating the Noise key** (if it ever leaks; done 2026-10-16). The device and
both bridges must change together, or the mic drops until they match:

1. Back up `secrets.yaml` and the bridge's `config.yaml`. Put a new key in
   `api_encryption_key` (`python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'`).
2. `esphome compile`, then `esphome upload --device kitchen-mic.local`. OTA
   uses `ota_password`, not the API key, so it works across the change.
3. Put the same key in `noise_psk` in `kitchen-mic/bridge/config.yaml` and
   restart `kitchen-bridge`. Wait for `Connected to kitchen-mic`.
4. Update the backup's standby bridge too (re-run `deploy_fallback.sh`, which
   regenerates its config from this one).

If you back out before flashing, restore the old `secrets.yaml` *and*
`esphome compile` again. Otherwise the cached `firmware.bin` still carries
the unused key, and a later bare `esphome upload` would flash it.

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
the satellite) at the backup, using its wg0 address once the mesh is up.

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
| Check log sizes & disk | `ls -lh logs/*.log && df -h / /tmp` |
| Force log rotation | `sudo logrotate -f /etc/logrotate.d/antigua` |
| Restart after a code change | `sudo systemctl restart antigua-server` (and `kitchen-bridge` / `antigua-tts` if you touched those) |
| Ask without speaking | `curl -X POST localhost:9393/pipeline_text -d '{"text":"tell me a fun fact"}'` (the full turn, skills included; silent unless `"quiet": false`) |
| Run a full turn out loud | `curl -X POST localhost:9393/pipeline_text -d '{"text":"what is 12 times 13","quiet":false}'` (skills included; the reply plays on the speakers) |
| Run the tests | `for t in tests/test_*.py; do venv/bin/python $t || echo "FAIL $t"; done` |
| Lint | `ruff check .` |
| Alerts | `server/scripts/health_alerts.py` from cron every 5 min: TTS fallback use, disk space. It sends through a `hermes send` command; swap in your own notifier |
| Security drift | `server/scripts/security_check.py` from cron Mondays 9:00, same notifier, quiet unless something changed: pip-audit on every venv, Ollama/Music Assistant behind a minor release, SearXNG pin over 60 days old, a public tunnel hostname added or pointed at the dashboard, a guard not loaded, a new LAN listener on the primary, red CI on `main`. `--dry-run` prints without sending. State: `data/security_check_state.json` (delete it to re-baseline) |
