# MCP servers

Antigua runs third-party [MCP](https://modelcontextprotocol.io) servers as
persistent stdio subprocesses and calls their tools from deterministic skill
handlers. The client is `server/antigua_core/mcp_client.py`: no SDK
dependency, one long-lived session per server, concurrent calls, and a respawn
if a server dies. The LLM never sees these tools. Intent parsing stays in
`classify.py`, so a 4b model can't mis-call your TV.

| Server | Package (pinned in `requirements.txt`) | Used by |
|---|---|---|
| `appletv` | [`mcp-pyatv`](https://github.com/crlian/mcp-pyatv) (own venv, `venv-atv/`, Python 3.13) | `living_room_tv`: power, volume, mute, apps, navigation, playback |
| `roku` | [`mcp-remote-control`](https://github.com/AaronGoldsmith/mcp-remote-control) | `living_room_tv`: inputs |
| `govee` | [`govee-mcp`](https://github.com/evefromwayback/govee-mcp) | `govee_lights` skill |

## Where they run

Each server box runs its own copies:

- **Primary**: `antigua-server` starts them at boot (`mcp_hub.warm()`).
- **Backup**: `antigua-fallback` starts them when it takes over and stops
  them when it stands down, so the box stays TTS-only while the primary is
  up. `deploy_fallback.sh` builds the venv there and copies `mcp.env`.

Both read the same `mcp:` block in `server/config/server.yaml`. Relative paths
resolve from the repo root, which on the backup is its deploy directory (`FALLBACK_DIR`).

`GET /health` on either server includes `"mcp": {name: {ready, tools}}`.

## Setup

```bash
server/mcp/setup.sh                     # builds server/mcp/venv (pinned)
cp /dev/null server/config/mcp.env      # gitignored; chmod 600
echo 'GOVEE_API_KEY=…' >> server/config/mcp.env
```

`venv/` pins `mcp<2`, because the Roku and Govee servers import
`mcp.server.fastmcp`, which SDK 2.x removed. `venv-atv/` is separate: mcp-pyatv
targets SDK 2.x and breaks on Python 3.14, so uv builds it on Python 3.13.

The Apple TV needs pairing once (the TV shows a PIN). The credentials in
`server/config/pyatv.conf` are gitignored, and `deploy_fallback.sh` copies
them to the backup. See the `appletv` entry in `server.yaml` for the command.

## Adding another MCP server

1. Add its package to `requirements.txt` (pin it) and re-run `setup.sh`.
   Then re-run `deploy_fallback.sh` so the backup gets it too.
2. Add an entry under `mcp.servers` in `server.yaml`: `command`, `env`,
   `timeout`. Put secrets in `mcp.env`, never in the yaml.
3. Check its tools:
   ```python
   from antigua_core.mcp_client import McpHub
   hub = McpHub.from_config(cfg["mcp"], BASE_DIR)
   hub.call("name", "some_tool", {...}).value()
   ```
4. Wire it to a skill the usual way (parser in `classify.py`, route,
   handler). Call it via an object on the `Backend` (see
   `home_control.HomeControl`), so tests can inject a fake hub.

Servers must speak stdio. For an HTTP-only server, add a transport to
`mcp_client.py`.
