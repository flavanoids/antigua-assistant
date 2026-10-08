""""Play a funny sound" — a random meme clip from server/static_audio/."""

import re


_FUNNY_SOUND_RE = re.compile(
    r"\b(play|make|do)( me| us)? (a |an |another |some )?"
    r"(random |silly )?(funny|silly|random) (sound|noise)s?( effects?)?\b",
    re.IGNORECASE,
)


def is_funny_sound_request(transcript: str) -> bool:
    return bool(_FUNNY_SOUND_RE.search(transcript))
