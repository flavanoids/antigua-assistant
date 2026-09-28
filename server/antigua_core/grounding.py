"""Grounding guards — shared by the primary and fallback servers.

Moved verbatim from antigua_server.py (Phase 2, antigua_core extraction).
"""

import re

from . import household, settings

# A weather claim with no weather data behind it is invented. The 4b model does
# this in greetings no matter how the prompt is worded ("mostly sunny, highs
# around ninety"), so the last line of defence is deterministic: on turns where
# no weather block was injected, these sentences never reach TTS.
_WEATHER_CLAIM_RE = re.compile(
    r"\b\d{2,3}\s*(?:°|degrees?\b)|\bfeels\s+like\s+\d|\bforecast\b|\bhumidity\b|"
    r"\bheat\s+index\b|\buv\s+index\b|\bchance\s+of\s+rain\b|"
    # Qualitative claims are still claims: "a beautiful day, the air feels crisp"
    # is a statement about conditions it cannot see.
    r"\b(?:sunny|cloudy|rainy|muggy|humid|breezy|drizzl\w+|scorching|sweltering)\b|"
    r"\bthe\s+(?:heat|weather)\b",
    re.IGNORECASE,
)


def weather_claim_allowed(sentence: str, weather_injected: bool) -> bool:
    """False when a sentence asserts weather the model was never given."""
    return weather_injected or not _WEATHER_CLAIM_RE.search(sentence)


# Telling the model to stay inside the search results is not enough. Asked who
# won the World Cup it answered "Spain beat Argentina ... 62 million viewers
# across Fox and Telemundo" — none of which appeared in the snippets — and then
# admitted in the next sentence it had no results. So names and numbers get
# checked against the context that was actually supplied.
_CLAIM_PROPER_RE = re.compile(r"\b[A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+)*")
_CLAIM_NUMBER_RE = re.compile(r"\b\d[\d,]*(?:\.\d+)?%?\b")
# Openers and words that are capitalised for grammar, not because they're facts.
_CLAIM_SKIP = {
    "i", "a", "an", "the", "it", "we", "you", "they", "he", "she", "this", "that",
    "there", "here", "my", "your", "our", "if", "and", "but", "so", "no", "yes",
    "based", "according", "sorry", "okay", "ok", "well", "first", "next", "then",
    "however", "both", "either", "using", "from", "for", "as", "at", "in", "on",
    "while", "when", "since", "although", "though", "because", "despite", "given",
    "antigua", "today", "tomorrow", "yesterday",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}


def unsupported_claims(sentence: str, context: str) -> list:
    """Names/numbers asserted in `sentence` that are absent from `context`."""
    ctx = context.lower()
    ctx_digits = ctx.replace(",", "")
    ctx_tokens = set(re.findall(r"[a-z0-9']+", ctx))
    # The home city and household names come up unprompted; not claims.
    skip_local = set(household.people()["canon"]) | {
        w.lower() for w in settings.SEARCH_HOME_CITY.split()
    }
    bad = []
    for m in _CLAIM_PROPER_RE.finditer(sentence):
        for tok in m.group(0).split():
            low = tok.lower().strip(".,;:'’-")
            if len(low) < 3 or low in _CLAIM_SKIP or low in skip_local:
                continue
            # A shared 3-letter prefix with some word in the context tolerates
            # morphology — "Spain" in the snippets vs "Spanish" in the answer,
            # which share only "spa" — while "Telemundo" against a context with
            # no tel- word stays flagged. Leniency here is deliberate: dropping
            # a correct sentence is worse than letting a near-miss through.
            if low in ctx or (
                len(low) >= 5 and any(t.startswith(low[:3]) for t in ctx_tokens)
            ):
                continue
            bad.append(tok)
    for m in _CLAIM_NUMBER_RE.finditer(sentence):
        num = m.group(0).replace(",", "")
        if len(num.rstrip("%")) < 2 or num in ctx_digits:
            continue
        bad.append(m.group(0))
    return bad
