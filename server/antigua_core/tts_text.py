"""TTS text normalisation — shared by the primary and fallback servers.

Moved verbatim from antigua_server.py (Phase 2, antigua_core extraction).
"""

import re

from num2words import num2words

from . import household

# ── TTS text normalisation helpers ──────────────────────────────────────────

# Filler openers the LLM produces that sound awkward spoken aloud
# Each opener needs its own punctuation: a bare prefix match turned "Sorry"
# into "rry" and "Right now" into "now", and "Great. Ready…" into ". Ready…".
_FILLER_RE = re.compile(
    r"^(?:(?:Sure|Certainly|Of course|Absolutely|Great|Awesome|No problem"
    r"|Happy to help|Here's the thing|The thing is|Look|Right|So|Well)[,!]"
    r"|(?:Good question|Good point|Glad you asked)[!.,])\s*",
    re.IGNORECASE,
)

# Inline self-identification phrases the model inserts unprompted
_SELF_ID_RE = [
    re.compile(r",?\s+(?:as|because I'?m|since I'?m)\s+Antigua[^,.!?]*", re.IGNORECASE),
    re.compile(r",?\s+your\s+(?:friendly|helpful|local|home|offline)?\s*(?:voice\s+)?assistant", re.IGNORECASE),
    re.compile(r"\bI(?:'m| am)\s+Antigua[,.]?\s*", re.IGNORECASE),
    re.compile(r",?\s+as\s+your\s+(?:friendly\s+)?(?:voice\s+)?assistant", re.IGNORECASE),
]

# "8 PM" / "8pm" / "8 p.m." → "8 PM". Kokoro's phonemizer reads a bare AM/PM
# after a number as letters; the dotted form adds a pause at each dot
# ("eight P… M"). Requiring a digit leaves "I AM ready" alone.
_MERIDIEM_RE = re.compile(r"(\d)\s*([AaPp])(?:(\.)\s?[Mm]\.|[Mm])(?![A-Za-z])")


def _meridiem(m):
    out = f"{m.group(1)} {m.group(2).upper()}M"
    # A dotted "p.m." also swallowed any sentence-ending period — put it back.
    if m.group(3):
        rest = m.string[m.end():]
        if not rest.strip() or re.match(r"\s+[A-Z]", rest):
            out += "."
    return out


# Abbreviations → spoken form (order matters: longer/more-specific first)
_ABBREV = [
    (re.compile(r"\b(\d[\d.,]*)\s*mph\b", re.I), r"\1 miles per hour"),
    (re.compile(r"\b(\d[\d.,]*)\s*kph\b", re.I), r"\1 kilometers per hour"),
    (re.compile(r"\b(\d[\d.,]*)\s*km\b", re.I), r"\1 kilometers"),
    (re.compile(r"\b(\d[\d.,]*)\s*lbs?\b", re.I), r"\1 pounds"),
    (re.compile(r"\b(\d[\d.,]*)\s*kg\b", re.I), r"\1 kilograms"),
    (re.compile(r"\b(\d[\d.,]*)\s*°F\b"), r"\1 degrees Fahrenheit"),
    (re.compile(r"\b(\d[\d.,]*)\s*°C\b"), r"\1 degrees Celsius"),
    (re.compile(r"\b(\d[\d.,]*)\s*°\b"), r"\1 degrees"),
    (re.compile(r"\b(\d[\d.,]*)\s*ft\b", re.I), r"\1 feet"),
    (re.compile(r"\b(\d[\d.,]*)\s*cm\b", re.I), r"\1 centimeters"),
    (re.compile(r"\b(\d[\d.,]*)\s*mm\b", re.I), r"\1 millimeters"),
    (re.compile(r"\b(\d[\d.,]*)\s*ms\b", re.I), r"\1 milliseconds"),
    (re.compile(r"\b(\d[\d.,]*)\s*min\b", re.I), r"\1 minutes"),
    (re.compile(r"\b(\d[\d.,]*)\s*hrs?\b", re.I), r"\1 hours"),
    (re.compile(r"\b(\d[\d.,]*)\s*MB\b"), r"\1 megabytes"),
    (re.compile(r"\b(\d[\d.,]*)\s*GB\b"), r"\1 gigabytes"),
    (re.compile(r"\b(\d[\d.,]*)\s*TB\b"), r"\1 terabytes"),
    (re.compile(r"\b(\d[\d.,]*)\s*KB\b"), r"\1 kilobytes"),
    (re.compile(r"\betc\.(?=[\s,;]|$)", re.I), "etcetera"),
    (re.compile(r"\be\.g\.(?=[\s,]|$)", re.I), "for example"),
    (re.compile(r"\bi\.e\.(?=[\s,]|$)", re.I), "that is"),
    (re.compile(r"\bvs\.(?=\s|$)", re.I), "versus"),
    (re.compile(r"\bvs\b", re.I), "versus"),
    (re.compile(r"\bDr\.(?=\s)", re.I), "Doctor"),
    (re.compile(r"\bMr\.(?=\s)", re.I), "Mister"),
    (re.compile(r"\bMrs\.(?=\s)", re.I), "Missus"),
    (re.compile(r"\bMs\.(?=\s)", re.I), "Miss"),
    (re.compile(r"\bSt\.(?=\s[A-Z])"), "Saint"),
    (re.compile(r"\bAve\.(?=[\s,]|$)", re.I), "Avenue"),
    (re.compile(r"\bBlvd\.(?=[\s,]|$)", re.I), "Boulevard"),
    (_MERIDIEM_RE, _meridiem),
    (re.compile(r"(\d[\d.,]*)\s*%"), r"\1 percent"),
]

_MONTHS = ("January|February|March|April|May|June|July|August|September"
           "|October|November|December")
# "September 1" / "on May 3" → spoken ordinal. Catches every strftime("%-d")
# formatter output regardless of which module produced it.
_MONTH_DAY_RE = re.compile(rf"\b({_MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b")
# "$4.99" / "$4. 99" (search snippets sometimes split the cents) → dollars/cents
_CURRENCY_CENTS_RE = re.compile(r"[\$](\d[\d,]*)\.\s?(\d{2})\b")

# Numeric ranges and dimensions, spoken naturally. Run before unit expansion
# and number normalisation so "25-30 minutes" and "9x13 pan" don't come out
# as "twenty-five-thirty" / "9x13". Both sides must be short runs of digits so
# phone numbers and long IDs are left alone.
_RANGE_DIM = [
    (re.compile(r"\b24/7\b"), "twenty-four seven"),
    (re.compile(r"\b9-?1-?1\b"), "nine one one"),
    (re.compile(r"\b(\d{1,4})\s*[-–—]\s*(\d{1,4})\b"), r"\1 to \2"),
    (re.compile(r"\b(\d{1,3})\s*[xX×]\s*(\d{1,3})\b"), r"\1 by \2"),
]

# Common cooking fractions → words. Proper fractions only, small denominators,
# so dates ("9/11") and shorthand ("24/7") are untouched. Mixed numbers
# ("1 1/2 cups") are handled first by _MIXED_FRACTION_RE.
_FRACTION_WORDS = {
    (1, 2): "one half", (1, 3): "one third", (2, 3): "two thirds",
    (1, 4): "one quarter", (3, 4): "three quarters",
    (1, 5): "one fifth", (2, 5): "two fifths", (3, 5): "three fifths",
    (4, 5): "four fifths", (1, 6): "one sixth", (5, 6): "five sixths",
    (1, 8): "one eighth", (3, 8): "three eighths", (5, 8): "five eighths",
    (7, 8): "seven eighths", (1, 16): "one sixteenth",
}
_FRACTION_PLURAL = {
    (1, 2): "halves", (1, 4): "quarters", (3, 4): "three quarters",
}
_MIXED_FRACTION_RE = re.compile(r"\b(\d{1,3})\s+(\d{1,2})/(\d{1,2})\b")
_FRACTION_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")

# Insert a comma after common leading adverbs/conjunctions that lack one
_LEADING_CLAUSE_RE = re.compile(
    r"^(Well|So|Now|Actually|Honestly|Basically|Apparently|Fortunately"
    r"|Unfortunately|Interestingly|Notably|Also|However|Meanwhile"
    r"|Therefore|Thus|Hence|Indeed)\s+(?=[a-z])",
    re.IGNORECASE,
)
_PREPOSITIONAL_CLAUSE_RE = re.compile(
    r"^(As of [^,]{3,20}?|Based on [^,]{3,20}?|According to [^,]{3,20}?"
    r"|For now|At the moment|Right now|For context"
    r"|In short|In brief|In other words|To be clear|To summarize)\s+(?=[a-z])",
    re.IGNORECASE,
)


def _strip_filler(text):
    text = _FILLER_RE.sub("", text).lstrip()
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def _expand_abbreviations(text):
    for pat, repl in _RANGE_DIM:
        text = pat.sub(repl, text)
    for pat, repl in _ABBREV:
        text = pat.sub(repl, text)
    return text


def _fraction_phrase(n, d):
    if (n, d) in _FRACTION_WORDS:
        return _FRACTION_WORDS[(n, d)]
    if n < d and d in (2, 3, 4, 5, 6, 8, 10, 16):
        denom = {2: "half", 3: "third", 4: "quarter", 5: "fifth", 6: "sixth",
                 8: "eighth", 10: "tenth", 16: "sixteenth"}[d]
        return f"{num2words(n)} {denom}{'s' if n != 1 else ''}"
    return None


def _expand_fractions(text):
    def mixed_sub(m):
        n, num, den = int(m.group(1)), int(m.group(2)), int(m.group(3))
        frac = _fraction_phrase(num, den)
        if frac is None:
            return m.group(0)
        # "1 1/2" → "one and a half"; keep "one half" → "and one half" readable
        frac = {"one half": "a half", "one quarter": "a quarter"}.get(frac, frac)
        return f"{num2words(n)} and {frac}"

    def frac_sub(m):
        frac = _fraction_phrase(int(m.group(1)), int(m.group(2)))
        return frac if frac is not None else m.group(0)

    text = _MIXED_FRACTION_RE.sub(mixed_sub, text)
    text = _FRACTION_RE.sub(frac_sub, text)
    return text


def _add_leading_commas(text):
    text = _LEADING_CLAUSE_RE.sub(lambda m: m.group(1) + ", ", text)
    text = _PREPOSITIONAL_CLAUSE_RE.sub(lambda m: m.group(1) + ", ", text)
    return text


_CURRENCY_RE = re.compile(r"[\$£€](\d[\d,]*(?:\.\d+)?)")
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_ORDINAL_RE = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})\b")
_LARGE_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+)\b")
_DECIMAL_RE = re.compile(r"\b(\d+)\.(\d+)\b")
_PLAIN_RE = re.compile(r"\b(\d+)\b")
_CURRENCY_LABELS = {"$": "dollars", "£": "pounds", "€": "euros"}


def _year_to_words(y):
    if 2000 <= y <= 2009:
        rest = y - 2000
        return "two thousand" if rest == 0 else "two thousand " + num2words(rest)
    hi, lo = y // 100, y % 100
    if lo == 0:
        return num2words(hi) + " hundred"
    elif lo < 10:
        return num2words(hi) + " oh " + num2words(lo)
    return num2words(hi) + " " + num2words(lo)


def _normalize_numbers(text):
    def currency_sub(m):
        sym = m.group(0)[0]
        label = _CURRENCY_LABELS.get(sym, "dollars")
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw) if "." in raw else int(raw)
            return f"{num2words(val)} {label}"
        except Exception:
            return m.group(0)

    def time_sub(m):
        h = num2words(int(m.group(1)))
        mn = int(m.group(2))
        if mn == 0:
            return h
        elif mn < 10:
            return f"{h} oh {num2words(mn)}"
        return f"{h} {num2words(mn)}"

    def ord_sub(m):
        try:
            return num2words(int(m.group(1)), to="ordinal")
        except Exception:
            return m.group(0)

    def year_sub(m):
        return _year_to_words(int(m.group(1)))

    def large_sub(m):
        try:
            return num2words(int(m.group(0).replace(",", "")))
        except Exception:
            return m.group(0)

    def decimal_sub(m):
        int_part = num2words(int(m.group(1)))
        frac = " ".join(num2words(int(d)) for d in m.group(2))
        return f"{int_part} point {frac}"

    def plain_sub(m):
        try:
            return num2words(int(m.group(0)))
        except Exception:
            return m.group(0)

    text = _CURRENCY_RE.sub(currency_sub, text)
    text = _TIME_RE.sub(time_sub, text)
    text = _ORDINAL_RE.sub(ord_sub, text)
    text = _YEAR_RE.sub(year_sub, text)
    text = _LARGE_RE.sub(large_sub, text)
    text = _DECIMAL_RE.sub(decimal_sub, text)
    text = _PLAIN_RE.sub(plain_sub, text)
    return text


def _strip_self_id(text):
    for pat in _SELF_ID_RE:
        text = pat.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


# Emoji (and the joiners/selectors that glue them) — Kokoro either drops them
# or spells out "person running", and the small fallback model loves them.
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u200D\uFE0F]+")


def _strip_markdown(text):
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\n+", " ", text)


# ── Spanish (espeak es-419) ──────────────────────────────────────────────────
# espeak reads most Spanish numbers itself (years, percents, "26 de
# septiembre"); these are the forms it gets wrong: clock times ("12:58" →
# "doce: cincuenta y ocho"), "1 minuto" → "uno minuto", "$4.99", "°F" → "grados
# efe", "km/h" spelled out, English thousands commas (read as decimals), and
# stylized titles ("DeBÍ TiRAR MáS FOToS" spelled letter by letter).

_ES_MERIDIEM = r"(?:\s*([ap])\.?\s?m\.?(?![a-z]))?"
_ES_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b" + _ES_MERIDIEM, re.I)
_ES_UNITS = [
    (re.compile(r"\s*°\s*[FC]\b|\s*°"), " grados"),
    (re.compile(r"\b(\d[\d.,]*)\s*(?:km/h|kph)\b", re.I), r"\1 kilómetros por hora"),
    (re.compile(r"\b(\d[\d.,]*)\s*mph\b", re.I), r"\1 millas por hora"),
    (re.compile(r"\b(\d[\d.,]*)\s*km\b", re.I), r"\1 kilómetros"),
]
_ES_THOUSANDS_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
_ES_DOLLARS_RE = re.compile(r"\$\s?(\d+)(?:\.(\d{2}))?\b")
_ES_DECIMAL_RE = re.compile(r"\b(\d+)\.(\d+)\b")
_ES_FIRST_RE = re.compile(r"\b1\s+de\s+(?=enero|febrero|marzo|abril|mayo|junio|julio"
                          r"|agosto|septiembre|octubre|noviembre|diciembre)", re.I)
# A number ending in 1 before a noun: "1 minuto" → "un", "21 grados" →
# "veintiún", "1 hora" → "una".
_ES_ONE_NOUN_RE = re.compile(r"\b(\d*1)\s+([a-záéíóúñ]+)", re.I)
_ES_MASC_A = {"día", "días", "mapa", "problema", "clima", "programa", "tema", "sistema"}
_ES_STYLIZED_RE = re.compile(r"\b\w*[a-záéíóúñ]\w*\b")


def _es_num(n):
    return num2words(n, lang="es")


def _es_feminine(noun):
    noun = noun.lower()
    return noun not in _ES_MASC_A and noun.endswith(("a", "as", "ión", "iones", "dad", "dades"))


def _es_time(m):
    h, mn, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    hour = "una" if h in (1, 13) else _es_num(h % 12 if ap and h > 12 else h)
    words = hour + {0: "", 15: " y cuarto", 30: " y media"}.get(mn, f" y {_es_num(mn)}")
    if ap == "a":
        words += " de la mañana"
    elif ap == "p":
        h12 = h % 12
        words += " del mediodía" if h12 == 0 else " de la tarde" if h12 < 7 else " de la noche"
    return words


def _es_one_noun(m):
    num, noun = m.group(1), m.group(2)
    n = int(num)
    if n % 100 == 11 or noun.lower() in ("de", "y", "o", "a", "por", "para"):
        return m.group(0)
    words = _es_num(n)
    if _es_feminine(noun):
        words = re.sub(r"uno$", "una", words)
    else:
        words = re.sub(r"veintiuno$", "veintiún", words)
        words = re.sub(r"uno$", "un", words)
    return f"{words} {noun}"


def _es_destylize(m):
    """Mixed case inside a word ("FOToS") makes espeak spell it; all-caps
    acronyms (NASA, TV) and normal capitalized words are left alone."""
    w = m.group(0)
    return w.lower() if any(c.isupper() for c in w[1:]) else w


def _es_dollars(m):
    d, c = int(m.group(1)), int(m.group(2) or 0)
    out = "un dólar" if d == 1 else f"{_es_num(d)} dólares"
    return out + (f" con {_es_num(c)} centavos" if c else "")


def _clean_spanish(text):
    text = _strip_markdown(text)
    text = _ES_STYLIZED_RE.sub(_es_destylize, text)
    text = _ES_TIME_RE.sub(_es_time, text)
    for pat, repl in _ES_UNITS:
        text = pat.sub(repl, text)
    text = _ES_DOLLARS_RE.sub(_es_dollars, text)
    text = _ES_THOUSANDS_RE.sub(lambda m: m.group(0).replace(",", ""), text)
    text = _ES_DECIMAL_RE.sub(lambda m: f"{_es_num(int(m.group(1)))} punto "
                              + " ".join(_es_num(int(d)) for d in m.group(2)), text)
    text = _ES_FIRST_RE.sub("primero de ", text)
    text = _ES_ONE_NOUN_RE.sub(_es_one_noun, text)
    return re.sub(r"\s{2,}", " ", re.sub(r"\.\.+", ".", text)).strip()


def clean_for_tts(text, lang="en"):
    """Strip markdown, normalize numbers/abbreviations/pacing for Kokoro.
    Everything past the markdown strip is English-specific (num2words,
    abbreviations, the "Antigwa"/"No-ee" phonetic fixes); Spanish has its own
    smaller pass (_clean_spanish) — espeak es-419 reads most digits itself."""
    if lang == "es":
        return _clean_spanish(text)
    if lang != "en":
        return re.sub(r"\.\.+", ".", _strip_markdown(text)).strip()
    # Strip LLM filler openers
    text = _strip_filler(text)
    # Strip inline self-identification phrases
    text = _strip_self_id(text)
    text = _strip_markdown(text)
    # Expand abbreviations before number normalisation
    text = _expand_abbreviations(text)
    # Add natural pause commas after leading clauses
    text = _add_leading_commas(text)
    # Phonetic fix: Kokoro says "Ant-ee-guh" (Caribbean island); we want
    # "Ant-tee-gwah" (Guatemalan city). "Antigwa" gets the right vowel.
    text = re.sub(r"\bAntigua\b", "Antigwa", text)
    text = re.sub(r"\bantigua\b", "antigwa", text)
    # Household names Kokoro mispronounces (household.say_as in server.yaml).
    text = household.respell_for_tts(text)
    text = _expand_fractions(text)
    text = _MONTH_DAY_RE.sub(
        lambda m: f"{m.group(1)} {num2words(int(m.group(2)), to='ordinal')}", text
    )
    def _cents_sub(m):
        dollars = num2words(int(m.group(1).replace(",", "")))
        if m.group(2) == "00":
            return f"{dollars} dollars"
        return f"{dollars} dollars and {num2words(int(m.group(2)))} cents"
    text = _CURRENCY_CENTS_RE.sub(_cents_sub, text)
    text = _normalize_numbers(text)
    # Collapse stray period runs ("etc.." etc.).
    text = re.sub(r"\.\.+", ".", text)
    return text.strip()
