# Skill: Govee Lights

**Status:** Active
**Pipeline stage:** Pre-LLM side-effect, via MCP (optimistic; runs in a background thread)
**LLM involved:** No (deterministic confirmation)

---

## What It Does

Controls the Govee lights (power, color, brightness, white temperature)
through the [`govee-mcp`](https://github.com/evefromwayback/govee-mcp) MCP
server. That server uses the LAN (UDP) when a light supports and allows it,
and falls back to the Govee cloud API otherwise.

It replaces the old setup, where we spawned a patched single-device
`govee_mcp_server` over SSH on the satellite for every command (about 2–3s,
cloud only, Pi required). Now each Antigua server keeps one long-lived session
to its own local copy, so the lights also work from the backup during failover.
See [`server/mcp/README.md`](../../server/mcp/README.md).

The spoken confirmation is **optimistic**: Antigua replies immediately
("Turning on the hallway lights") while the commands finish in a daemon
thread. Failures are logged, not spoken.

---

## Devices

`parse_govee_request()` returns aliases; `govee.devices` in `server.yaml`
maps each alias to a Govee device ID. The server is addressed by ID, not by
the Govee app's names: the six chandelier bulbs have duplicate names and a
"Chadelier" typo.

| Alias | Govee device | SKU | Spoken names |
|---|---|---|---|
| `tv-bar` | TV Bar | H6054 | "tv bar", "tv light bar", "tv lights" |
| `rope-neon` | Rope Neon Light | H61A0 | "rope light", "neon light" |
| `pink-left` / `pink-right` | Pink Lights | H6008 | "left/right pink light" |
| `hallway-1` / `hallway-2` | Hallway Lights | H6008 | "hallway light one/two" |
| `chandelier-1`…`6` | Kitchen chandelier bulbs | H600B | "kitchen chandelier" (group only) |
| `monitor-strip` | Monitor Strip | H612F | "monitor strip", "monitor lights" |

Groups: "pink lights", "hallway lights", "chandelier", "all the lights" (all
13). Group commands fan out one tool call per device in parallel (up to
`govee.concurrency` at once) over the same MCP session.

---

## How Users Trigger It

- **Power:** "turn on the hallway lights", "turn the tv bar off", "turn off all the lights"
- **Color:** "set the rope light to purple", "make the tv bar red"
- **Brightness:** "set the tv lights to 50 percent", "dim the hallway lights" (dim = 20%)
- **White warmth:** "set the chandelier to 5700 kelvin", "make the chandelier daylight"

Color, warmth and brightness send `set_power(on)` first, because the H6054 TV
bar otherwise stages the setting without lighting up. Kelvin is clamped per
device: 2700–6500K for chandelier and monitor strip, 2000–9000K for the rest.

Guards (unchanged): questions ("are the hallway lights on?") and mentions
without a command verb fall through to the LLM. That guard stops Antigua's own
speech from flipping the lights (2026-09-23).

---

## MCP tools used

`set_power(name, on)`, `set_brightness(name, level)`,
`set_color(name, red, green, blue)`, `set_color_temp(name, kelvin)`, and
`refresh()`. The server caches its device list on disk for a day. If a
command gets "no device named …" (a light that was offline, or the API key
was just added), `HomeControl` runs `refresh` at most once a minute and
retries.

---

## Config Knobs

```yaml
mcp:
  env_file: server/config/mcp.env   # GOVEE_API_KEY=… (gitignored, chmod 600)
  servers:
    govee:
      command: [server/mcp/venv/bin/govee-mcp]
      env:
        GOVEE_PREFER_LAN: "true"
        GOVEE_CACHE_PATH: data/govee-mcp/devices.json
        GOVEE_GROUPS_PATH: data/govee-mcp/groups.json
      timeout: 20

govee:
  mcp_server: govee
  concurrency: 6
  devices: {tv-bar: "72:76:…", …}
```

---

## LAN vs cloud

Without an API key, only lights with **LAN Control** enabled in the Govee app
are visible. On 2026-09-25 that was just the rope neon (answered in 0.3s). For
faster, cloud-independent control, enable LAN Control per device in the Govee
app (Device → Settings → LAN Control). The H600B and H612F may not support it;
those stay on the cloud.

---

## Limitations

- **Optimistic replies**: if a light is unreachable, Antigua still says
  "Turning on…". The failure only appears in the log.
- **No status by voice**: `get_device_state` exists but isn't wired to an
  intent.
- **No relative brightness** ("a bit dimmer").
- **Scenes** (`set_scene`) are available from the server but not wired to
  voice yet.
- **New devices**: add a pattern row in `_GOVEE_DEVICE_PATTERNS`
  (`classify.py`) and an ID under `govee.devices`. IDs come from the
  server's `list_devices` tool.
