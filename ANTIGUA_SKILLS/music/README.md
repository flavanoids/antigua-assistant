# Skill: Music (Apple Music)

**Status:** Built and unit-tested. Live once Music Assistant is set up (see Setup).
**Pipeline stage:** Pre-LLM (route `music`, before `volume`)
**LLM involved:** No. Parsing, search resolution and replies are all deterministic.

---

## What It Does

Alexa-style music. Antigua parses the request (`antigua_core/music_intents.py`)
and resolves it against Apple Music through
[Music Assistant](https://www.music-assistant.io) (`antigua_core/music.py`).
Music Assistant then streams it to an AirPlay speaker.

| She says | What happens |
|---|---|
| "Play Beyoncé" | Apple's **Beyoncé Essentials** playlist; if there is none, the artist's top tracks (radio) |
| "Play the album Lemonade" / "Play Lemonade album by Beyoncé" | The whole album, in order |
| "Play Halo" / "Play Halo by Beyoncé" / "Play the song Halo" | That song, then similar songs (radio, like Alexa) |
| "Play Beyoncé's newest album" / "…first album" | Newest/oldest *full-length* album: singles, EPs, live and compilations are skipped |
| "Play Bad Bunny songs from 2021" / "…from the 90s" | Every release in that year or decade, oldest first |
| "Play the song that goes *I'm in love with the shape of you*" | Lyrics search (see below) |
| "Play some jazz" / "Play chill music" | Best matching playlist, shuffled |
| "Play Adele **in the bedroom**" / "…on the soundbar" / "…on the TV" | Same, on that speaker |
| "What are the older albums by Toro y Moi?" | Reads out three with years ("…Underneath the Pine, from 2011…") |
| …then "Play Underneath the Pine" / "Play the second one" / "Play it" | Plays one from the list just read out (remembered for 10 minutes) |
| "What's Beyoncé's latest album?" / "What albums does Toro y Moi have?" | Answers by voice; the answer can be followed with "play it" |

### Playback controls

These act on the speaker Antigua last played on (or the one named, or
whatever is playing).

| She says | Action | Reply |
|---|---|---|
| "Pause" / "Stop the music" | pause | *silent* |
| "Resume" / "Resume the music" / "Play music" | resume | *silent* |
| "Next song" / "Skip" | next | *silent* |
| "Previous song" / "Go back a song" | previous | *silent* |
| "Restart the song" / "Play it again" / "From the top" | seek to 0 | *silent* |
| "Replay the album" / "Start the album over" | play queue index 0 | *silent* |
| "Repeat this song" / "Repeat the album" / "Turn off repeat" | repeat one/all/off | "Repeating this song." |
| "Shuffle" / "Turn off shuffle" | shuffle on/off | "Shuffling." |
| "What song is this?" / "Who sings this?" | now playing | "This is Halo by Beyoncé, from I Am... Sasha Fierce." |
| "Turn the music up/down" / "Set the music volume to 30" | player volume | silent / "Music volume 30." |
| "Move the music to the soundbar" / "Play this in the bedroom" | transfer queue | "Moving the music to the soundbar." |

### Navigation, queueing and your library

| She says | What happens |
|---|---|
| "Play Halo next" / "After this, play Jolene" | Queued right after the current song ("Halo by Beyoncé is up next.") |
| "Add Rumours to the queue" / "Queue up Halo" | Added to the end of the queue |
| "Skip ahead 30 seconds" / "Fast forward a minute" / "Rewind" / "Go back 10 seconds" | Seek within the song (bare: +30s / −15s), *silent* |
| "Go to 1:30" / "Jump to the 2 minute mark" | Seek to that point, *silent* |
| "Skip two songs" / "Go back two songs" / "Skip to track 3" | Move within the queue, *silent* |
| "What's next?" / "What's coming up?" | "Next is Stand by Me by Ben E. King." |
| "How long is this song?" / "How much is left in this song?" | Length and time left |
| "Play more like this" / "Play something similar" | After this song: top songs by Apple's similar artists, plus two more by this one, shuffled |
| "Play more by this artist" | After this song: their Essentials (or top songs) |
| "Play the whole album" / "Play the album this song is from" | That album from the start |
| "Play the rest of the album" | After this song: the album's remaining tracks |
| "Stop the music in 30 minutes" / "Set a sleep timer for 20 minutes" | Pauses then; "cancel the sleep timer" undoes it |
| "Stop after this song" / "Stop after this album" | Pauses when it ends (not for endless radio queues) |
| "I like this song" / "Add this to my library" | Added to Music Assistant favorites |
| "I don't like this song" / "Never play this again" | Skips it, and it's scored down in future searches |
| "Play my favorites" / "Play my library" | Your favorited tracks / your library, shuffled |
| "Play my Cocktail Hour playlist" / "Play the playlist Car" | Your playlist by name (Apple's catalogue if you have none by that name) |

### Multi-room

| She says | What happens |
|---|---|
| "Play Adele everywhere" / "Play some jazz in every room" | Groups every `everywhere` speaker under the default one, then plays ("Playing … everywhere.") |
| "Play this everywhere" / "Play the music on all the speakers" | Groups them under whatever's playing |
| "Also play it in the bedroom" / "Add the soundbar" | Adds that speaker to the group |
| "Stop the music in the bedroom" / "Take the bedroom out" / "Pause the bedroom" | Drops that speaker from the group (pauses it if it's the only one) |
| "Just the kitchen" / "Only in the kitchen" | Ungroups everything else |

Volume commands on a group change the whole group. `music.everywhere`
lists which players "everywhere" means (default: every player in
`speakers`, including the Apple TV, which only plays with the TV on).

### Anything else

When no pattern matches but the words sound like music ("throw on some
Ne-Yo", "I'm in the mood for 90s R&B", "this song sucks"), the local model
restates the request as one of the commands above (`music_router.py`,
~0.6s) and it runs as if said that way. Questions about music ("who sings
Halo", "what's the best Beyoncé album") stay with the chat model.
`music.llm_fallback: false` turns this off.

"More like this", "more by this artist" and the album requests answer at
once and build the queue in the background (up to ~5s), since the current
song keeps playing. "How much time is left" without "song" stays with the
timers skill.

Transport controls stay silent on success, like Alexa: the music doing the
thing is the confirmation. Bare "turn it up" goes to the `volume` route,
which steps the music volume when music is playing and the TV otherwise.

**Ducking:** while the user is talking to Antigua, music on the kitchen
speaker drops to 25% so commands don't have to be shouted over it. The
kitchen bridge publishes MQTT `antigua/listening` `{"active": true}` when it
starts recording (wake word, button, or follow-up speech) and
`{"active": false}` as soon as the reSpeaker's VAD hears the user stop, and
the music comes straight back to its previous level. Antigua's reply plays
over the restored music. If the "stopped" message is lost, it restores after
25s. A music volume set during that turn is kept rather than overwritten.

---

## How "Play X" is resolved

`music.py` `_resolve()` scores one pool of candidates rather than trying
artist, then song, then album in a fixed order.

1. **Readings.** Every " by " is a possible split and the whole phrase may be
   a title, so "Stand by Me by Ben E. King" is tried as *Stand* by *Me by Ben
   E. King*, *Stand by Me* by *Ben E. King*, and as a title.
2. **Candidates.** Music Assistant's Apple Music search runs for each reading
   (the title with and without the artist) across artists, songs, albums and
   playlists, in parallel.
3. **Score.** Each candidate gets:
   - **name fit**: spelling or sound-alike, whichever is closer, ignoring a
     leading "The" ("beyond say" ~ Beyoncé; "killers" ~ The Killers). Saying
     "the" and matching it word for word adds a little.
   - **artist fit** when an artist was named: soft, so a misheard artist
     ("So Sick by Nia") doesn't lock out the right song.
   - **Apple's order** within each type, steeply weighted for songs: it knows
     Dolly Parton's "Jolene" from Beyoncé's.
   - **song popularity**: where Apple's public iTunes search (US store,
     `music.store_country`) ranks that exact song for the title.
   - **artist popularity**: Deezer fan counts, strongest for artist
     candidates (Queen the band vs a song called "QUEEN"). Cached in
     `data/music_artist_pop.json` for 30 days.
   - **penalties** for covers, karaoke, soundtrack re-recordings, Apple's
     "Sing" playlists, singles/EPs standing in for a song, and live/remix
     versions, unless the request asked for one.
   - **type hints**: "the song X", "the album X", "the artist X" favor that
     type; a bare title leans to songs and artists; a genre or mood ("some
     jazz", "chill music", "90s hip hop", "something relaxing") favors
     Apple's own playlists.
4. **Play the best.** An album from the list Antigua just read out still wins
   outright. With no candidate, four or more words are tried as lyrics.

### Corrections

For 3 minutes after Antigua starts something by name, a correction replaces
it. The pipeline checks these before normal routing, and only then, so the
same words mean nothing to music at other times.

| She says | Plays |
|---|---|
| "No, the Adele one" / "the one by Lionel Richie" / "Adele's version" / "no, by Adele" | That artist's version, from the same search or a new one |
| "The other one" / "wrong song" / "not that one" / "that's not it" | The next version of the same title, else the next best match |
| "No, the original" | The same title without cover, karaoke, live or remix markers (Apple gives no release years, so "original" can't mean "earliest") |
| "No, I meant the album" / "…the song" / "…the artist" | The best match of that type |

A correction never goes back to a version already rejected for the same
request. Corrections are remembered in `data/music_prefs.json`, keyed by
the request: after "play hello" → "no, the one by Adele", "play hello"
plays Adele's. A named pick ("the Adele one", "the original", "the
album") is remembered as wanted. "The other one" only marks the rejected
version as unwanted.

### Benchmark

`venv/bin/python tests/music_bench.py` scores the resolver against the live
catalogue with playback recorded, not sent. Responses are cached in `data/`,
so a change is compared on identical search results, and each run is diffed
against the previous one. Cases live in `tests/music_bench/cases.yaml`; real
requests from the logs can go in the git-ignored `data/music_bench_local.yaml`.

| 2026-09-30 | Before | After |
|---|---|---|
| Tuned cases (139) | 101 (72.7%) | 130 (93.5%) |
| Held-out cases (40) | 34 (85.0%) | 39 (97.5%) |
| Corrections (13) | 0 | 13 |

---

## Lyrics search

Apple Music has no lyric search, so `_track_by_lyrics` asks the local SearXNG
for `"<line>" lyrics`. Lyric sites title their pages "Artist – Title Lyrics".
The candidate pairs are voted on, tried in both orders, then matched in the
Apple Music catalogue. On 2026-09-25 it found all five test lines, including
the Spanish "yo perreo sola" (Bad Bunny).

---

## Speakers

| Say | Music Assistant player | Device |
|---|---|---|
| (default), "kitchen" | Kitchen Speaker | The satellite (shairport-sync) |
| "living room", "TV", "Apple TV" | Living Room | Apple TV 4K via AirPlay |
| "soundbar", "JBL" | Soundbar | JBL Bar 500 (AirPlay/Cast) |
| "bedroom", "HomePod" | Bedroom (2) | HomePod mini |

Edit `music.speakers` / `music.labels` in `server.yaml`.

---

## Setup

1. Music Assistant runs from `server/music/docker-compose.yml` on the primary
   (image pinned at 2.10.4; data directory set by `MA_DATA_DIR` in `server/music/.env`).
2. Open `http://<primary>:8095`. Create the admin account, then
   Settings → Music providers → **Apple Music** → sign in.
3. Profile → create a **long-lived token**. Put
   `MUSIC_ASSISTANT_TOKEN=…` in `server/config/mcp.env`.
4. Restart `antigua-server`. Run `deploy_fallback.sh` so the backup gets the
   token too.

Apple Music's token lasts 180 days, after which Music Assistant needs a
re-login.

---

## Limitations

- **Apple Music via Music Assistant is unofficial.** It's AAC 256 (no
  lossless), and there can be up to about 5s of gap between tracks.
- **Transcription:** speech-to-text is English-only. Spanish titles and
  artist names may come through wrong. The fuzzy matching absorbs some of
  this; the lyrics path helps for songs.
- **Ducking is kitchen-only and primary-only.** The fallback server doesn't
  subscribe to the MQTT cues.
- **Failover:** Music Assistant runs only on the primary box. The backup can use it
  if just antigua-server is down, but not if the whole box is.
- **Apple TV as a speaker** plays through the TV, which must be on. AirPlay
  to the Apple TV may need its AirPlay setting to allow everyone on the
  network.
