"""News requests: source, category, topic, place."""

import re


_NEWS_INTENT_RE = re.compile(
    r"\b(headline|headlines|the news|latest news|top stories|top news|"
    r"what.?s happening|what.?s going on|what.?s in the news|news update|"
    r"news briefing|tell me the news|give me the news|"
    r"brief me|news roundup|any news|catch me up|what did I miss|"
    r"what happened today|anything happen today)\b",
    re.IGNORECASE,
)
_NEWS_SOURCE_RE = re.compile(
    r"\b(al[- ]?jazeera|aljazeera|reuters|bbc(?:\s+news)?|the\s+guardian|guardian|"
    r"associated\s+press|ap(?:\s+news)?|npr)\b",
    re.IGNORECASE,
)
_NEWS_TOPIC_RE = re.compile(
    r"\b(?:news|headlines?)\s+(?:about|on|in|from|regarding|covering)\s+"
    r"([A-Za-z][A-Za-z\s]{1,30}?)(?:\?|$|\.|\s+from\b)",
    re.IGNORECASE,
)
_NEWS_LOCATION_RE = re.compile(
    r"\bwhat.?s\s+happening\s+in\s+([A-Za-z][A-Za-z\s]{1,25}?)(?:\?|$|\.)",
    re.IGNORECASE,
)
# Place-scoped news ("news in Chicago") goes to the web; topic prepositions
# (about/on/regarding/covering) stay on the RSS path with topic filtering.
_NEWS_PLACE_RE = re.compile(
    r"\b(?:news|headlines?)\s+in\s+([A-Za-z][A-Za-z\s]{1,30})",
    re.IGNORECASE,
)
_NEWS_CATEGORY_RE = re.compile(
    r"\b(tech(?:nology)?|science|business|finance|health|environment|sports?)\s+"
    r"(?:news|headlines?|updates?|stories)\b|"
    r"\b(?:news|headlines?)\s+(?:about|on)\s+"
    r"(tech(?:nology)?|science|business|finance|health|environment|sports?)\b",
    re.IGNORECASE,
)


# Follow-up on a story just read — "tell me more about X", "what else on the
# openai story", "any more detail on that". Broader than the generic
# _FOLLOWUP_RE above (that one only covers "what was that"/"repeat that").
_NEWS_FOLLOWUP_RE = re.compile(
    r"\b(?:tell\s+me\s+more(?:\s+about)?|(?:what|any|know)\s+(?:else|more)|"
    r"any(?:thing)?\s+else|"
    r"(?:more|any)\s+(?:detail|details|info|information)\s*(?:on|about)?|"
    r"(?:go|say\s+more)\s+on\s+(?:that|it))\b",
    re.IGNORECASE,
)


def is_news_followup(text: str) -> bool:
    return bool(_NEWS_FOLLOWUP_RE.search(text))


def is_news_request(text: str) -> bool:
    return bool(_NEWS_INTENT_RE.search(text)) or bool(_NEWS_CATEGORY_RE.search(text))


def extract_news_source(text: str):
    m = _NEWS_SOURCE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).lower().replace(" ", "").replace("-", "")
    if "jazeera" in raw:
        return "aljazeera"
    if "guardian" in raw:
        return "guardian"
    if "bbc" in raw:
        return "bbc"
    if "reuters" in raw:
        return "reuters"
    if "associatedpress" in raw or raw.startswith("ap"):
        return "ap"
    if "npr" in raw:
        return "npr"
    return None


def extract_news_category(text: str):
    m = _NEWS_CATEGORY_RE.search(text)
    if not m:
        return None
    cat = (m.group(1) or m.group(2)).lower()
    if cat.startswith("tech"):
        return "tech"
    if cat in ("sport", "sports"):
        return "sports"
    if cat in ("finance",):
        return "business"
    return cat


def extract_news_topic(text: str):
    m = _NEWS_TOPIC_RE.search(text) or _NEWS_LOCATION_RE.search(text)
    return m.group(1).strip() if m else None
