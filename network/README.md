# Antigua's private network

Antigua's hosts talk to each other over a WireGuard mesh (`wg0`), and a small
nftables guard keeps Antigua's ports off the LAN. Other devices on the LAN
(TVs, plugs, bulbs) can't subscribe to Antigua's MQTT topics, inject
`antigua/play` / `antigua/alarm`, fetch reply audio, or POST to `/pipeline`.

The reSpeaker's own link is separate and already private: ESPHome's native
API with Noise encryption (`api: encryption:` on the device, `noise_psk` in
`kitchen-mic/bridge/config.yaml`).

## Addresses and ports

| Role      | wg0       | Guarded port (tunnel + localhost only) |
|-----------|-----------|----------------------------------------|
| primary   | 10.77.0.1 | 9393 antigua-server, 11434 Ollama (also bound to 127.0.0.1) |
| satellite | 10.77.0.2 | 1883 mosquitto                         |
| backup    | 10.77.0.3 | 9394 antigua-fallback                  |

WireGuard listens on UDP 51820 on each host's LAN address. LAN addresses, ssh
targets and the services per host live in `network/hosts.conf` (git-ignored;
copy `hosts.conf.example`).

## What it doesn't touch

- **Routing:** each peer's `AllowedIPs` is its `/32`; there's no default route
  or `DNS=` in `wg0.conf`. Internet, web search, MCP servers, Docker and LXC
  all use the LAN default route exactly as before.
- **AirPlay:** shairport-sync and Bonjour stay on `wlan0`. `wg0` has no
  MULTICAST flag, so avahi never advertises on it.
- **Services, listeners:** services keep listening where they did. The guard
  drops LAN packets to their ports instead, so localhost always works and a
  slow tunnel at boot can't stop a service from starting.
- **Other ports:** Music Assistant (8095) and backup TTS (5500) stay on the
  LAN. The PinedaDisplay dashboard (8090) on the primary and backup has its
  own guard from the PinedaDisplay_Web repo, `pineda-web-guard`. It answers
  the LAN, wg0 and localhost, and can still fetch its data from the internet,
  but it's never reachable from outside the LAN: non-private sources are
  dropped, and cloudflared can't connect to it whatever its ingress says. See
  that repo's `docs/OPERATIONS.md` ("Keeping it on the LAN").

## Pieces

- `wg-mesh.sh`: run from the primary; safe to re-run. It installs wireguard-tools,
  generates each host's private key on that host (the key never leaves), writes
  `/etc/wireguard/wg0.conf`, installs the guard, and adds the drop-ins.
- `antigua-guard.nft` → `/etc/antigua/guard.nft`: its own `inet antigua_guard`
  table, so reloading it never touches Docker's rules. LAN hits are logged to
  the kernel log with the prefix `antigua-guard:`.
- The same table's output chain controls who may talk to MQTT. Mosquitto has
  no passwords (`allow_anonymous`); the tunnel keeps other machines out, and
  this keeps other accounts out. On each host only the accounts in
  `hosts.conf`'s last column (default `root`) can connect to the broker
  (`127.0.0.1` or its wg0 address). Anyone else, say a compromised `ollama`
  or a login account, gets a TCP reset, logged as `antigua-guard-mqtt:`.
  Today that's `root,pineda` on the primary and the backup (pineda-web runs
  as `pineda`) and root plus the Pi's desktop user on the Pi (the satellite,
  shairport-sync and PinedaDisplay's AirPlay bridge run as that user). A new
  MQTT client running as another user needs its account added there, then a
  re-run of `wg-mesh.sh`.
- `antigua-guard.service`: loads the guard before the network comes up.
- `10-wireguard.conf`: a drop-in that orders the tunnel's users after `wg0`
  (`Wants`, not `Requires`). Installed for the services listed per host in
  `hosts.conf`: antigua-server and kitchen-bridge on the primary,
  antigua-satellite on the satellite, and antigua-fallback and
  kitchen-bridge-standby on the backup.

Clients use the wg0 addresses: `mqtt.broker` in `server/config/server.yaml`
and the bridge config, `server.host` / `fallback_host` in
`config/satellite.yaml`, `[mqtt] host` in `/etc/pineda-web/config.toml`, and
`PRIMARY_HOST` in the fallback server. Reply-audio URLs use whichever local
address reaches the broker, which is the host's wg0 address.

Links to unguarded ports use wg0 too (2026-10-16), so nothing Antigua sends
crosses the LAN in cleartext: `tts.fallback_url` (reply text to the backup's
Kokoro), `pineda.standby_url` (alarms mirrored to the standby dashboard) and
`music.url` (Music Assistant, with its bearer token). The one exception is
Home Assistant (`security.ha_url`), which isn't in the mesh: its token still
crosses the LAN over plain HTTP until HA gets HTTPS. Since 2026-10-16 that
token belongs to a non-admin HA user limited to the entities Antigua uses,
so a sniffed token can't administer HA.

## Operating it

```sh
network/wg-mesh.sh                      # install / refresh, guard enforcing
GUARD_MODE=observe network/wg-mesh.sh   # guard logs LAN hits but lets them through
sudo wg show                            # peers and last handshakes
sudo nft list table inet antigua_guard  # rules and drop counters
journalctl -k | grep antigua-guard      # who tried to reach a guarded port
```

To roll back on a host, run
`systemctl disable --now antigua-guard wg-quick@wg0`, point the clients above
back at the LAN addresses, and restart them.
