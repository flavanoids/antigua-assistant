"""Web search: which questions need live results, and the query to send."""

import re
from datetime import datetime

from .. import settings
from ..caches import pin_year
from .memory import is_memory_query
from .news import _NEWS_LOCATION_RE, _NEWS_PLACE_RE, extract_news_category, extract_news_topic, is_news_request
from .time_date import _DATE_QUERY_RE, _TIME_QUERY_RE
from .timers import parse_timer_request
from .weather import _WEATHER_SIMPLE_RE


_SEARCH_EXPLICIT_RE = re.compile(
    r"\b(?:"
    r"search\s+(?:for|the\s+web\s+for|online\s+for|that|it)|"
    r"look\s+it\s+up|look\s+up|"
    r"google\s+(?:it|that|for\s+me)?|"
    r"find\s+(?:information|info|out|me)\s+(?:about|on)|"
    r"what\s+can\s+you\s+find\s+(?:about|on)"
    r")\b",
    re.IGNORECASE,
)

_SEARCH_FACTUAL_RE = re.compile(
    r"\b(?:"
    r"who\s+(?:is|was|are|were|invented|discovered|founded|wrote|directed|created|won|plays|sang)\b|"
    r"what\s+(?:is|are|was|were)\s+the\s+(?:capital|population|tallest|largest|smallest|fastest|richest|biggest)|"
    r"when\s+(?:was|did|is|are)\s+\S+.{0,25}\s+(?:born|founded|built|invented|released|open|closed)|"
    r"how\s+(?:tall|old|far|many|much|big|large|fast|heavy)\s+is\b|"
    r"(?:price|cost|fee)\s+of\b|"
    r"phone\s+number\s+(?:for|of)\b|"
    r"(?:business\s+)?hours\s+(?:for|of)\b|"
    r"is\s+\S+.{0,20}\s+open\b|"
    r"recipe\s+for\b|"
    r"how\s+to\s+(?:make|cook|bake|fix|install|use|set\s+up|connect|reset)\b|"
    r"definition\s+of\b|"
    r"what\s+does\s+\w+\s+mean\b"
    r")\b",
    re.IGNORECASE,
)

_SEARCH_TEMPORAL_RE = re.compile(
    r"\b(?:latest|recent|newest|current|this\s+week.?s|today.?s)\s+"
    r"(?!weather|temperature|forecast|news|headlines?|time|date|timer)",
    re.IGNORECASE,
)

_SEARCH_STRIP_PREFIX_RE = re.compile(
    r"^\s*(?:(?:can\s+you|please|hey)\s+)?"
    r"(?:search\s+(?:for|the\s+web\s+for)|look\s+up|google\s+(?:for)?|"
    r"find\s+(?:information|info|out)\s+(?:about|on)|tell\s+me\s+about|"
    r"what\s+can\s+you\s+find\s+(?:about|on))\s+",
    re.IGNORECASE,
)

# Live/scheduled-event questions — fixtures, showtimes, results, standings.
# ("when is the next Dynamo game", "who won last night", "what time does the
# Rockets game start".) The model's weights cannot know these, but they match
# none of the explicit/factual/temporal patterns above, so before this they
# fell through to a bare LLM answer that sounded confident and was invented.
_SEARCH_EVENT_RE = re.compile(
    r"(?:"
    r"\bwhen\s+(?:is|are|was|does|do|did)\s+(?:the\s+)?(?:next|last|first)\b|"
    r"\bwhen\s+(?:do|does|did|will)\s+.{1,40}?\s+(?:play|start|begin|air|open|release)\b|"
    r"\bwhat\s+time\s+(?:is|does|do|did)\s+.{1,40}?\s+"
    r"(?:start|play|air|begin|kick\s*off|tip\s*off|come\s+on)\b|"
    r"\bnext\s+(?:game|match|fixture|race|fight|episode|launch|showing|concert)\b|"
    r"\b(?:who|what)\s+(?:won|lost|scored)\b|"
    r"\b(?:score|result|standings|schedule|lineup|odds|record)\s+(?:of|for|in)\b|"
    r"\b(?:is|are)\s+.{1,40}?\s+(?:playing|winning|open)\s+"
    r"(?:today|tonight|tomorrow|right\s+now|now|this\s+week|this\s+weekend)\b|"
    r"\bhow\s+did\s+.{1,40}?\s+do\s+(?:last\s+night|today|yesterday|this\s+season)\b"
    r")",
    re.IGNORECASE,
)


_HOWTO_RE = re.compile(
    r"\bhow\s+(?:do|would|can)\s+(?:i|you|we)\b|\bhow\s+to\b|"
    r"\b(?:install|replace|repair|wire|assemble|troubleshoot)\b",
    re.IGNORECASE,
)


def is_live_event_query(transcript: str) -> bool:
    """True for schedule/result questions that only the live web can answer."""
    return bool(_SEARCH_EVENT_RE.search(transcript))


def is_local_news_query(transcript: str) -> bool:
    """News scoped to a *place* the configured RSS feeds can't serve.

    "news in NYC" / "what's happening in Chicago" must hit the web: the feeds
    are BBC/Reuters/NPR world and national wires, so answering them from the
    cache returns confident, unrelated headlines. Topic news ("news about
    Ukraine", "headlines on the middle east") stays on the RSS path — that is
    exactly what extract_news_topic() filtering exists for. Category news
    (tech, science, business) has dedicated feeds and also stays on RSS.
    """
    if extract_news_category(transcript):
        return False
    if _NEWS_LOCATION_RE.search(transcript):
        return True
    return bool(_NEWS_PLACE_RE.search(transcript))


def is_search_request(transcript: str) -> bool:
    if not settings.SEARCH_ENABLED:
        return False
    if (
        (is_news_request(transcript) and not is_local_news_query(transcript))
        or bool(_WEATHER_SIMPLE_RE.search(transcript))
        or bool(_TIME_QUERY_RE.search(transcript))
        or bool(_DATE_QUERY_RE.search(transcript))
        or parse_timer_request(transcript) is not None
        or is_memory_query(transcript)
    ):
        return False
    return (
        bool(_SEARCH_EXPLICIT_RE.search(transcript))
        or bool(_SEARCH_FACTUAL_RE.search(transcript))
        or bool(_SEARCH_TEMPORAL_RE.search(transcript))
        or is_live_event_query(transcript)
        or is_local_news_query(transcript)
    )


def extract_search_query(transcript: str) -> str:
    q = _SEARCH_STRIP_PREFIX_RE.sub("", transcript).strip().rstrip("?.,!")
    return q or transcript.strip().rstrip("?.,!")


# Spoken questions make poor web queries: "when is the next Dynamo game" ranks a
# clothing retailer (Next), "who won the Astros game last night" ranks dictionary
# entries for "won". Strip the question framing down to the subject and re-add
# the intent as keywords a search engine can actually rank on.
_EVENT_FILLER = {
    "a", "an", "the", "is", "are", "was", "were", "do", "does", "did", "will", "when",
    "what", "who", "how", "time", "next", "last", "first", "of", "for", "in", "on",
    "at", "to", "up", "me", "my", "tell", "i", "it", "there", "their", "this", "that",
    "play", "plays", "playing", "played", "start", "starts", "starting", "begin",
    "begins", "game", "games", "match", "matches", "score", "scores", "result",
    "results", "won", "win", "wins", "lost", "vs", "versus", "against", "today",
    "tonight", "tomorrow", "yesterday", "night", "week", "weekend", "season", "and",
    "hey", "please", "can", "you", "s",
}
_EVENT_RESULT_RE = re.compile(
    r"\b(?:won|lost|score|scored|result|how\s+did|beat)\b", re.IGNORECASE
)
_EVENT_TIME_RE = re.compile(r"\bwhat\s+time\b", re.IGNORECASE)
_EVENT_WHEN_RE = re.compile(
    r"\b(?:last\s+night|yesterday|tonight|today|tomorrow|this\s+weekend)\b", re.IGNORECASE
)


def rewrite_event_query(transcript: str) -> str:
    """Turn a spoken fixture/result question into a keyword search query."""
    def _key(w: str) -> str:
        # Possessives only — str.strip("'s") would turn "does" into "doe" and
        # leak every function word ending in s back into the query.
        k = w.lower()
        return k[:-2] if k.endswith("'s") else k

    words = re.findall(r"[A-Za-z0-9']+", transcript)
    subject = " ".join(w for w in words if _key(w) not in _EVENT_FILLER)
    if not subject:
        return extract_search_query(transcript)

    # A bare "Dynamo" ranks Dynamo Kyiv and electrical generators; the home city
    # pins it to the local team. Only for one-word subjects — anything longer
    # ("New York Yankees", "SpaceX launch") already carries its own context.
    if settings.SEARCH_HOME_CITY and len(subject.split()) == 1:
        subject = f"{settings.SEARCH_HOME_CITY} {subject}"

    when = _EVENT_WHEN_RE.search(transcript)
    when_hint = f" {when.group(0)}" if when else ""
    # Undated queries rank evergreen and historical pages: "World Cup score
    # result" returned a 2034 bid story and a 2023 basketball upset. The current
    # year pins them — with it, the top hit is the actual final result.
    year = "" if re.search(r"\b(19|20)\d{2}\b", transcript) else f" {datetime.now():%Y}"
    if _EVENT_RESULT_RE.search(transcript):
        return f"{subject} score result{when_hint}{year}"
    if _EVENT_TIME_RE.search(transcript):
        return f"{subject} start time{when_hint}"
    return f"{subject} schedule next game{year}"

# Spoken questions rank glossaries and definitions: "who is the CEO of
# Starbucks" returned "Hierarchy of Company: CEO, CFO, COO…". Stripping the
# question framing leaves the keywords a search engine can actually rank on.
# This is the general form of rewrite_event_query, which only ever saw fixtures.
_QUERY_FRAMING_RE = re.compile(
    r"^\s*(?:(?:hey|ok|okay)\s+)?"
    r"(?:who|what|which|where|when|how\s+much|how\s+many|how\s+long|how\s+far)\s+"
    r"(?:is|are|was|were|does|do|did|will)\s+(?:the\s+|a\s+|an\s+)?",
    re.IGNORECASE,
)
# "X of Y" reads better to an engine as "Y X" — "CEO of Starbucks" -> "Starbucks CEO".
_QUERY_OF_RE = re.compile(r"^(.{2,40}?)\s+(?:of|for)\s+(?:the\s+)?(.+)$", re.IGNORECASE)

# Things that change over time. Undated queries for these rank whatever page has
# the most links, which is usually years old.
_FRESHNESS_RE = re.compile(
    r"\b(?:price|cost|worth|rating|score|ranking|record|latest|current|currently|"
    r"newest|recent|now|today|this\s+year|who\s+is\s+the\s+(?:ceo|president|"
    r"prime\s+minister|governor|mayor|coach|manager)|stock|shares|release\s+date|"
    r"out\s+yet|available|open|hours|schedule|winner|won|standings|election|"
    r"best[- ]selling|most\s+popular)\b",
    re.IGNORECASE,
)


def shape_general_query(transcript: str) -> str:
    """Turn a spoken question into keywords, without touching named entities."""
    q = extract_search_query(transcript)
    stripped = _QUERY_FRAMING_RE.sub("", q).strip()
    if not stripped:
        return q
    m = _QUERY_OF_RE.match(stripped)
    if m:
        attribute, subject = m.group(1).strip(), m.group(2).strip()
        # Only flip when the attribute is a short noun phrase ("CEO", "price",
        # "capital"); longer left sides are usually already a good query.
        if len(attribute.split()) <= 3:
            stripped = f"{subject} {attribute}"
    return stripped


def needs_fresh_results(transcript: str) -> bool:
    """Does this question's answer change over time?"""
    return bool(_FRESHNESS_RE.search(transcript) or _SEARCH_TEMPORAL_RE.search(transcript))


def build_search_query(transcript: str) -> tuple:
    """Return (query, engines_override) for a search-worthy transcript.

    Live questions (fixtures, results, local news) go to the news engines —
    the general engines answer them with homepage boilerplate ("Official match
    schedule of Chicago Fire FC") that contains no date to answer from.
    """
    if is_local_news_query(transcript):
        return f"{extract_news_topic(transcript)} news today", settings.SEARCH_NEWS_ENGINES
    if is_live_event_query(transcript):
        return rewrite_event_query(transcript), settings.SEARCH_NEWS_ENGINES

    query = shape_general_query(transcript)
    # "latest/current/newest X" still goes to the news engines, as before.
    if _SEARCH_TEMPORAL_RE.search(transcript):
        return pin_year(query), settings.SEARCH_NEWS_ENGINES
    if needs_fresh_results(transcript):
        # Year-pinning is what turned "World Cup score result" (a 2034 bid story
        # and a 2023 basketball upset) into the actual final. The same applies to
        # prices, officeholders and rankings — anything whose answer moves, even
        # when the general engines are the right place to look.
        return pin_year(query), None
    return query, None
