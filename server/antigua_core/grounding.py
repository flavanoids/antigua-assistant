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
    # Common sentence openers: capitalised for grammar, never a name.
    "before", "after", "during", "later", "earlier", "also", "still", "even", "with",
    "without", "among", "besides", "currently", "eventually", "meanwhile", "throughout",
    "beyond", "within", "across", "along", "around", "through", "under", "over",
    "until", "upon", "into", "about", "above", "against", "between", "toward",
    "towards", "unlike", "like", "following", "including", "known", "born", "raised",
    "growing", "today's", "together", "overall", "additionally", "furthermore",
    "moreover", "instead", "otherwise", "therefore", "thus", "hence", "indeed",
    "notably", "famously", "originally", "initially", "ultimately", "finally",
    "afterward", "afterwards", "soon", "once", "only", "just", "perhaps", "sadly",
    "tragically", "unfortunately", "fortunately", "interestingly", "similarly",
    "likewise", "specifically", "particularly", "especially", "mostly", "mainly",
    "primarily", "largely", "generally", "officially", "publicly", "privately",
    "personally", "professionally", "politically", "musically", "early", "late",
    "many", "most", "some", "several", "few", "each", "every", "all", "these",
    "those", "such", "other", "another", "his", "her", "their", "its", "she's",
    "he's", "they're", "it's", "what", "which", "who", "whose", "where", "why",
    "how", "not",
    "antigua", "today", "tomorrow", "yesterday",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    # Spanish sentence openers — a grounded Spanish answer starts "Aunque fue…"
    "aunque", "fue", "era", "fueron", "eran", "nació", "murió", "sin", "con", "tras",
    "durante", "después", "antes", "además", "también", "sus", "los", "las", "una",
    "del", "por", "para", "cuando", "como", "pero", "ella", "ellos", "este", "esta",
    "hoy", "mañana", "ayer", "según", "sí",
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
            # "He's" / "She'll" are pronouns, not names — drop the contraction.
            low = re.sub(r"['’](?:s|re|ll|d|ve|m)$", "", tok.lower().strip(".,;:'’-"))
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


# ── Recipe Q&A ─────────────────────────────────────────────────────────────────
# A recipe answer may add general kitchen technique, but never an amount, time or
# temperature the recipe doesn't state. unsupported_claims misses single digits
# and spelled-out numbers ("bake 5 more minutes", "about five minutes"), so recipe
# mode also runs recipe_claims_ok: every number in a sentence, digits or words,
# must be backed by the context, and every <number> <unit> phrase must appear
# there with the same value ("5 minutes" fails when the recipe says 30).

_QA_NUM = r"\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?"
# No bare "in" (a preposition); "g"/"oz"/"lb" only pair after a number.
_QA_UNITS = [
    r"degrees?(?:\s*[fc])?", r"minutes?", r"mins?", r"hours?", r"hrs?",
    r"cups?", r"tablespoons?", r"tbsps?", r"tbsp", r"teaspoons?", r"tsps?",
    r"tsp", r"ounces?", r"oz", r"pounds?", r"lbs?", r"lb", r"grams?", r"g",
    r"inches?", r"inch",
]
_QA_UNIT_CANON = {
    "degree": "degree", "degrees": "degree", "degree f": "degree",
    "degrees f": "degree", "degree c": "degree", "degrees c": "degree",
    "minute": "minute", "minutes": "minute", "min": "minute", "mins": "minute",
    "hour": "hour", "hours": "hour", "hr": "hour", "hrs": "hour",
    "cup": "cup", "cups": "cup",
    "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsp": "tbsp", "tbsps": "tbsp",
    "teaspoon": "tsp", "teaspoons": "tsp", "tsp": "tsp", "tsps": "tsp",
    "ounce": "ounce", "ounces": "ounce", "oz": "ounce",
    "pound": "pound", "pounds": "pound", "lb": "pound", "lbs": "pound",
    "gram": "gram", "grams": "gram", "g": "gram",
    "inch": "inch", "inches": "inch",
}
# "5 more minutes", "about 30 minutes": a short qualifier may sit between the
# number and its unit, and the pair still has to match the context.
_QA_QUAL = r"\s*(?:more|about|another|approximately|roughly|around|extra|fewer|less|additional)\s+"
_QA_PAIR_RE = re.compile(
    r"(" + _QA_NUM + r")(?:[\s-]*|" + _QA_QUAL + r")(" + "|".join(_QA_UNITS) + r")\b")
_QA_WORD_NUMS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12,
}
# Longest first so "seventeen" wins over its prefix "seven".
_QA_WORD_NUM_RE = re.compile(
    r"\b(" + "|".join(sorted(_QA_WORD_NUMS, key=len, reverse=True)) + r")\b")
_QA_COMPOUND_RE = re.compile(
    r"\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)[- ]"
    r"(one|two|three|four|five|six|seven|eight|nine)\b")
# Fractions said in words, longest first. "a half" after "and a half" is handled.
_QA_FRAC_WORDS = [
    (r"\bseven[- ]eighths?\b", " 0.875 "),
    (r"\bfive[- ]eighths?\b", " 0.625 "),
    (r"\bthree[- ]eighths?\b", " 0.375 "),
    (r"\bthree[- ]quarters?\b", " 0.75 "),
    (r"\btwo[- ]thirds?\b", " 0.667 "),
    (r"\b(?:an?|one)[- ]hal(?:f|ves)\b", " 0.5 "),
    (r"\b(?:an?|one)[- ]quarters?\b", " 0.25 "),
    (r"\b(?:an?|one)[- ]thirds?\b", " 0.333 "),
    (r"\b(?:an?|one)[- ]eighths?\b", " 0.125 "),
]
# "one and a half" etc. — before "a half" becomes 0.5 on its own.
_QA_AND_HALF_RE = re.compile(
    r"\b(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve)\s+and\s+a\s+half\b")
_QA_STEP_REF_RE = re.compile(r"\bstep\s+(\d+)\b|\b(\d+)\s+steps?\b")


def _qa_value(tok: str) -> float:
    """"1 1/2" → 1.5, "2/3" → 0.667, "30" → 30.0."""
    tok = tok.strip()
    if " " in tok:  # mixed number
        whole, _, frac = tok.partition(" ")
        n, d = frac.split("/")
        return round(float(whole) + int(n) / int(d), 3)
    if "/" in tok:
        n, d = tok.split("/")
        return round(int(n) / int(d), 3)
    return round(float(tok), 3)


def _qa_and_half(m) -> str:
    v = _QA_WORD_NUMS.get(m.group(1))
    return f" {(v if v is not None else float(m.group(1))) + 0.5:g} "


def _qa_normalize(text: str) -> str:
    """Both the sentence and the context go through the same rewrite, so
    "one and a half cups" can match "1 1/2 cups" and "325 degrees" matches 325°F."""
    s = text.lower()
    s = s.replace("&deg;", " degrees ").replace("°", " degrees ")
    s = re.sub(r"\ban\s+hour\s+and\s+a\s+half\b", " 1.5 hours ", s)
    s = _QA_AND_HALF_RE.sub(_qa_and_half, s)
    for pat, rep in _QA_FRAC_WORDS:
        s = re.sub(pat, rep, s)
    s = re.sub(r"\b(?:an?|one)\s+hundred\b", " 100 ", s)
    s = _QA_COMPOUND_RE.sub(
        lambda m: f" {_QA_WORD_NUMS[m.group(1)] + _QA_WORD_NUMS[m.group(2)]} ", s)
    s = _QA_WORD_NUM_RE.sub(lambda m: f" {_QA_WORD_NUMS[m.group(1)]} ", s)
    s = _QA_AND_HALF_RE.sub(_qa_and_half, s)  # "2 and a half" once digits
    s = re.sub(r"(\d+(?:\.\d+)?)\s+hours?\s+and\s+a\s+half\b",
               lambda m: f" {float(m.group(1)) + 0.5:g} hours ", s)
    s = re.sub(r"\ban\s+hour\b", " 1 hour ", s)
    s = re.sub(r"\bhalf\b", " 0.5 ", s)
    # "add them one at a time" claims nothing.
    s = re.sub(r"\b1\s+at\s+a\s+time\b", "at a time", s)
    return s


def _qa_scan(text: str):
    """(number values, {(<value>, <canonical unit>)}) found in text."""
    nums = [_qa_value(m.group(0)) for m in re.finditer(_QA_NUM, text)]
    pairs = set()
    for m in _QA_PAIR_RE.finditer(text):
        unit = _QA_UNIT_CANON.get(m.group(2).strip(), m.group(2).strip())
        pairs.add((_qa_value(m.group(1)), unit))
    return nums, pairs


def recipe_claims_ok(sentence: str, context: str) -> bool:
    """True when every number in `sentence` is backed by the recipe `context`.

    A "step 3" reference is allowed while the recipe has a step 3; the numbered
    step prefixes themselves ("1. ") are structure, not facts to hide behind.
    """
    s = _qa_normalize(sentence)
    ctx = _qa_normalize(context)
    n_steps = len(re.findall(r"(?m)^\s*\d+[.)]\s", ctx))
    ctx = re.sub(r"(?m)^\s*\d+[.)]\s", " ", ctx)
    s_nums, s_pairs = _qa_scan(s)
    c_nums, c_pairs = _qa_scan(ctx)
    step_refs = set()
    for m in _QA_STEP_REF_RE.finditer(s):
        step_refs.add(int(m.group(1) or m.group(2)))
    # A step reference past the last step is a claim about nothing.
    s_nums = [v for v in s_nums if v not in step_refs or v > n_steps]
    if not all(v in c_nums for v in s_nums):
        return False
    return s_pairs <= c_pairs
