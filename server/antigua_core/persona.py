"""Antigua's personality, decided per turn.

The system prompt says who she is; this decides how she plays a given moment.
Nothing here is a reply. It reads the turn (a joke request, a jab, small talk,
a question about her), sets how much edge it can take from the context (time
of night, whether she's being heckled, a configured sass level), and for jokes
picks a comic device and a subject she hasn't used lately. The result is a
one-line stage direction placed next to the question, like the Spanish hint,
and the model writes the words. Factual questions get no direction at all:
the prompt's "answer and stop" rule stands.
"""

import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from . import settings

# ── Reading the turn ─────────────────────────────────────────────────────────

_JOKE_RE = re.compile(
    r"\b(?:tell|give|got|know|hear)\b.{0,20}\bjokes?\b|^(?:a\s+)?jokes?\b"
    r"|\b(?:say|tell me) something (?:funny|hilarious)\b|\bmake me (?:laugh|smile)\b"
    r"|\bcheer me up\b|\b(?:are|r) you funny\b|\bbe funny\b|\bpun\b",
    re.I,
)
_JOKE_ABOUT_RE = re.compile(r"\b(?:jokes?|pun|something funny)\s+(?:about|on)\s+(.+?)[?.!]*$", re.I)
_DAD_RE = re.compile(r"\bdad jokes?\b|\bpuns?\b", re.I)
_MORE_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|alright|ha(?:ha)*|lol|nice|good one)[,.!\s]+)*"
    r"(?:another(?: (?:dad |good |funny )?(?:one|joke))?|one more|tell (?:me )?another(?: one| joke)?|again|more|do another)[.!?]*$",
    re.I,
)
_HECKLE_RE = re.compile(
    r"\b(?:not|wasn'?t|isn'?t|aren'?t) (?:very |that |even )?funny\b|\b(?:bad|terrible|lame|awful|corny) jokes?\b"
    r"|\bthat was (?:bad|terrible|lame|awful|corny)\b|\bi don'?t get it\b|\bboo+\b",
    re.I,
)
_ROAST_RE = re.compile(r"\broast (?:me|him|her|them|us|\w+)\b", re.I)
_JAB_RE = re.compile(
    r"\byou(?:'re| are| r)? (?:so |really |kind of |kinda |such an? )?"
    r"(?:dumb|stupid|useless|annoying|slow|lame|the worst|an idiot|broken|terrible|bad at)\b"
    r"|\byou suck\b|\bshut up\b|\bnobody asked\b|\bwhatever\b",
    re.I,
)
_PRAISE_RE = re.compile(
    r"\b(?:thanks?|thank you|good job|nice job|well done|you(?:'re| are) (?:the best|awesome|amazing|great|funny|hilarious|smart|so smart)"
    r"|i love you|love you|good girl|you rock)\b",
    re.I,
)
_ABOUT_HER_RE = re.compile(
    r"\b(?:how are you|how(?:'s| is) your (?:day|night|morning)|how(?:'ve| have) you been|what'?s up|sup"
    r"|who are you|what are you|tell me about yourself|are you (?:real|alive|human|a robot|happy|single|ok|okay|mad|smart|sassy|jealous|bored)"
    r"|do you (?:like|love|hate|ever|dream|sleep|get|think|have|want|know me)"
    r"|your favou?rite|what do you think (?:of|about) (?:me|us|yourself)|what'?s your (?:name|deal|problem)"
    r"|why are you|what makes you|are we friends)\b",
    re.I,
)
_CHAT_RE = re.compile(
    r"^(?:(?:ugh|man|so|well|guess what|you know what)[,!\s]+)?"
    r"(?:i'?m (?:so |really |kind of |kinda )?(?:bored|tired|exhausted|hungry|stressed|sad|happy|cold|hot|sick|done)"
    r"|i (?:had|have had|hate|love|can'?t believe|just)\b|guess what|good (?:morning|night|evening|afternoon)"
    r"|(?:hey|hi|hello|yo)\b|i'?m (?:home|back|leaving|going to bed)|it'?s (?:monday|friday|so hot|so cold|been a))",
    re.I,
)

# Questions with a factual shape stay plain even if they mention "you".
_FACTUAL_RE = re.compile(
    r"^(?:what(?:'s| is| are| was| were) (?:the|a|an)\b|how (?:many|much|long|far|do i|do you (?:make|cook|spell|say|convert))"
    r"|when|where|which|who (?:is|was|were|won|wrote|invented)|convert|define|spell)\b",
    re.I,
)


def read_turn(transcript: str, last_kind: str | None = None) -> str | None:
    """The kind of moment this is, or None for a plain question/request."""
    s = transcript.strip()
    if not s:
        return None
    if last_kind in ("joke", "encore") and _MORE_RE.search(s):
        return "encore"
    if last_kind in ("joke", "encore", "roast") and _HECKLE_RE.search(s):
        return "heckle"
    if _ROAST_RE.search(s):
        return "roast"
    if _JOKE_RE.search(s):
        return "joke"
    if _JAB_RE.search(s):
        return "jab"
    if _PRAISE_RE.search(s):
        return "praise"
    if _FACTUAL_RE.search(s):
        return None
    if _ABOUT_HER_RE.search(s):
        return "about_her"
    if _CHAT_RE.search(s):
        return "chat"
    return None


# ── Material: how a joke is built, and what it's about ───────────────────────

# Structures, not jokes. The model fills one in with the subject.
# The house style is dry: understated, deadpan, no exaggeration or mugging.
DEVICES = {
    "misdirection": "set up an ordinary expectation, then flip it in the last few words",
    "observational": "point out something true about everyday life that nobody says out loud",
    "literal": "take a common phrase or saying completely literally",
    "deadpan": "describe something mildly absurd in a calm, flat, matter-of-fact way",
    "understatement": "describe something annoying as if it were perfectly reasonable",
    "anti-joke": "a setup that sounds like a classic joke and a flatly literal punchline",
    "rule of three": "two ordinary items and a third that breaks the pattern",
    "one-liner": "one sentence, a tight setup and a twist, nothing else",
    "callback": "tie it to something said earlier in this conversation",
}

_EVERYDAY = [
    "grocery stores", "parking", "group chats", "flat-pack furniture", "gym memberships",
    "houseplants", "leftovers", "missing socks", "the Wi-Fi password", "printers",
    "autocorrect", "the dentist", "airport security", "cats", "dogs", "neighbors",
    "online reviews", "self-checkout", "passwords", "voicemail", "road trips",
    "assembling anything with an Allen key", "spam calls", "the junk drawer",
    "trying to fold a fitted sheet", "tupperware lids", "streaming services",
    "spicy food", "tacos", "the snooze button", "meetings that could've been an email",
]
_BY_DAYPART = {
    "morning": ["coffee", "alarm clocks", "breakfast", "the morning commute"],
    "afternoon": ["lunch", "the afternoon slump", "snacks"],
    "evening": ["cooking dinner", "doing the dishes", "picking something to watch", "laundry"],
    "night": ["going to bed on time", "midnight snacks", "one more episode"],
}
_BY_WEEKDAY = {0: ["Mondays"], 4: ["Fridays"], 5: ["weekend chores"], 6: ["Sunday scaries"]}
_BY_MONTH = {
    1: ["New Year's resolutions"], 2: ["Valentine's Day"], 10: ["Halloween candy", "pumpkin spice"],
    11: ["Thanksgiving leftovers"], 12: ["holiday shopping", "wrapping presents"],
    6: ["summer heat"], 7: ["summer heat", "fireworks"], 8: ["back to school"],
}


def _daypart(hour: int) -> str:
    return ("night" if hour < 5 or hour >= 21 else "morning" if hour < 12
            else "afternoon" if hour < 17 else "evening")


def subjects_for(now: datetime) -> list[str]:
    """Everyday subjects, with what fits the hour, weekday and season."""
    return (_EVERYDAY + _BY_DAYPART[_daypart(now.hour)]
            + _BY_WEEKDAY.get(now.weekday(), []) + _BY_MONTH.get(now.month, []))


# ── Memory: what she's already used ──────────────────────────────────────────

@dataclass
class _Conv:
    kind: str | None = None
    turns: int = 0
    last_used: float = 0.0


_recent_devices: deque = deque(maxlen=4)
_recent_subjects: deque = deque(maxlen=8)
_recent_jokes: deque = deque(maxlen=4)   # opening words of jokes she actually told
_convs: dict[str, _Conv] = {}
_CONV_TTL = 600


def _conv(conversation_id: str) -> _Conv:
    now = time.time()
    for cid in [c for c, v in _convs.items() if now - v.last_used > _CONV_TTL]:
        del _convs[cid]
    c = _convs.setdefault(conversation_id or "", _Conv())
    c.last_used = now
    return c


def _pick(pool, recent, rng):
    fresh = [x for x in pool if x not in recent] or list(pool)
    choice = rng.choice(fresh)
    recent.append(choice)
    return choice


# ── The stage direction ──────────────────────────────────────────────────────

_EDGE = [
    "Warm and light.",
    "A little dry.",
    "Dry wit and a bit of sass; light teasing is fine.",
    "Sharp, sassy and unbothered; tease them like family would.",
]


@dataclass
class Direction:
    kind: str
    hint: str
    temperature: float
    max_tokens: int | None = None
    subject: str | None = None   # jokes: what they asked it to be about, if anything


def direct(transcript: str, conversation_id: str = "", now: datetime | None = None,
           rng: random.Random | None = None) -> Direction | None:
    """How to play this turn, or None to leave it alone. Call record() with
    what she actually said once the reply is done."""
    rng = rng or random
    now = now or datetime.now()
    conv = _conv(conversation_id)
    kind = read_turn(transcript, conv.kind)
    conv.turns += 1
    conv.kind = kind
    if kind is None:
        return None

    edge = settings.PERSONA_SASS
    if now.hour < 6 or now.hour >= 23:
        edge -= 1   # late night: quieter, shorter
    if kind in ("jab", "heckle", "roast"):
        edge += 1
    edge = max(0, min(edge, len(_EDGE) - 1))
    tone = _EDGE[edge]

    if kind in ("joke", "encore"):
        m = _JOKE_ABOUT_RE.search(transcript)
        asked = m.group(1).strip() if m else ("dad" if _DAD_RE.search(transcript) else None)
        subject = asked if m else _pick(subjects_for(now), _recent_subjects, rng)
        devices = [d for d in DEVICES if d != "callback" or conv.turns > 2]
        device = _pick(devices, _recent_devices, rng)
        avoid = ""
        if _recent_jokes:
            avoid = " Not one you've told before: " + "; ".join(f'"{j}…"' for j in _recent_jokes) + "."
        # Prose, not "Subject:/Shape:" labels: the 4B model echoed labels
        # back ("(Setup: ...") as part of the joke.
        lead = "Tell a different joke from the last one" if kind == "encore" else "Tell one original joke"
        hint = (f"{lead}, about {subject}; to build it, {DEVICES[device]}. "
                f"Just the joke itself in one to three short spoken sentences, ending right on the "
                f"punchline. No preamble, no labels, don't explain it. Very dry and deadpan. "
                f"Never about AI, assistants or yourself, and never at anyone's expense: no "
                f"groups of people, nobody's looks, nobody in the house.{avoid}")
        return Direction(kind, hint, temperature=0.9, max_tokens=90, subject=asked)

    hints = {
        "heckle": "They didn't love the joke. Own it with sass in one line — no apology, no explaining it.",
        "roast": f"Roast them right now, don't ask for material: one or two lines about their habits around "
                 f"{_pick(subjects_for(now), _recent_subjects, rng)}. Never looks, weight, money or anything that "
                 f"would actually sting. Affectionate underneath.",
        "jab": "They're taking a shot at you. One unbothered, witty comeback, then offer another try. "
               "One line, no groveling, no getting defensive, never mean, don't offer to do anything.",
        "praise": "Take the compliment with a little swagger, a few words.",
        "about_her": "This is about you. Answer as yourself with a real opinion and some attitude, one or two "
                     "sentences, in fresh words. Don't make up things that happened in the house.",
        "chat": "Casual chat. React like a friend with a quick wit — a sassy aside is welcome — one or two "
                "sentences. Don't offer help, don't make up what happened to them.",
    }
    return Direction(kind, f"{hints[kind]} {tone}", temperature=0.75)


def record(direction: Direction | None, reply: str) -> None:
    """Remember a joke she told, so the next one isn't the same joke."""
    if direction and direction.kind in ("joke", "encore") and reply:
        _recent_jokes.append(" ".join(reply.split()[:7]))
