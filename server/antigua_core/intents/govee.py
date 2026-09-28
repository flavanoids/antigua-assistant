"""Govee light commands."""

import re


# Spoken-name → device alias(es). server.yaml's govee.devices maps each alias
# to a Govee device ID for the govee MCP server. Order matters: specific patterns
# (left pink, hallway one) must come before the group patterns that also match.
_GOVEE_CHANDELIER = [f"chandelier-{i}" for i in range(1, 7)]
_GOVEE_ALL_DEVICES = [
    "tv-bar", "rope-neon", "pink-left", "pink-right", "hallway-1", "hallway-2",
    *_GOVEE_CHANDELIER, "monitor-strip",
]
_GOVEE_DEVICE_PATTERNS = [
    # H600B bulbs are named inconsistently in the Govee app (dupes + a
    # "Chadelier" typo), so voice only addresses the chandelier as one group.
    (re.compile(r"\b(?:kitchen\s+)?chandeliers?(?:\s+lights?)?\b", re.IGNORECASE),
     _GOVEE_CHANDELIER, "the kitchen chandelier"),
    (re.compile(r"\bmonitor\s+(?:light\s+)?strip\b|\bmonitor\s+lights?\b", re.IGNORECASE),
     ["monitor-strip"], "the monitor strip"),
    (re.compile(r"\b(?:tv|television)\s+(?:light\s*)?bar\b|\btv\s+lights?\b", re.IGNORECASE),
     ["tv-bar"], "the TV bar"),
    (re.compile(r"\b(?:rope|neon)(?:\s+neon)?\s+lights?\b", re.IGNORECASE),
     ["rope-neon"], "the rope light"),
    (re.compile(r"\bleft\s+pink\s+lights?\b|\bpink\s+left\b", re.IGNORECASE),
     ["pink-left"], "the left pink light"),
    (re.compile(r"\bright\s+pink\s+lights?\b|\bpink\s+right\b", re.IGNORECASE),
     ["pink-right"], "the right pink light"),
    (re.compile(r"\bpink\s+lights?\b", re.IGNORECASE),
     ["pink-left", "pink-right"], "the pink lights"),
    (re.compile(r"\bhallway\s+lights?\s+(?:one|1)\b", re.IGNORECASE),
     ["hallway-1"], "hallway light one"),
    (re.compile(r"\bhallway\s+lights?\s+(?:two|2)\b", re.IGNORECASE),
     ["hallway-2"], "hallway light two"),
    (re.compile(r"\bhallway\s+lights?\b", re.IGNORECASE),
     ["hallway-1", "hallway-2"], "the hallway lights"),
    (re.compile(r"\b(?:all\s+(?:the\s+)?|every\s+)lights?\b|\bthe\s+lights\b", re.IGNORECASE),
     _GOVEE_ALL_DEVICES, "the lights"),
]

# White shades map to real color temperature (CCT), not RGB — the bulbs
# render CCT white far better. Multi-word names first so "warm white" wins
# over "white". Chandelier/monitor strip accept 2700-6500K, the rest 2000-9000K.
_GOVEE_WHITE_TEMPS = [
    ("soft white", 2700),
    ("warm white", 3000),
    ("neutral white", 4500),
    ("cool white", 6500),
    ("daylight", 5700),
    ("candlelight", 2700),
    ("white", 5000),
]
_GOVEE_KELVIN_RE = re.compile(r"\b(\d{4})\s*(?:k|kelvin)\b", re.IGNORECASE)

_GOVEE_COLORS = [
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
    ("purple", (128, 0, 128)),
    ("violet", (148, 0, 211)),
    ("pink", (255, 105, 180)),
    ("orange", (255, 120, 0)),
    ("yellow", (255, 200, 0)),
    ("cyan", (0, 255, 255)),
    ("teal", (0, 128, 128)),
    ("magenta", (255, 0, 255)),
]

_GOVEE_BRIGHTNESS_RE = re.compile(
    r"\b(?:brightness\s+(?:to\s+)?)?(\d{1,3})\s*(?:percent|%)\b|\bbrightness\s+(?:to\s+)?(\d{1,3})\b",
    re.IGNORECASE,
)
_GOVEE_QUESTION_RE = re.compile(
    r"^\s*(?:are|is|was|were|what|which|why|when|how|do|does|did|can|could|should)\b",
    re.IGNORECASE,
)

# On/off needs a command verb, or a bare "hallway lights off". Without this,
# any sentence that happened to contain "the lights on" flipped them — on
# 2026-09-23 Antigua's own "I've got the door open and the lights on", heard
# back by the kitchen mic, turned on every light in the house.
_GOVEE_POWER_VERB_RE = re.compile(r"\b(?:turn|switch|shut|power|kill|flip|put)\b", re.IGNORECASE)
_GOVEE_BARE_POWER_MAX_WORDS = 4


def parse_govee_request(transcript: str):
    """Return (action, device_aliases, spoken_name, param) or None.

    action: "power" (param bool), "color" (param (name, rgb)),
    "brightness" (param int 0-100), or "temperature" (param (label, kelvin)).
    """
    if _GOVEE_QUESTION_RE.search(transcript):
        return None  # status questions go to the LLM, not the lights
    for dev_re, aliases, spoken in _GOVEE_DEVICE_PATTERNS:
        m = dev_re.search(transcript)
        if not m:
            continue
        # Look for the command in the text minus the device name, so the
        # "pink" in "pink lights" can't be read as a color.
        rest = (transcript[: m.start()] + " " + transcript[m.end():]).lower()
        bm = _GOVEE_BRIGHTNESS_RE.search(rest)
        if bm:
            level = int(bm.group(1) or bm.group(2))
            if 0 <= level <= 100:
                return ("brightness", aliases, spoken, level)
        km = _GOVEE_KELVIN_RE.search(rest.replace(",", ""))
        if km:
            kelvin = max(2000, min(9000, int(km.group(1))))
            return ("temperature", aliases, spoken, (f"{kelvin} kelvin", kelvin))
        if re.search(r"\bdim\b", rest):
            return ("brightness", aliases, spoken, 20)
        for tname, kelvin in _GOVEE_WHITE_TEMPS:
            if re.search(r"\b" + tname.replace(" ", r"\s+") + r"\b", rest):
                return ("temperature", aliases, spoken, (tname, kelvin))
        for cname, rgb in _GOVEE_COLORS:
            if re.search(r"\b" + cname.replace(" ", r"\s+") + r"\b", rest):
                return ("color", aliases, spoken, (cname, rgb))
        if not (_GOVEE_POWER_VERB_RE.search(transcript)
                or len(transcript.split()) <= _GOVEE_BARE_POWER_MAX_WORDS):
            return None  # mentions the lights but isn't telling them to do anything
        if re.search(r"\boff\b", rest):
            return ("power", aliases, spoken, False)
        if re.search(r"\bon\b", rest):
            return ("power", aliases, spoken, True)
        return None  # device named but no recognizable command
    return None
