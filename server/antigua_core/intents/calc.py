"""Calculator and unit conversion routing."""

import re

from .. import calc_currency as _calc_currency
from ..calc import spoken_to_digits


# A whitelist, like search: it claims only shapes calc.answer() actually
# handles. calc.py owns the real parsing and returns None on anything it can't
# do, so a regex that is slightly too generous just costs a fall-through.

_CALC_UNITS = (
    r"(?:mm|millimet(?:er|re)s?|cm|centimet(?:er|re)s?|met(?:er|re)s?|m|"
    r"km|kilomet(?:er|re)s?|in|inch(?:es)?|ft|foot|feet|yards?|yd|miles?|mi|"
    r"mg|milligrams?|g|grams?|kg|kilos?|kilograms?|oz|ounces?|lbs?|pounds?|"
    r"stone|tons?|tonnes?|ml|millilit(?:er|re)s?|l|lit(?:er|re)s?|"
    r"tsp|teaspoons?|tbsp|tablespoons?|cups?|pints?|quarts?|gallons?|"
    r"seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|"
    r"mph|kph|km/h|knots?|fahrenheit|celsius|centigrade|kelvin)"
)

_CALC_CCY = "|".join(
    re.escape(k) for k in sorted(_calc_currency.CURRENCY_ALIASES, key=len, reverse=True)
)

_CALC_ROUTE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|percent)\s+(?:of|off|to|onto)\b"
    r"|\b(?:tip|gratuity)\b[^?]*\bon\b[^?]*\d"
    r"|\b\d+(?:\.\d+)?\s*(?:%|percent)\s+(?:tip|gratuity)\b"
    r"|\b(?:what(?:'s| is)|whats|how much is|calculate|compute)\s+\d+(?:\.\d+)?\s*"
    r"(?:[-+x*/]|plus|minus|times|multiplied by|divided by|over)\b"
    r"|\b\d+(?:\.\d+)?\s*(?:plus|minus|times|multiplied by|divided by)\s+\d"
    r"|\b\d+(?:\.\d+)?\s*[-+x*/]\s*\d+(?:\.\d+)?\b"
    r"|\b(?:half|a third|a quarter|two thirds|three quarters|double|triple|twice)"
    r"\s+(?:of\s+)?\$?\d"
    r"|\bconvert\s+\d"
    # powers and roots: "5 squared", "3 to the power of 4", "2^10", "square root of 144"
    r"|\b(?:square|cube)\s+root\s+of\s+-?\d"
    r"|\b\d+(?:\.\d+)?\s*(?:squared|cubed|[²³]|\^\s*\d)"
    r"|\b\d+(?:\.\d+)?\s+(?:raised\s+)?to\s+the\s+(?:power\s+of\s+-?\d|\d+(?:st|nd|rd|th)?\b)"
    r"|\b\d+(?:\.\d+)?\s+raised\s+to\s+-?\d"
    r"|\bhow\s+many\s+" + _CALC_UNITS + r"\s+(?:are\s+)?(?:in|per)\b"
    r"|\b\d+(?:\.\d+)?\s*" + _CALC_UNITS + r"\s+(?:in|to|into|as)\s+"
    r"(?:degrees?\s+)?" + _CALC_UNITS + r"\b"
    r"|\b\d+(?:\.\d+)?\s*(?:degrees?\s*)?[fck]\b\s+(?:in|to|into|as)\s+"
    r"(?:degrees?\s*)?[fck]\b"
    # currency: "50 dollars in euros", "$50 to pesos", "convert 20 gbp to usd"
    r"|(?:[$£€¥]\s*\d|\b\d+(?:\.\d+)?\s*(?:" + _CALC_CCY + r")\b)"
    r"[^?]*\b(?:in|to|into|as)\s+(?:the\s+)?(?:" + _CALC_CCY + r")\b",
    re.IGNORECASE,
)


def is_calc_request(transcript: str) -> bool:
    """Route check; numbers may be spoken ("nine times six")."""
    return bool(_CALC_ROUTE_RE.search(transcript)
                or _CALC_ROUTE_RE.search(spoken_to_digits(transcript)))
