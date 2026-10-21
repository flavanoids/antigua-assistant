# Skill: News Podcasts

**Status:** Active
**Pipeline stage:** LLM bypass (route `podcast`, after `funny_sound`, before
`music` — so "play Apple News today" isn't an Apple Music search)

---

## What It Does

"Play Apple News Today" / "play Up First" plays that day's episode on the
music speakers through Music Assistant. The episode is picked in code from
the show's feed or its Apple Podcasts page; the LLM is never involved and
the reply is a template.

Which episode plays:

- today's episode is out → play it
- a news day, not out yet (shows drop around 4 am) → the latest one, no
  question
- not a news day (Apple News Today skips weekends; Up First's Sunday episode
  is the long-form Sunday Story), or a named day with no episode → she
  offers the latest one before it ("Want me to play Friday's?"); saying the
  wake word and "yes" within a minute plays it

---

## How Users Trigger It

"Play Apple News Today" / "Play the Apple News podcast" / "Play Up First" /
"Stop Apple News" / "Pause the podcast" / "Resume the podcast".

---

## Setup

Shows come from the `podcasts:` block in `server.yaml` (a default set if
absent). Each show is either an RSS feed (`feed:`), like NPR's Up First, or
an Apple-exclusive show with no feed (`apple_id:`), like Apple News Today —
its Apple Podcasts page embeds the recent episodes as JSON, each with a
plain MP3. `url_pattern` and `max_minutes` keep only the daily news
episodes: Apple News Today's feed also carries interviews and a trailer,
and Up First's carries the half-hour Sunday Story and bonus interviews.
Needs Music Assistant reachable to play.

---

## Code Location

| What | Where |
|---|---|
| Detection + episode choice | `parse_podcast()` in `server/antigua_core/podcast.py` |
| Routing | `_handle_podcast()` in `server/antigua_core/pipeline.py` |
| Playback | `MusicControl.play_url` (Music Assistant) |
| Tests | `tests/test_podcast.py` |

---

## Limitations

- Music Assistant needs to be up and the speakers reachable; the fallback
  server doesn't run Music Assistant.
- Apple's podcast page scraping breaks if Apple changes the page's embedded
  JSON.
- Only the shows listed in config; asking for an unlisted podcast falls
  through to the music route (Apple Music) or the LLM.