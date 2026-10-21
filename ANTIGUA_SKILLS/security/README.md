# Skill: Cameras & Front Door

**Status:** Active (primary server only)
**Pipeline stage:** LLM bypass (route `security`, first after `garbage`, ahead of `govee` so "show me the porch" isn't a light)

---

## What It Does

The Eufy doorbell and garage cameras and the front door lock, through Home
Assistant. Ask for a camera and its livestream goes up on PinedaDisplay; ask
about the lock and the answer comes from a local event log. The LLM is never
involved: detection is regex, the replies are templates, and the facts come
from Home Assistant or the local log.

A doorbell ring puts the front door camera on the display by itself. Motion
and person detections are logged, and can optionally trigger spoken announcements
over Antigua's satellite speakers with configurable cooldown (`motion_cooldown_seconds`)
and quiet hours (`motion_hours`).

The visitor announcement framework supports natural phrasing for identified people:
- Unknown visitor: `"Somebody is at the front door."`
- Single visitor: `"Alex's at the front door."` (or `"James is at the front door."`)
- Dual visitors: `"Maria is at the front door with Vivicca."`
- Multiple visitors: `"Alex, Jordan, and Maria are at the front door."`
- Known visitor with guest: `"Alex is at the front door with a guest."`

Visitor identity can be supplied via:
1. **Home Assistant Sensors / Attributes**: Eufy BionicMind (`person_name`), Frigate (`sub_label`), or Double Take entity attributes.
2. **MQTT Topics**: Direct announcements on `antigua/security/visitor`, Double Take matches on `double-take/matches`, or Frigate events on `frigate/events`.

**There is no unlock.** "Unlock the door" gets an explanation, never an
action — a mishearing or the TV saying her name must never open the door.
Locking is fine.

---

## How Users Trigger It

- "Show me the front door" / "pull up the garage camera" / "who's at the
  door?" / "can I see the porch?"
- "Lock the door"
- "Did I lock the door?" / "is the door locked?" / "when did the door get
  locked?" / "when was it last unlocked?"
- "When did the doorbell ring?" / "who triggered the garage cam?" / "did
  anyone come by the front door earlier today?"
- "What's the lock's battery?" / "how's the doorbell camera's battery?"

Camera words: "front door", "porch" and "doorbell" are the front camera;
"garage" and "driveway" the garage camera. The show-verb regexes deliberately
won't take "the porch **light**" or the "garage **door opener**" — those stay
Govee's and the TV's.

---

## Setup

Needs Home Assistant with the `eufy_security` integration, go2rtc (HA pushes
the P2P livestreams into it) and PinedaDisplay. Configure the `security:`
block in `server.yaml` (`ha_url`, `token_file`, `go2rtc`, `live_seconds`,
`snapshots_dir`, `low_battery`, `cameras:`) — see
`server/config/server.example.yaml`. The HA token lives in a root-only env
file (`HA_TOKEN=...`), never in the repo.

**Use a non-admin HA user** (done 2026-10-16). Antigua's token travels to HA
in plain HTTP over the LAN, so it should only be able to do what the skill
needs: read states, `lock.lock`, `eufy_security.start/stop_p2p_livestream`,
and `input_button.press`. Two things change for a non-admin user, and the
code handles both:

- **Push updates** use the WebSocket `subscribe_events` for `state_changed`,
  filtered to the configured entities. HA refuses `subscribe_trigger` to
  non-admin users, but `state_changed` is on their allowlist and respects
  their entity permissions.
- **The Eufy bridge restart** after a charge can't call `hassio.addon_restart`,
  which is admin-only. Set `eufy_restart_button` to an `input_button`. An HA
  automation watching it does the restart in HA's own context (here:
  `input_button.antigua_restart_eufy_bridge`, about 15 s after the press;
  presses within 5 minutes coalesce). Without the button, an admin token
  restarts the add-on directly via `eufy_addon`.

`lock.front_door` is currently disabled in HA by the Eufy integration, so
"lock the door" answers from `sensor.front_door_lock_status`. That isn't
caused by the non-admin user.

---

## Code Location

| What | Where |
|---|---|
| Detection | `parse_security_request()` in `server/antigua_core/intents/security.py` |
| Routing | `_handle_security()` in `server/antigua_core/pipeline.py` |
| Home Assistant client, event log, poller | `server/antigua_core/security.py` |
| Wiring (primary only) | `server/antigua_server.py` (the fallback server has none) |
| Tests | `tests/test_security.py` |

---

## Limitations

- Primary-only: the backup doesn't run the skill, and everything stops with
  "Home Assistant unreachable" if HA is down.
- A livestream closes when the display card is dismissed, replaced or times
  out (`security.live_seconds`, 60 s), so a battery camera is never left
  streaming.
- Lock history and activity come from the local SQLite log
  (`data/security_events.db`), not HA's recorder — events older than the poll
  interval or the log itself are gone. The log keeps 30 days
  (`EventLog.KEEP_S`), pruned at startup and daily. The poller also refreshes camera
  snapshots for the display's traffic-cam card on its own slow schedule.