"""Live local facts answered from a data source rather than a web search:
store hours, stock and crypto prices, air quality and pollen."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# ── store hours ──

_DAY = (r"(?:\s+(?P<day>today|tonight|tomorrow|on\s+(?:monday|tuesday|wednesday|thursday|friday|"
        r"saturday|sunday)|(?:this\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)))?")
_PLACE = r"(?:the\s+)?(?P<place>[\w'&.\- ]+?)"
_HOURS_RES = [
    ("close", re.compile(rf"^(?:what\s+time|when)\s+(?:does|do|will)\s+{_PLACE}\s+close{_DAY}$", re.I)),
    ("open", re.compile(rf"^(?:what\s+time|when)\s+(?:does|do|will)\s+{_PLACE}\s+open(?:\s+up)?{_DAY}$", re.I)),
    ("close", re.compile(rf"^how\s+late\s+(?:is|does)\s+{_PLACE}\s+(?:open|stay\s+open){_DAY}$", re.I)),
    ("open_now", re.compile(rf"^(?:is|are)\s+{_PLACE}\s+(?:still\s+)?(?:open|closed)(?:\s+(?:right\s+)?now)?{_DAY}$",
                            re.I)),
    ("hours", re.compile(rf"^(?:what\s+are\s+)?(?:the\s+)?(?:store\s+)?hours\s+(?:for|of|at)\s+{_PLACE}{_DAY}$", re.I)),
    # "Costco's hours", "what are Target's store hours": the possessive is
    # required, or "wake me up in 8 hours" would be a store.
    ("hours", re.compile(rf"^(?:what\s+are\s+)?{_PLACE}(?:'s|s')\s+(?:store\s+)?hours{_DAY}$", re.I)),
]
_NOT_A_PLACE = re.compile(r"^(?:it|that|this|they|she|he|you|i|we|the\s+sun|sunset|sunrise|"
                          r"the\s+(?:market|stock\s+market|polls?|pool|window|door|garage|game)|"
                          r"the\s+(?:oven|timer|alarm)|school|daylight\s+saving.*)$", re.I)


@dataclass
class HoursRequest:
    kind: str           # "open_now" | "close" | "open" | "hours"
    place: str
    day: date | None = None


def _day(word: str | None, today: date) -> date | None:
    if not word:
        return None
    w = word.lower().replace("on ", "").replace("this ", "").strip()
    if w in ("today", "tonight"):
        return today
    if w == "tomorrow":
        return today + timedelta(days=1)
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if w in names:
        return today + timedelta(days=(names.index(w) - today.weekday()) % 7)
    return None


def parse_hours_request(text: str, today: date | None = None) -> HoursRequest | None:
    t = re.sub(r"^(?:do\s+you\s+know\s+|can\s+you\s+tell\s+me\s+)", "", text.strip().rstrip(".?!"), flags=re.I)
    for kind, rx in _HOURS_RES:
        m = rx.match(t)
        if not m:
            continue
        place = m.group("place").strip()
        if _NOT_A_PLACE.match(place) or _NOT_A_PLACE.match(f"the {place}") or len(place) < 2:
            return None
        return HoursRequest(kind, place, _day(m.group("day"), today or datetime.now().date()))
    return None


# ── stocks and crypto ──

CRYPTO = {
    "bitcoin": "bitcoin", "btc": "bitcoin", "ethereum": "ethereum", "ether": "ethereum",
    "eth": "ethereum", "dogecoin": "dogecoin", "doge": "dogecoin", "solana": "solana",
    "xrp": "ripple", "ripple": "ripple", "litecoin": "litecoin", "cardano": "cardano",
}
INDEXES = {
    "the stock market": "^GSPC", "the market": "^GSPC", "the markets": "^GSPC", "stocks": "^GSPC",
    "the s&p": "^GSPC", "the s&p 500": "^GSPC", "s&p 500": "^GSPC", "s and p": "^GSPC",
    "the s and p": "^GSPC", "the s and p 500": "^GSPC", "the dow": "^DJI", "the dow jones": "^DJI",
    "dow jones": "^DJI", "the nasdaq": "^IXIC", "nasdaq": "^IXIC",
}
_TICKER_NAME = r"(?P<name>[\w&.' -]+?)"
_MARKET_RES = [
    re.compile(rf"^(?:what(?:'s|\s+is)|how\s+much\s+is|check)\s+(?:the\s+)?(?:price\s+of\s+|value\s+of\s+)?{_TICKER_NAME}"
               r"(?:'s)?(?:\s+(?:stock|shares?|share\s+price|stock\s+price|price|trading\s+at|at|worth|"
               r"going\s+for|doing))*(?:\s+(?:right\s+)?(?:now|today))?$", re.I),
    re.compile(rf"^how(?:'s|\s+is|\s+are)\s+{_TICKER_NAME}(?:'s)?(?:\s+(?:stock|shares?))?(?:\s+doing)?"
               r"(?:\s+(?:right\s+)?(?:now|today))?$", re.I),
    re.compile(rf"^(?:what\s+did|where\s+did)\s+{_TICKER_NAME}(?:\s+(?:stock|shares?))?\s+close(?:\s+at)?"
               r"(?:\s+today)?$", re.I),
    re.compile(rf"^{_TICKER_NAME}\s+(?:stock|share|bitcoin)?\s*price$", re.I),
]


@dataclass
class MarketRequest:
    kind: str           # "crypto" | "index" | "stock"
    key: str            # coingecko id, index symbol, or a company name to look up


def parse_market_request(text: str) -> MarketRequest | None:
    t = text.strip().rstrip(".?!").replace("’", "'")
    for rx in _MARKET_RES:
        m = rx.match(t)
        if not m:
            continue
        raw = m.group("name").strip()
        name = raw.lower()
        bare = re.sub(r"^(?:the|a)\s+", "", name)
        if bare in CRYPTO:
            return MarketRequest("crypto", CRYPTO[bare])
        if name in INDEXES or f"the {name}" in INDEXES:
            return MarketRequest("index", INDEXES.get(name) or INDEXES[f"the {name}"])
        # Anything else is a stock only when the words say so: "how's Apple
        # doing" alone could be anything.
        if re.search(r"\b(?:stock|shares?|share\s+price|stock\s+price|trading|close)\b", t, re.I):
            return MarketRequest("stock", bare)
        return None
    return None


# ── air quality and pollen ──

_AIR_RE = re.compile(
    r"\b(?:air\s+quality|aqi|air\s+pollution|smog|ozone\s+(?:level|alert|today)|"
    r"(?:is|how(?:'s|\s+is))\s+the\s+air\b(?!\s+condition))", re.I)
_POLLEN_RE = re.compile(
    r"\b(?:pollen|allerg(?:y|ies)\s+(?:today|bad|forecast|outlook|report|level)|"
    r"(?:is|are)\s+(?:my\s+)?allergies\s+going\s+to\s+be|how\s+(?:bad\s+)?are\s+allergies)\b", re.I)
_ELSEWHERE = re.compile(r"\b(?:in|for|at)\s+(?!the\s+(?:house|morning|afternoon|evening)|here\b|"
                        r"today\b|tomorrow\b|my\b|the\s+area\b)[a-z]", re.I)


def parse_air_request(text: str) -> str | None:
    """"air" / "pollen" for home; None for another city (search has it)."""
    if _ELSEWHERE.search(re.sub(r"\b(?:in|for)\s+(?:the\s+)?air\b", "", text, flags=re.I)):
        return None
    if _POLLEN_RE.search(text):
        return "pollen"
    if _AIR_RE.search(text):
        return "air"
    return None
