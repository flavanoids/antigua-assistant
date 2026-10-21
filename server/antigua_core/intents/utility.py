"""Small everyday requests answered in code: a stopwatch, spelling, coin
flips and dice, "repeat that", "what can you do", bare "mute", "volume to
5", and an honest "I can't do that yet" for actions no skill handles (the
model, left alone, says "Got it" and does nothing)."""

import re

# ── stopwatch ──

_STOPWATCH = r"(?:the\s+|a\s+|my\s+)?stop\s?watch"
_STOPWATCH_RE = [
    ("start", re.compile(rf"^(?:please\s+)?(?:start|begin|run|set)\s+{_STOPWATCH}\b", re.I)),
    ("stop", re.compile(rf"^(?:please\s+)?(?:stop|end|pause|cancel)\s+{_STOPWATCH}\b", re.I)),
    ("reset", re.compile(rf"^(?:please\s+)?(?:reset|restart|clear)\s+{_STOPWATCH}\b", re.I)),
    ("status", re.compile(
        rf"\b(?:how\s+long\s+(?:has|is)\s+{_STOPWATCH}|{_STOPWATCH}\s+(?:time|status)|"
        rf"what(?:'s|\s+is|\s+does)\s+{_STOPWATCH}(?:\s+(?:at|say|show))?|"
        rf"check\s+{_STOPWATCH})\b", re.I)),
]


def parse_stopwatch(text: str) -> str | None:
    t = text.strip().rstrip(".?!")
    for action, rx in _STOPWATCH_RE:
        if rx.search(t):
            return action
    return None


# ── spelling ──

_SPELL_RE = re.compile(
    r"^(?:(?:can|could)\s+you\s+)?(?:please\s+)?(?:how\s+(?:do|would)\s+(?:you|i|we)\s+spell|"
    r"spell(?:\s+out)?|what(?:'s|\s+is)\s+the\s+spelling\s+of)\s+"
    r"(?:the\s+word\s+)?[\"']?([a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*){0,2})[\"']?"
    r"(?:\s+for\s+me)?(?:\s+please)?$", re.I)


def parse_spell(text: str) -> str | None:
    m = _SPELL_RE.match(text.strip().rstrip(".?!,"))
    return m.group(1).lower() if m else None


# ── coin, dice, a random number ──

_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_COIN_RE = re.compile(r"\b(?:flip|toss)\s+a\s+coin\b|\bheads\s+or\s+tails\b", re.I)
_DICE_RE = re.compile(
    r"\broll\s+(a|an|one|two|three|four|five|six|\d)?\s*(?:(\d{1,3})[\s-]sided\s+)?(?:die|dice)\b", re.I)
_PICK_RE = re.compile(
    r"\b(?:pick|choose|give\s+me|say)\s+a\s+(?:random\s+)?number\s+(?:between|from)\s+"
    r"(-?\d+)\s+(?:and|to)\s+(-?\d+)\b", re.I)


def parse_random(text: str):
    """("coin",) / ("dice", count, sides) / ("number", low, high), or None."""
    if _COIN_RE.search(text):
        return ("coin",)
    m = _DICE_RE.search(text)
    if m:
        n = m.group(1) or "a"
        count = int(n) if n.isdigit() else _NUM_WORDS[n.lower()]
        sides = int(m.group(2) or 6)
        if not 1 <= count <= 6 or not 2 <= sides <= 100:
            return None
        if not m.group(1) and re.search(r"\bdice\b", m.group(0), re.I):
            count = 2 if re.search(r"\bthe\s+dice\b|\bsome\s+dice\b", text, re.I) else 1
        return ("dice", count, sides)
    m = _PICK_RE.search(text)
    if m:
        lo, hi = sorted((int(m.group(1)), int(m.group(2))))
        return ("number", lo, hi)
    return None


# ── "repeat that", "what can you do" ──

_REPEAT_RE = re.compile(
    r"^(?:sorry,?\s+|what\??,?\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:repeat\s+(?:that|it|yourself|what\s+you\s+said)|say\s+(?:that|it)\s+again|"
    r"what\s+did\s+you\s+(?:just\s+)?say|come\s+again|one\s+more\s+time)"
    r"(?:\s+please)?$", re.I)

_HELP_RE = re.compile(
    r"^(?:help|help\s+me|what\s+(?:can|do)\s+you\s+do|what\s+can\s+i\s+(?:ask|say\s+to)\s+you|"
    r"what\s+are\s+you\s+able\s+to\s+do|what\s+are\s+your\s+(?:skills|features)|"
    r"what\s+(?:kind\s+of\s+)?things\s+can\s+you\s+do)(?:\s+for\s+me)?$", re.I)


def is_repeat_request(text: str) -> bool:
    return bool(_REPEAT_RE.match(text.strip().rstrip(".?!")))


def is_help_request(text: str) -> bool:
    return bool(_HELP_RE.match(text.strip().rstrip(".?!")))


# ── bare mute, volume to a level ──

_MUTE_RE = re.compile(r"^(?:please\s+)?(un)?mute(?:\s+(?:it|that|this|the\s+(?:sound|volume|audio)))?"
                      r"(?:\s+please)?$", re.I)
_LEVEL_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_VOLUME_SET_RE = re.compile(
    r"^(?:please\s+)?(?:(?:set|turn|put|change|bring|lower|raise|drop|make)\s+)?(?:the\s+)?"
    r"(?:tv\s+|tv's\s+)?(?:volume|sound)\s+(?:down\s+|up\s+)?(?:to|at)\s+"
    r"(\d{1,3}|zero|one|two|three|four|five|six|seven|eight|nine|ten)\s*(percent|%)?$"
    r"|^volume\s+(\d{1,3}|zero|one|two|three|four|five|six|seven|eight|nine|ten)$", re.I)


def parse_mute(text: str) -> str | None:
    m = _MUTE_RE.match(text.strip().rstrip(".?!"))
    if not m:
        return None
    return "unmute" if m.group(1) else "mute"


def parse_volume_level(text: str) -> int | None:
    """0-100. "volume to 3" is on a 1-10 scale (30%), "to 30 percent" isn't."""
    m = _VOLUME_SET_RE.match(text.strip().rstrip(".?!"))
    if not m:
        return None
    raw = m.group(1) or m.group(3)
    n = int(raw) if raw.isdigit() else _LEVEL_WORDS[raw.lower()]
    if m.group(2) is None and n <= 10:
        n *= 10
    return n if 0 <= n <= 100 else None


# ── things she can't do yet ──
# Matched only when no skill claimed the request. A request that names one
# of these gets a specific answer; any other unclaimed command gets the
# general one. Questions and chat aren't commands, so they still reach the
# model.

_CANT = [
    (re.compile(r"\b(?:calendar|meeting|appointment)s?\b", re.I),
     "I can't see your calendar yet."),
    (re.compile(r"\b(?:e-?mails?|inbox)\b", re.I),
     "I can't read or send email yet."),
    (re.compile(r"^(?:please\s+)?(?:text|message|call|phone|facetime|dial|ring)\s+(?!it\b|that\b|this\b|me\b)\w|"
                r"\bsend\s+(?:a\s+)?(?:text|message)\b", re.I),
     "I can't send messages or make calls yet."),
    (re.compile(r"\b(?:thermostat|air\s+conditioning|the\s+a\.?c\.?|the\s+heat(?:er|ing)?)\b", re.I),
     "I can't change the thermostat. The AC controller sets it on its own every half hour."),
    (re.compile(r"\b(?:order|buy|purchase)\s+(?:me\s+)?(?:a|an|some|more|the)\b", re.I),
     "I can't order or buy things."),
    (re.compile(r"\bgarage\s+door\b", re.I),
     "I can't open or close the garage door."),
]

_COMMAND_RE = re.compile(
    r"^(?:(?:hey|ok(?:ay)?|so|um+|uh+|please|now)[,\s]+)*(?:can\s+you\s+|could\s+you\s+|would\s+you\s+|will\s+you\s+)?"
    r"(?:please\s+)?(?:start|stop|set|turn|switch|open|close|send|"
    r"email|order|buy|book|schedule|cancel|delete|enable|disable|activate|record|dim|brighten|"
    r"lower|raise|adjust|unlock|arm|disarm|run|launch|connect|pair)\b", re.I)


def unsupported_reply(text: str) -> str | None:
    """What to say for a command no skill can do, or None if it isn't one."""
    t = text.strip()
    for rx, reply in _CANT:
        if rx.search(t) and (_COMMAND_RE.search(t) or rx.pattern.startswith("^")
                             or re.search(r"\b(?:what's|whats|what\s+is|do\s+i\s+have|read|check|any)\b", t, re.I)):
            return reply
    if _COMMAND_RE.search(t):
        return "I can't do that yet."
    return None
