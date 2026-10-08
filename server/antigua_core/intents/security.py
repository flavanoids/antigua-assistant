"""Home security: the Eufy cameras and the front door lock, through Home
Assistant (antigua_core.security).

Pure regex, no network. There is deliberately no unlock action: a mishearing
or the TV saying the wake word must never open the door, so "unlock" only
gets an explanation.
"""

import re

# Which camera. "door bell" and "porch" are the front door; "driveway" is what
# the garage camera looks at.
_FRONT = r"(?:front\s+door|front\s+porch|porch|door\s*bell|front)"
_GARAGE = r"(?:garage|driveway)"
_CAM = r"(?:\s+(?:cam|camera|cams|cameras|feed|video))?"

_SHOW_VERB = (r"(?:show|let\s+me\s+see|pull\s+up|bring\s+up|put\s+up|display"
              r"|check|look\s+at|can\s+i\s+see|view)")
_SHOW_RE = re.compile(
    rf"\b{_SHOW_VERB}\b(?:\s+(?:me|us))?(?:\s+(?:the|a))?(?:\s+live)?\s+"
    rf"(?P<cam>{_FRONT}|{_GARAGE}){_CAM}\b(?!\s+(?:door|light|lights|opener)\b)",
    re.IGNORECASE,
)
# "Who's at the door?" — answered by looking.
_WHO_AT_DOOR_RE = re.compile(
    r"\bwho(?:'s|\s+is)\s+(?:at|outside)\s+(?:the\s+)?(?:front\s+)?door\b", re.IGNORECASE
)

_DOOR = r"(?:the\s+)?(?:front\s+)?door"
_UNLOCK_RE = re.compile(r"\bunlock\b.*\bdoor\b|\bunlock\s+(?:it|the\s+lock)\b", re.IGNORECASE)
_LOCK_RE = re.compile(
    rf"\block\s+{_DOOR}\b|\block\s+(?:it|up)\b(?=.*\bdoor\b)|\block\s+up\s*[.!?]*$",
    re.IGNORECASE,
)
_LOCK_STATE_RE = re.compile(
    rf"\b(?:is|did\s+(?:i|we|you|someone|anyone)\s+(?:lock|leave))\s+{_DOOR}\s*(?:locked|unlocked|open)?\b"
    rf"|\bis\s+{_DOOR}\s+(?:locked|unlocked)\b"
    rf"|\bdid\s+(?:i|we|you|someone|anyone)\s+lock\s+{_DOOR}\b",
    re.IGNORECASE,
)
_LOCK_WHEN_RE = re.compile(
    r"\bwhen\b.*\b(?:lock|locked|unlock|unlocked)\b.*\bdoor\b"
    r"|\bwhen\b.*\bdoor\b.*\b(?:lock|locked|unlock|unlocked)\b"
    r"|\b(?:last|latest)\s+time\b.*\bdoor\b.*\b(?:lock|locked|unlock|unlocked)\b",
    re.IGNORECASE,
)
_UNLOCKED_WORD_RE = re.compile(r"\bunlock(?:ed)?\b|\bopened\b", re.IGNORECASE)

# "Who triggered the garage cam?" "When did someone ring the doorbell?"
_ACTIVITY_RE = re.compile(
    rf"\b(?:who|what)\b.*\b(?:trigger(?:ed)?|set\s+off|tripped|was\s+(?:at|in)|came\s+to)\b.*"
    rf"\b(?P<cam>{_FRONT}|{_GARAGE}){_CAM}\b"
    rf"|\bwhen\b.*\b(?:ring|rang|rung|motion|someone|somebody|anyone|anybody|movement)\b.*"
    rf"\b(?P<cam2>{_FRONT}|{_GARAGE}){_CAM}\b"
    rf"|\b(?:anyone|anybody|someone|somebody)\s+(?:at|by|in)\s+(?:the\s+)?(?P<cam3>{_FRONT}|{_GARAGE})\b"
    rf"(?=.*\b(?:today|earlier|while|recently|lately|last)\b)",
    re.IGNORECASE,
)
_BATTERY_RE = re.compile(
    rf"\bbattery\b.*\b(?P<what>{_FRONT}|{_GARAGE}|lock){_CAM}\b"
    rf"|\b(?P<what2>{_FRONT}|{_GARAGE}|lock){_CAM}(?:'s)?\s+battery\b",
    re.IGNORECASE,
)


def _camera(word: str) -> str:
    return "garage" if re.fullmatch(_GARAGE, word, re.IGNORECASE) else "front_door"


def parse_security_request(transcript: str):
    """(action, arg) for a camera or lock request, or None.

    Actions: show_camera (arg = "front_door" | "garage"), lock, unlock
    (refused by the handler), lock_state, lock_when (arg = "locked" |
    "unlocked"), activity (arg = camera), battery (arg = "front_door" |
    "garage" | "lock").
    """
    if not transcript:
        return None
    t = transcript.strip()

    if _UNLOCK_RE.search(t) and not _LOCK_WHEN_RE.search(t) and not _LOCK_STATE_RE.search(t):
        return "unlock", None
    if _LOCK_WHEN_RE.search(t):
        return "lock_when", "unlocked" if _UNLOCKED_WORD_RE.search(t) else "locked"
    if _LOCK_STATE_RE.search(t):
        return "lock_state", None
    if _LOCK_RE.search(t):
        return "lock", None

    m = _BATTERY_RE.search(t)
    if m:
        what = (m.group("what") or m.group("what2")).lower()
        return "battery", "lock" if what == "lock" else _camera(what)
    m = _ACTIVITY_RE.search(t)
    if m:
        return "activity", _camera(m.group("cam") or m.group("cam2") or m.group("cam3"))
    m = _SHOW_RE.search(t)
    if m:
        return "show_camera", _camera(m.group("cam"))
    if _WHO_AT_DOOR_RE.search(t):
        return "show_camera", "front_door"
    return None
