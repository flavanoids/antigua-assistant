# Skill: Volume Control

**Status:** Active  
**Pipeline stage:** LLM bypass (route `volume`, after `tv` and `music`)

---

## What It Does

A bare volume request ("turn it up", "too loud") adjusts **whatever is
playing**, never Antigua's own voice or the Pi's speaker sink:

1. **Music is playing** (any Music Assistant player, ducked counts) → the
   music player's volume steps up/down, silently, the same as "turn the
   music up" (`music_volume_up/down`).
2. **Otherwise** → the Living Room TV via the Apple TV MCP (`volume_up` /
   `volume_down`), which reaches the soundbar over HDMI-CEC
   (`tv_volume_up/down`, "Turning up the Living Room TV volume").

If neither backend is available: "I can't control the TV right now."
Antigua's speaking level is set only by `antigua_volume_pct` in
`satellite.yaml`.

---

## How Users Trigger It

"Turn it up/down", "Louder", "Quieter" / "Softer", "Speak up", "Too loud",
"Too quiet", "Volume up/down". Explicit targets go straight to their own
route: "tv volume down" → `tv`, "turn the music up" → `music`.

---

## Code Location

| What | Where |
|---|---|
| Detection | `parse_volume_request()` in `server/antigua_core/classify.py` |
| Routing | `_handle_volume()` in `server/antigua_core/pipeline.py` |
| "Is music playing?" | `MusicControl.playing()` in `server/antigua_core/music.py` |
| TV volume | `HomeControl.tv("tv_volume_up"/"tv_volume_down")` |

---

## Limitations

- Relative steps only; "set the music volume to 30" works via the music
  route, but there is no absolute TV level.
- The music check costs one short Music Assistant call per configured
  player (3s timeout each) when nothing Antigua started is playing.
