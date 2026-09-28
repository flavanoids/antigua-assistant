"""Transcript cleanup and quality: STT fixes, wake-word echoes, garbage, display commands."""

import logging
import re

from .. import household, settings
from ..music_intents import parse_music
from .timers import parse_snooze_request
from .volume import parse_volume_request

log = logging.getLogger("antigua_core")


# Common STT mishearings to correct before regex and LLM see the transcript
_STT_FIXES = [
    # "timer" mishearings
    (re.compile(r"\b(tired|time\s+it|timed|timmer|ty-mer)\b", re.IGNORECASE), "timer"),
    # "set a" mishearings that land before "timer"
    (
        re.compile(r"\b(seven|set\s+it|set\s+a\s+it)\s+timer\b", re.IGNORECASE),
        "set a timer",
    ),
    # "set a timer" full reconstructions
    (re.compile(r"\bset\s+it\s+timer\b", re.IGNORECASE), "set a timer"),
    (re.compile(r"\bset\s+it\s+tired\b", re.IGNORECASE), "set a timer"),
    # Household name mishearings (household.misheard_as) are applied in
    # correct_transcript().
    # News source mishearings
    (re.compile(r"\b(rooters|routers|rioters|writers)\b", re.IGNORECASE), "reuters"),
    (
        re.compile(r"\bal[- ]?(ja[sz]eera|jazira|jazira|jezeera)\b", re.IGNORECASE),
        "al jazeera",
    ),
    # Years spoken as "twenty twenty-six" come back hyphenated: "the 20-26 FIFA
    # World Cup". The hyphen survives into the search query and ranks nothing.
    (re.compile(r"\b(19|20)-(\d{2})\b"), r"\1\2"),
    # "Who won ..." is heard as "We won ...". A question is far likelier than
    # the user reporting their own victory to a voice assistant, and only when
    # it opens the sentence and is followed by an article.
    (re.compile(r"^\s*we\s+won\s+(the|a)\b", re.IGNORECASE), r"who won \1"),
]


def correct_transcript(text: str) -> str:
    for pattern, replacement in _STT_FIXES:
        text = pattern.sub(replacement, text)
    return household.fix_mishearings(text)


_SHOW_ME_RE = re.compile(r"^\s*show\s+(?:me\s+)?(.+)", re.IGNORECASE)


def detect_display_command(transcript: str) -> dict | None:
    """Return a display command config dict if the transcript requests one."""
    m = _SHOW_ME_RE.match(transcript)
    if not m:
        return None
    query = m.group(1).lower().strip().rstrip(".,!?")
    for cmd in settings.DISPLAY_COMMANDS:
        if any(kw in query for kw in cmd.get("keywords", [])):
            return cmd
    return None


def strip_wake_prefix(text: str) -> str:
    """Remove leading wake word tokens that the user repeated before their question."""
    if settings.WAKE_PREFIX_RE is None:
        return text
    stripped = settings.WAKE_PREFIX_RE.sub("", text).strip()
    if stripped != text.strip():
        log.info("Stripped wake prefix: %r → %r", text.strip(), stripped)
    return stripped


_WHISPER_GARBAGE = re.compile(
    r"^\s*\[[\w\s]+\]\s*$",  # e.g. [Music], [Applause], [inaudible]
    re.IGNORECASE,
)
_DIDNT_CATCH = [
    "Hmm, I didn't quite catch that. Could you say it again?",
    "Sorry, I missed that. Want to try once more?",
    "I didn't hear that clearly. Could you repeat it?",
]


def is_garbage_transcript(text: str) -> bool:
    stripped = text.strip()
    if _WHISPER_GARBAGE.match(stripped):
        return True
    words = re.sub(r"[^\w\s]", "", stripped).split()
    if len(words) > 1:
        return False
    # One-word skill triggers ("louder", "snooze") are commands, not noise
    if words and (parse_volume_request(stripped) or parse_snooze_request(stripped)
                  or parse_music(stripped)):
        return False
    return True
