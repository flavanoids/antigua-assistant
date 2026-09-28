"""Time and date questions, answered by the time_date skill."""

import re


_TIME_QUERY_RE = re.compile(
    r"\b(what(?:'s|\s+is)\s+(?:the\s+)?time(?:\s+is\s+it)?|"
    r"what\s+time\s+is\s+it|"
    r"current\s+time|"
    r"time\s+(?:right\s+now|now))\b",
    re.IGNORECASE,
)
_DATE_QUERY_RE = re.compile(
    r"\b(what(?:'s|\s+is)\s+(?:the\s+)?(?:date|day)|"
    r"what\s+day\s+(?:is\s+it|of\s+the\s+week)|"
    r"today'?s?\s+date|"
    r"which\s+(?:day|date|is)\s+(?:is\s+)?(?:it|today)|"
    r"what\s+(?:day|month|year)\s+is\s+(?:it|today))\b",
    re.IGNORECASE,
)
