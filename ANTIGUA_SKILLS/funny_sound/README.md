# Skill: Funny Sound

**Status:** Active
**Pipeline stage:** LLM bypass (route `funny_sound`, after `tv`, before `podcast` and `music`)

---

## What It Does

Plays one random clip from the top 50 of myinstants' US "sound effects"
category (vine boom, airhorn, bruh...), at half Antigua's speaking level.
Nothing is said and nothing is synthesized: the clip is the reply.

---

## How Users Trigger It

"Play a funny sound", "Play me a funny sound effect", "Play another funny
sound", "Make a funny noise", "Play a random sound".

---

## Setup

The clips are downloaded, not tracked (they aren't ours to publish):

```bash
uv run --no-project --with curl_cffi python server/scripts/fetch_funny_sounds.py
```

It writes `server/static_audio/funny_*.wav`, replacing the old set. Flags:
`--count` (50), `--volume` (50, percent of Antigua's voice level) and
`--max-seconds` (10). Each clip is loudness-normalized to the cues' level
before the volume cut, so a quiet "huh" and a clipped airhorn come out
equally loud. `deploy_fallback.sh` rsyncs `static_audio/` to the backup.
Run the script again to pick up a new top 50.

---

## Code Location

| What | Where |
|---|---|
| Detection | `is_funny_sound_request()` in `server/antigua_core/intents/funny_sound.py` |
| Routing | `_handle_funny_sound()` in `server/antigua_core/pipeline.py` |
| Download + mixing | `server/scripts/fetch_funny_sounds.py` |

---

## Limitations

- The volume is baked into the files, so changing it means re-running the
  script. It's relative to Antigua's voice, which `antigua_volume_pct` sets.
- myinstants is behind a Cloudflare challenge; the script gets through by
  impersonating Chrome (curl_cffi) and will break if that stops working.
- Clips are cut at 10 seconds.
