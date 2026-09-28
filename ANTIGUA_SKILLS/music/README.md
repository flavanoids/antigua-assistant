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

## Resolution order for a bare "Play X"

1. An album from the list Antigua just read out
2. An artist whose name matches (≥ 0.9 similarity) → Essentials
3. A song title match → song + radio
4. An album title match → album
5. A playlist (for 1–3 word queries: genres and moods) → shuffled
6. Four or more words: try it as lyrics
7. The top song result
8. "I couldn't find X on Apple Music."

"Play Stand by Me" first parses as "Stand" by "Me"; when that finds nothing,
the whole phrase is retried as a title.

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
