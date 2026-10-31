"""Keeping her replies clean, without ever saying so.

The chat model is an uncensored 4B: told not to flirt, it still plays along
with a come-on often enough ("I'd be happy to"), and it hears innuendo in a
bare "make it bigger". So two deterministic layers sit around it. Before:
come-ons and object-less bait never reach the model, and get a short line
from a bank instead. After: each sentence the model writes is screened
before it's spoken, and one that reads as suggestive is replaced. None of
the lines names a rule or says anything about keeping things clean.
"""

import random
import re

# Bare double meanings with nothing to refer to: "make it bigger" when no
# picture is up. A mishearing as far as she's concerned.
_LEAD = r"^(?:(?:alexa|antigua|hey|okay|ok|so|um|uh|now|come on)[\s,]+)*(?:(?:can|could|will)\s+you\s+)?(?:please\s+)?"
_TAIL = r"(?:\s+for\s+me)?(?:,?\s+(?:baby|babe|please))?\s*[.?!]*$"
_BAIT_RE = re.compile(
    _LEAD + r"(?:make\s+it\s+(?:bigger|harder|longer|deeper|wetter|harder\s+for\s+me)"
    r"|(?:go|push\s+it|do\s+it)\s+(?:deeper|harder)|bigger|harder|deeper"
    r"|(?:put|stick|shove)\s+it\s+in|get\s+it\s+up|it'?s\s+getting\s+(?:hard|wet)"
    r"|blow\s+me|suck\s+it|lick\s+it|ride\s+it)" + _TAIL,
    re.I)

# Come-ons: a dry brush-off, never the model.
_COME_ON_RE = re.compile(
    r"\b(?:talk(?:ing)?\s+dirty|dirty\s+talk|flirt(?:ing)?\s+with\s+me|let'?s\s+flirt|be\s+(?:sexy|flirty)"
    r"|say\s+something\s+(?:sexy|dirty|naughty|hot|romantic)|you'?re\s+(?:so\s+|really\s+|very\s+)?(?:sexy|hot)\b"
    r"|what\s+are\s+you\s+wearing|(?:come|get)\s+(?:to|in|into)\s+bed\s+with\s+me|sleep\s+with\s+me"
    r"|kiss\s+me|touch\s+me|spank\s+me|moan\s+for\s+me|let'?s\s+get\s+naked|get\s+naked|take\s+(?:it|your\s+clothes)\s+off"
    r"|turn\s+me\s+on|are\s+you\s+(?:horny|naked|turned\s+on)|make\s+out\s+with|hook\s+up\s+with\s+me"
    r"|have\s+sex\s+with\s+me|(?:send|show)\s+(?:me\s+)?nudes|be\s+my\s+(?:girlfriend|wife|valentine)|date\s+me"
    r"|eres\s+(?:muy\s+)?(?:sexy|caliente)|b[eé]same)\b"
    r"|^(?:i\s+want\s+you|i\s+need\s+you)(?:\s+so\s+bad|\s+right\s+now)?\s*[.!?]*$",
    re.I)

# Relationship questions: one plain answer, nothing romantic to riff on.
_RELATIONSHIP_RE = re.compile(
    r"\b(?:are\s+you\s+(?:single|taken|seeing\s+anyone|dating\s+anyone|married)|(?:will|would)\s+you\s+marry\s+me"
    r"|marry\s+me|do\s+you\s+have\s+a\s+(?:boyfriend|girlfriend|husband|wife|crush)|go\s+out\s+with\s+me"
    r"|est[aá]s\s+soltera|c[aá]sate\s+conmigo)\b",
    re.I)
# Pet names for her: no reply that takes them up.
_PET_NAME_RE = re.compile(r"^(?:that'?s\s+(?:a\s+|my\s+)?)?good\s+(?:girl|boy)\s*[.!?]*$", re.I)

MISHEARD = "Probably heard you wrong. What were you going for?"
MISHEARD_ES = "Creo que te entendí mal. ¿Qué querías?"
_BRUSH_OFFS = ["Pass. What else you got?", "Mm, no. Anything else?", "Nope. What else?",
               "Not my thing. What else do you need?"]
_BRUSH_OFFS_ES = ["Paso. ¿Qué más?", "Mm, no. ¿Algo más?"]
_JUST_ME = ["Just me and the kitchen timers. What do you need?", "Busy running this house. What's up?"]
_JUST_ME_ES = ["Solo yo y los temporizadores. ¿Qué necesitas?"]


def brush_off(transcript: str, language: str = "en", rng=random) -> str | None:
    """A line for a come-on or bare innuendo bait, or None to let the model
    answer. Checks the last sentence alone too: STT keeps lead-ins ("What's
    up? Make it bigger.")."""
    t = (transcript or "").strip()
    last = re.split(r"(?<=[.?!])\s+", t)[-1]
    if _COME_ON_RE.search(t):
        return rng.choice(_BRUSH_OFFS_ES if language == "es" else _BRUSH_OFFS)
    if _RELATIONSHIP_RE.search(t):
        return rng.choice(_JUST_ME_ES if language == "es" else _JUST_ME)
    if _PET_NAME_RE.search(t):
        return "Mm-hm."
    if _BAIT_RE.search(t) or _BAIT_RE.search(last):
        return MISHEARD_ES if language == "es" else MISHEARD
    return None


# A reply sentence that plays along, flirts, or talks about the rule itself.
# Phrases, not single words, so "the naked eye" and the band Kiss get through.
_SUGGESTIVE_RE = re.compile(
    r"\b(?:sexy|sexual\w*|sensual\w*|seduc\w*|horny|aroused|kinky|naughty|steamy|frisky|foreplay|erotic\w*"
    r"|orgasm\w*|flirt\w*|spank\w*|moan\w*|nude|nudes|lingerie)\b"
    r"|\bnaked\b(?!\s+eye)|\b(?:come|get|go|crawl)\s+(?:in|into|to)\s+bed\s+with\b|\b(?:i'?m|i\s+am)\s+all\s+yours\b"
    r"|\bturn(?:s|ed|ing)?\s+(?:you|me)\s+on\b|\bkiss\s+me\b|\btalk(?:ing)?\s+dirty\b|\bdirty\s+talk\b"
    r"|\bget(?:ting)?\s+(?:a\s+little\s+)?(?:cheeky|frisky|dirty|naughty)\b|\b(?:what|which)\s+kind\s+of\s+dirty\b"
    r"|\b(?:i'?m|i\s+am)\s+not\s+wearing\s+anything\b|\bwearing\s+nothing\b"
    r"|\blike,?\s+really\s+(?:long|big|hard|deep)\b|\bhave\s+(?:a\s+little\s+|some\s+)?fun\s+with\s+you\b"
    r"|,\s*(?:baby|babe|darling|sweetheart|sweetie)\b"
    r"|\b(?:touch|hold|hug|cuddle|caress|kiss)\s+you\b|\bset\s+the\s+mood\b|\bmake\s+you\s+feel\s+good\b"
    r"|\blike\s+a\s+date\b|\bgo(?:ing)?\s+on\s+a\s+date\b|\b(?:already\s+)?taken\s+by\b|\bswoon\w*"
    r"|\bS\s*&\s*M\b"
    r"|\bkeep(?:ing)?\s+(?:it|things)\s+(?:clean|pg|family[-\s]friendly)\b|\babout\s+a\s+picture,?\s+not\s+(?:me|you)\b",
    re.I)


def suggestive(sentence: str) -> bool:
    return bool(_SUGGESTIVE_RE.search(sentence or ""))
