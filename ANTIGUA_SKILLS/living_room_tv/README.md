# Skill: Living Room TV

**Status:** Active. Apple TV control needs a one-time pairing; see Setup.
**Pipeline stage:** Pre-LLM side-effect, via MCP (route `tv`)
**LLM involved:** No (deterministic confirmation)

---

## What It Does

One TV, two MCP servers:

| Function | Provider | MCP server |
|---|---|---|
| Power, volume, mute, apps, navigation, play/pause | **Apple TV** "Living Room" (4K) | [`mcp-pyatv`](https://github.com/crlian/mcp-pyatv) (pyatv, Companion protocol) |
| Inputs (HDMI 1–4, live TV, AV) | **Roku TV** (address in `mcp.servers.roku.env.HOST_IP`) | [`mcp-remote-control`](https://github.com/AaronGoldsmith/mcp-remote-control) (ECP) |

The Apple TV reaches the TV itself (power, volume) over **HDMI-CEC**. That
needs "Control TVs and Receivers" on the Apple TV (Settings → Remotes and
Devices) and CEC on the Roku TV (Settings → System → Control other devices).

`living_room_tv.providers` in `server.yaml` sets which server handles each
function. Each is a list in order of preference. The Roku server can do
everything except playback and non-Roku apps, so adding `roku` after
`appletv` gives a fallback. When a later provider exists, a failed one is
skipped for `failure_cooldown_seconds`. A lone provider is always tried.

Works on the primary and the backup; each runs its own copies of
the servers. See [`server/mcp/README.md`](../../server/mcp/README.md).

---

## How Users Trigger It

"TV", "Apple TV", "Roku" and "television" all mean this screen.

- **Power:** "turn on the tv", "turn the apple tv off"
- **Volume / mute:** "tv volume up", "volume down on the tv", "mute the tv", "unmute the tv"
- **Playback:** "pause the tv", "resume the tv", "play the apple tv". Bare
  "pause" stays with the satellite's media route.
- **Navigation:** "tv home", "go back on the tv"
- **Apps:** "open netflix", "put on youtube on the tv", "launch disney plus",
  "open plex". The list is in `_TV_APP_RE` (`classify.py`) and
  `_TV_APPS` (`home_control.py`).
- **Inputs:** "switch to hdmi 2", "switch to the playstation", "switch to live tv"

---

## Apple TV calls

| Action | Tool |
|---|---|
| power on/off | `turn_on` / `turn_off` |
| volume up/down | `volume_up` / `volume_down` |
| mute | `get_volume` (remembered), then `set_volume(0)` |
| unmute | `set_volume(<remembered level, else 30>)` |
| home / back | `navigate(direction="home" / "menu")` |
| play / pause | `play` / `pause` |
| open app | `list_apps` (cached 1h), match by name, then `launch_app(<bundle id>)` |

The MCP server has no mute tool, hence the volume save and restore. Whether
`set_volume` works over CEC on this TV is **unverified until pairing**.

Apps are matched against what is actually installed on the Apple TV. A
missing app gets "I don't see Plex on the Living Room TV." rather than a
failure.

---

## Setup

```bash
server/mcp/setup.sh     # builds venv-atv (Python 3.13 via uv) with the pins

# Find <APPLE_TV_ID> with: server/mcp/venv-atv/bin/atvremote scan
# With the TV on, pair once per protocol (the TV shows a PIN):
server/mcp/venv-atv/bin/atvremote --id <APPLE_TV_ID> --protocol companion \
    --storage-filename server/config/pyatv.conf pair
server/mcp/venv-atv/bin/atvremote --id <APPLE_TV_ID> --protocol airplay \
    --storage-filename server/config/pyatv.conf pair

server/scripts/deploy_fallback.sh   # copies pyatv.conf to the backup
```

---

## Limitations

- **Unpaired or unreachable Apple TV:** pyatv retries for about 18s, so the
  call is capped at 10s (`mcp.servers.appletv.timeout`) and then reported as
  "did not respond". The server pre-connects at startup so the first command
  skips the ~3s scan.
- **CEC dependence:** if CEC is off, the Apple TV can sleep and wake itself
  but not the TV. Power and volume then need `roku` added as a fallback.
- **tvOS 27.2 is newer than pyatv 0.18 has been tested against.** mcp-pyatv
  already notes `play_url` is broken on tvOS 26.
- **Roku standby:** a standby Roku TV holds keypresses about 5s, and the Roku
  MCP gives up at 5s, so an input switch while the TV is off may report
  failure.
- **No state query by voice** (`power_state` and `now_playing` exist, but
  aren't wired to intents).
