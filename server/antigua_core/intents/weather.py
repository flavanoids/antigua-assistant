"""Weather routing: which turns are about weather, and another city if named."""

import re

from .. import settings


_WEATHER_CITY_RE = re.compile(
    r"(?:weather|temperature|forecast|rain|snow|hot|cold|humid)\s+"
    r"(?:in|for|at)\s+([A-Za-z][A-Za-z\s]{1,30}?)(?:\?|$|,|\s+today|\s+tomorrow)",
    re.IGNORECASE,
)




# Deliberately broader than _WEATHER_SIMPLE_RE (which triggers the weather
# skill). This only decides whether to hand the model the forecast, and
# over-injecting is harmless — weather_claim_allowed() is what stops it
# talking about weather nobody asked about.
_WEATHER_TOPIC_RE = re.compile(
    r"\b(?:weather|forecast|temperature|degrees|hot|cold|warm|chilly|freezing|"
    r"rain|raining|rainy|snow|storm|humid|humidity|sunny|cloudy|windy|"
    r"umbrella|jacket|sunscreen|outside|outdoors|heat)\b",
    re.IGNORECASE,
)


def wants_weather_context(transcript: str, messages: list) -> bool:
    """Should this turn carry the weather block?

    Injecting the forecast into every turn made the model volunteer it
    constantly — "I had a rough day" came back with the humidity reading. The
    data outweighed any instruction telling it to keep quiet, so it is left out
    unless the turn is plausibly about weather. Recent history counts, so a
    follow-up like "what about tomorrow?" still has the forecast to work from.
    """
    if _WEATHER_TOPIC_RE.search(transcript) or _WEATHER_CITY_RE.search(transcript):
        return True
    recent = [m.get("content", "") for m in messages if m.get("role") == "user"][-2:]
    return any(_WEATHER_TOPIC_RE.search(m) for m in recent)


def extract_weather_location(text):
    """Return a non-default location string if user asked about another city."""
    m = _WEATHER_CITY_RE.search(text)
    if m:
        city = m.group(1).strip()
        # The home city means "here" — answered from the home forecast.
        if city.lower() not in (
            settings.SEARCH_HOME_CITY.lower() or "here",
            "here",
            "outside",
            "there",
            "today",
            "tomorrow",
        ):
            return city
    return None

# Simple weather queries that bypass the LLM entirely
_WEATHER_SIMPLE_RE = re.compile(
    r"\b(what(?:'s|\s+is)\s+(?:the\s+)?weather|"
    r"how'?s?\s+(?:the\s+)?weather|"
    r"what(?:'s|\s+is)\s+(?:the\s+)?temperature|"
    r"how\s+(?:hot|cold|warm)\s+(?:is\s+it|outside)|"
    r"will\s+it\s+rain|"
    r"is\s+it\s+going\s+to\s+rain|"
    r"what(?:'s|\s+is)\s+(?:the\s+)?forecast)\b",
    re.IGNORECASE,
)

# Broader weather routing gate: any phrasing the deterministic weather skill can
# answer (weather.classify_weather_intent selects which phrase_*() handles it).
# Deliberately question-shaped, and checked LATE in classify() so imperatives
# ("remind me to grab an umbrella") are claimed by memory/timers first.
_WEATHER_ROUTE_RE = re.compile(
    r"\b(weather|forecast|"
    r"(?:how\s+(?:hot|cold|warm|humid|windy)|what.*(?:temperature|degrees))|"
    r"is\s+it\s+(?:cold|chilly|hot|freezing|windy|humid|nice)\s+(?:out|outside|today)|"
    r"is\s+it\s+(?:going\s+to|gonna)\s+(?:rain|snow|storm)|"
    r"(?:will|is)\s+it\s+(?:rain|snow|drizzle|pour)\w*|"
    r"chance\s+of\s+(?:rain|showers|snow|storms|thunderstorms)|"
    r"(?:any|more)\s+(?:rain|snow|storms)\b|"
    r"raining|snowing|"
    r"do\s+i\s+need\s+(?:an?\s+)?(?:umbrella|jacket|coat|raincoat)|"
    r"need\s+(?:an?\s+)?umbrella|"
    r"(?:when(?:'s| is)?\s+)?(?:sunset|sunrise|sun\s+set|sun\s+rise)|"
    r"when\s+(?:will\s+it|does\s+it)\s+get\s+dark|"
    r"(?:warmer|colder|cooler|hotter)\s+than\s+yesterday)\b",
    re.IGNORECASE,
)
