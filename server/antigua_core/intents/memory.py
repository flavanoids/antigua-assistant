"""Memory (remember / forget / what did I …) and medicine questions."""

import functools
import re

from .. import household
from ..stores import _MEMORY_TAGS


# Broad medicine terms that trigger a "what specifically?" follow-up
_BROAD_MEDICINE_TERMS = {"medicine", "meds", "medication", "pill"}
_SPECIFIC_MEDICINE_TERMS = set(_MEMORY_TAGS["medicine"]) - _BROAD_MEDICINE_TERMS


def _needs_medicine_detail(raw: str) -> bool:
    """Return True if the raw text refers to medicine broadly but doesn't name a specific one."""
    raw_low = raw.lower()
    has_broad = any(t in raw_low for t in _BROAD_MEDICINE_TERMS)
    has_specific = any(t in raw_low for t in _SPECIFIC_MEDICINE_TERMS)
    return has_broad and not has_specific


def _is_medicine_query(transcript: str) -> bool:
    """Return True if the transcript is asking about medicine intake today."""
    low = transcript.lower()
    if not is_memory_query(transcript):
        return False
    return any(t in low for t in _MEMORY_TAGS["medicine"])


_REMEMBER_INTENT_RE = re.compile(
    r"^\s*(?:can you |please )?"
    r"(?:remember|don'?t forget|make a note|note that|keep in mind|log that|record that)"
    r"(?:\s+for\s+\w+)?(?:\s+that)?\s+(.+)",
    re.IGNORECASE,
)
# Person names (remember-for, "who?" replies, queries) come from
# antigua_core.household, built from server.yaml's household: block.
_DID_PERSON = r"\b(?:did|when did|has|have|what time did|what did)\s+(?:{})\b"
# Memory retrieval queries
_MEMORY_QUERY_RE = re.compile(
    _DID_PERSON.format("i|we|he|she")
    + r"|did (?:anyone|somebody|someone)\b"
    r"|\bsummary for\b|\bwhat did \w+ do\b",
    re.IGNORECASE,
)


@functools.lru_cache(maxsize=4)
def _did_member_re(alt: str):
    return re.compile(_DID_PERSON.format(alt), re.IGNORECASE)


_FORGET_LAST_RE = re.compile(
    r"\b(?:forget|delete|remove|undo|cancel)\s+(?:that|the last|my last|that last)\b"
    r"|\bthat(?:'s| is) wrong\b|\bactually(?:,)? (?:never mind|ignore that|forget that)\b",
    re.IGNORECASE,
)
# Content-specific forget: "forget that I took medicine", "delete about breakfast",
# "forget everything about vitamins", "delete all the memories about dogs"
_FORGET_CONTENT_RE = re.compile(
    r"\b(?:forget|delete|remove)\s+"
    r"(?:everything\s+|all\s+|every\s+)?"
    r"(?:that\s+|about\s+|"
    r"(?:the\s+)?(?:memory|memories|note|notes|entry|entries)\s+(?:about\s+|that\s+|for\s+|of\s+)?)"
    r"(.+)",
    re.IGNORECASE,
)
# "forget my medicine memory" / "delete the dog note"
_FORGET_MY_MEMORY_RE = re.compile(
    r"\b(?:forget|delete|remove)\s+(?:my|the|that|his|her|their)?\s*"
    r"(.+?)"
    r"\s+(?:memory|note|entry)\b",
    re.IGNORECASE,
)


def parse_remember_request(transcript: str) -> str | None:
    """Return the fact string if transcript is a remember request, else None."""
    m = _REMEMBER_INTENT_RE.match(transcript)
    if not m:
        return None
    fact = m.group(1).strip().rstrip(".,!?")
    # Strip leading "that" if it slipped through
    fact = re.sub(r"^that\s+", "", fact, flags=re.IGNORECASE)
    return fact if fact else None


def extract_remember_person(transcript: str) -> str | None:
    """Return canonical person name if explicitly named in a remember request."""
    m = household.people()["remember"].search(transcript)
    if not m:
        return None
    # Only household names match, so "for me" is never taken as a person.
    return household.canonical((m.group(1) or m.group(2) or "").strip())


def is_memory_query(transcript: str) -> bool:
    return bool(
        _MEMORY_QUERY_RE.search(transcript)
        or _did_member_re(household.people()["alt"]).search(transcript)
    )


def extract_query_person(transcript: str) -> str | None:
    """Return person name from a query if explicitly named, else None."""
    m = household.people()["query"].search(transcript)
    return household.canonical(m.group(1)) if m else None


def is_forget_request(transcript: str) -> bool:
    return bool(_FORGET_LAST_RE.search(transcript))


# Follow-up queries after a recent memory save — e.g. "When was that?", "What did I say?"
_FOLLOWUP_RE = re.compile(
    r"\b(?:when\s+(?:was|did)\s+(?:that|it)\b|"
    r"what\s+(?:did|time|was)\s+(?:that|it|I|he|she)\b|"
    r"what\s+did\s+(?:I|he|she|we)\s+say\b|"
    r"what\s+was\s+(?:that|it)\s+again\b|"
    r"repeat\s+(?:that|it)\b|"
    r"tell\s+me\s+again\b)",
    re.IGNORECASE,
)


def _extract_memory_keywords(transcript: str) -> list[str] | None:
    """Extract likely keywords from a memory query for relevance filtering.
    Returns a list of keywords, or None if no specific keywords found."""
    # Strip common query words and short tokens
    words = re.findall(r"[a-zA-Z]{3,}", transcript)
    stopwords = {
        "did",
        "when",
        "has",
        "have",
        "what",
        "time",
        "was",
        "were",
        "the",
        "that",
        "this",
        "today",
        "morning",
        "afternoon",
        "evening",
        "night",
        "and",
        "for",
        "with",
        "about",
        "from",
        "take",
        "took",
        "make",
        "made",
        "get",
        "got",
        "give",
        "gave",
        "say",
        "said",
        "tell",
        "told",
        "ask",
        "asked",
        "know",
        "knew",
        "think",
        "thought",
        "want",
        "wanted",
        "need",
        "needed",
        "like",
        "liked",
        "look",
        "looked",
        "come",
        "came",
        "go",
        "went",
        "put",
        "see",
        "saw",
        "find",
        "found",
        "use",
        "used",
        "work",
        "worked",
        "call",
        "called",
        "try",
        "tried",
    }
    keywords = [w.lower() for w in words if w.lower() not in stopwords]
    # If the query names a person, include that person's other memories too
    # by not filtering — just return None if too generic
    if len(keywords) < 2:
        return None
    # Limit to most distinctive words (longer = more distinctive)
    keywords = sorted(keywords, key=len, reverse=True)[:4]
    return keywords


def parse_forget_content(transcript: str) -> tuple[str, bool] | None:
    """Return (keyword, delete_all) for a content-specific forget request, else None.

    delete_all is True when the user says "everything", "all", etc.
    """
    delete_all = bool(
        re.search(r"\b(?:everything|all|every)\b", transcript, re.IGNORECASE)
    )

    m = _FORGET_CONTENT_RE.search(transcript)
    if m:
        keyword = m.group(1).strip().rstrip(".,!?")
        keyword = re.sub(
            r"\s+(?:memory|note|entry|please|today)\b$",
            "",
            keyword,
            flags=re.IGNORECASE,
        )
        if len(keyword) > 2 and keyword.lower() not in ("last", "that", "this"):
            return keyword, delete_all

    m = _FORGET_MY_MEMORY_RE.search(transcript)
    if m:
        keyword = m.group(1).strip().rstrip(".,!?")
        keyword = re.sub(
            r"\s+(?:memory|note|entry|please|today)\b$",
            "",
            keyword,
            flags=re.IGNORECASE,
        )
        if len(keyword) > 2 and keyword.lower() not in ("last", "that", "this"):
            return keyword, delete_all

    return None
