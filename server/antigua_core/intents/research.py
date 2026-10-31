"""Research: which questions get the multi-search research run
(research.py) instead of a quick answer."""

import re

_LEAD = (r"^\s*(?:(?:hey\s+)?antigua[,\s]+)?"
         r"(?:(?:can|could|would|will)\s+you\s+|please\s+|i\s+(?:want|need)\s+you\s+to\s+)*")
# "Research X", "look into X", "dig into X": always research.
_EXPLICIT_RE = re.compile(
    _LEAD + r"(?:research|look\s+into|dig\s+into|do\s+(?:some\s+)?research\s+(?:on|into|about)|"
            r"(?:do\s+a\s+)?deep\s+(?:dive|search)\s+(?:on|into)|investigate)\s+(?P<q>.{4,})$",
    re.I)
# Shapes that are research on their own: prices across stores, comparisons,
# "is it worth it", and patterns over years.
_AUTO_RE = re.compile(
    r"\b(?:best|lowest|cheapest|good)\s+(?:price|deal)s?\s+(?:on|for)\b|"
    r"\bcheapest\s+(?:place|store|way)\s+to\s+(?:buy|get)\b|"
    r"\bwhere\s+(?:can|should)\s+i\s+(?:buy|get)\b.*\b(?:cheap|cheapest|best\s+price|deal)\b|"
    r"\bcompare\s+.+\s+(?:and|with|to|vs\.?|versus)\s+|"
    r"\bwhich\s+(?:one\s+)?is\s+better\b.+\bor\b|"
    r"\bshould\s+(?:i|we)\s+(?:get|buy)\s+.+\s+or\s+|"
    r"\bpros\s+and\s+cons\s+of\b|"
    r"\bis\s+.+\s+worth\s+(?:it|buying|getting|the\s+(?:money|price))\b|"
    r"\bweather\s+patterns?\b|\bclimate\s+(?:trends?|patterns?|history)\b|"
    r"\bhow\s+(?:has|have)\s+.+\s+changed\s+(?:over|in|since)\b|"
    r"\b(?:over|in)\s+the\s+(?:last|past)\s+(?:\d+|few|several|two|three|four|five|ten|twenty|thirty)\s+"
    r"(?:years|decades)\b",
    re.I)
# House commands that share a word ("look into the camera", "remind me to
# compare insurance quotes and prices"): research runs ahead of their routes.
_NOT_RESEARCH_RE = re.compile(r"\b(?:camera|doorbell|garage|screen|timers?|alarms?|remind|"
                              r"remember|note\s+that|to\s+(?:the|my)\s+(?:\w+\s+)?list)\b", re.I)


def parse(transcript: str) -> str | None:
    """The question to research in `transcript`, or None for a quick turn."""
    if not transcript or _NOT_RESEARCH_RE.search(transcript):
        return None
    m = _EXPLICIT_RE.match(transcript)
    if m:
        return m.group("q").strip(" ?.!")
    if _AUTO_RE.search(transcript):
        return re.sub(r"^\s*(?:hey\s+)?antigua[,\s]+", "", transcript).strip(" ?.!")
    return None
