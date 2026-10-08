"""LLM fallback for music requests the patterns don't know, shared by both servers.

music_intents is a whitelist of phrasings; people also say "throw on some
Ne-Yo", "I'm in the mood for 90s R&B" and "can we get something upbeat for
cooking". When classify() found nothing and the words sound like music, ask
the model to restate the request as one canonical command that parse_music()
already understands. The model never picks what plays: the resolver does.
"""

import logging
import re

import requests

from .music_intents import _GENRE_WORDS

log = logging.getLogger("antigua_core")

# Words that make a request worth ~0.5s of the model's time. Anything else
# goes straight to the chat LLM as before.
MUSIC_CUE = re.compile(
    r"\b(?:music|song|songs|tune|tunes|track|tracks|album|albums|playlist|artist|band|singer|rapper|"
    r"play|playing|played|listen|hear|put\s+(?:\S+\s+){0,4}on|throw\s+(?:\S+\s+){0,4}on|spin|blast|bump|"
    r"crank|jam|jams|beats|queue|skip|rewind|volume|louder|quieter|vibes?|mood\s+for|radio|station|dj|mix|"
    r"banger|bangers|(?:give\s+me|gimme|drop|let'?s\s+do|do|get|hit\s+me\s+with)\s+some|something\s+(?:to\s+)?(?:\w+\s+)?(?:for|while|to)\b)\b",
    re.IGNORECASE)


def looks_like_music(transcript: str) -> bool:
    return bool(MUSIC_CUE.search(transcript) or _GENRE_WORDS.search(transcript))

_PROMPT = """You turn a voice request into ONE command for a home music player.
Reply with one line only: the command, or NONE.

Commands:
play <artist>                      play Ne-Yo
play <song> by <artist>            play So Sick by Ne-Yo
play <song>                        play Bohemian Rhapsody
play the album <album>             play the album Rumours
play some <style, mood or era>     play some 90s r&b   /   play some upbeat cooking music
play my favorites                  play my <name> playlist
play <thing> next                  add <thing> to the queue
pause | resume | next song | previous song | restart the song | play more like this | play more by this artist
skip ahead <n> seconds | rewind | what's next | turn the music up | turn the music down
stop the music in <n> minutes | I like this song | I don't like this song

Rules:
- Copy every title and name word for word, including words like "songs", "the" or "of". Never shorten, \
correct or add a song, artist or album the user didn't name.
- A style, mood, activity or decade is "play some ...".
- Questions about music (who sings it, what an album is, trivia, opinions) and anything not about \
playing or controlling music: NONE.

Examples:
throw on some drake -> play drake
can we get something upbeat for cooking -> play some upbeat cooking music
i'm in the mood for some old school hip hop -> play some old school hip hop
put that halo song by beyonce on -> play halo by beyonce
could you do the album the dark side of the moon by pink floyd -> play the album the dark side of the moon by pink floyd
crank it -> turn the music up
ok we're done with music for tonight -> pause
this song is terrible -> I don't like this song
who sings halo -> NONE
what's your favorite band -> NONE

Request: {q}
->"""


def llm_route_music(transcript: str, *, ollama_host, model, timeout,
                    keep_alive="15m", num_ctx=8192, enabled=True) -> str | None:
    """A canonical music command for transcript, or None (not music / failed)."""
    if not enabled or not looks_like_music(transcript):
        return None
    try:
        resp = requests.post(
            f"{ollama_host}/api/generate",
            json={
                "model": model,
                "prompt": _PROMPT.format(q=transcript.strip()),
                "stream": False,
                "think": False,
                # Must match the chat calls, or Ollama reloads the model.
                "options": {"temperature": 0, "num_predict": 24, "num_ctx": num_ctx},
                "keep_alive": keep_alive,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        line = ((resp.json().get("response") or "").strip().splitlines() or [""])[0]
    except Exception as e:
        log.warning("Music router failed: %s", e)
        return None
    line = line.strip().strip("\"'`").removeprefix("->").strip()
    if not line or line.upper().startswith("NONE"):
        return None
    return line
