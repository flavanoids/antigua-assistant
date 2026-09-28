"""Ingredient substitution questions."""

import re


_SUBST_EXPLICIT_RE = re.compile(
    r"\b(?:"
    r"substitut(?:e|ion)\s+for\b|"
    r"(?:can\s+I|what\s+can\s+I)\s+use\s+instead\s+of\b|"
    r"instead\s+of\b.{0,30}\b(?:in\s+(?:baking|cooking|a\s+recipe))?\b|"
    r"replace\b.{0,20}\bwith\b|"
    r"(?:I\s+(?:don'?t|do\s+not)\s+have|I'?m\s+(?:out\s+of|missing))\b|"
    r"(?:dairy[- ]free|vegan|egg[- ]free|gluten[- ]free)\s+(?:substitute|replacement|alternative)\s+for\b|"
    r"(?:substitute|swap|swapping|replace|replacement|alternative)\s+for\b"
    r")\b",
    re.IGNORECASE,
)

_SUBST_CONTEXTUAL_RE = re.compile(
    r"\b\w+\s+substitute\s+(?:in|for|when)\b|"
    r"\buse\s+\w+\s+instead\b",
    re.IGNORECASE,
)

# Strips framing to isolate the ingredient name
_SUBST_STRIP_RE = re.compile(
    r"^\s*(?:(?:can\s+I|what\s+can\s+I|what'?s|what\s+(?:is|are)|what)\s+)?"
    r"(?:a\s+|an\s+|the\s+|(?:dairy[- ]free|vegan|egg[- ]free|gluten[- ]free)\s+)?"
    r"(?:use|substitut(?:e|ion)\s+for|replace|swap(?:ping)?|alternative\s+(?:to|for)|replacement\s+for)\s+",
    re.IGNORECASE,
)
_SUBST_TRAILING_RE = re.compile(
    r"\s+(?:instead(?:\s+of)?|in\s+(?:a\s+)?(?:baking|cooking|a\s+recipe|this\s+recipe)|"
    r"with\s+\S+.*|if\s+I\s+don'?t\s+have.*)$",
    re.IGNORECASE,
)
_SUBST_MISSING_RE = re.compile(
    r"(?:I\s+(?:don'?t|do\s+not)\s+have|I'?m\s+(?:out\s+of|missing))\s+(.+?)(?:\s*[,.].*)?$",
    re.IGNORECASE,
)
_SUBST_INSTEAD_RE = re.compile(
    r"instead\s+of\s+(.+?)(?:\s+(?:in|for|when)\b.*)?$",
    re.IGNORECASE,
)


def is_substitution_request(transcript: str) -> bool:
    return bool(_SUBST_EXPLICIT_RE.search(transcript)) or bool(
        _SUBST_CONTEXTUAL_RE.search(transcript)
    )


def extract_substitution_ingredient(transcript: str) -> str:
    """Return the ingredient being substituted, cleaned for use as a search query."""
    # "I don't have X" / "I'm out of X"
    m = _SUBST_MISSING_RE.search(transcript)
    if m:
        return m.group(1).strip().rstrip("?.,!")

    # "instead of X"
    m = _SUBST_INSTEAD_RE.search(transcript)
    if m:
        return m.group(1).strip().rstrip("?.,!")

    # strip leading framing then trailing qualifiers
    q = _SUBST_STRIP_RE.sub("", transcript).strip()
    q = _SUBST_TRAILING_RE.sub("", q).strip().rstrip("?.,!")
    return q or transcript.strip().rstrip("?.,!")
