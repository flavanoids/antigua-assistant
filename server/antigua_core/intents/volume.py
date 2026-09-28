"""Volume up/down requests."""

import re


_VOLUME_RE = re.compile(
    r"\b(turn (it |the )?(up|down|louder|quieter|softer)|"
    r"(volume|sound) (up|down)|"
    r"louder|quieter|softer|speak up|speak louder|too loud|too quiet)\b",
    re.IGNORECASE,
)


def parse_volume_request(transcript: str):
    if not _VOLUME_RE.search(transcript):
        return None
    low = transcript.lower()
    if any(w in low for w in ["up", "louder", "speak up", "speak louder", "too quiet"]):
        return "volume_up"
    return "volume_down"
