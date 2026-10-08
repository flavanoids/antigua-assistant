"""Recipe finder: real recipes from the web, never written by the model.

Recipe sites embed schema.org Recipe JSON-LD for search engines: one line per
ingredient with its amount, the steps, times, servings and ratings. This
module searches SearXNG for "<dish> recipe", fetches the top pages in
parallel, keeps the ones that carry that data, ranks them and turns them into
text that reads well aloud. Nothing here is generated; if no page has a
recipe, find() returns [] and Antigua says so (hard rule, see
docs/recipe_skill_plan.md).
"""

import html as _html
import json
import logging
import math
import re
import time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

import requests

from . import settings

log = logging.getLogger("antigua_core")

# A plain browser request: several recipe blogs refuse anything else.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
}
# Pages that never carry a recipe's structured data.
_SKIP_HOSTS = re.compile(r"(?:^|\.)(?:youtube|youtu|pinterest|reddit|tiktok|facebook|instagram|"
                         r"twitter|x|amazon|wikipedia|quora)\.", re.IGNORECASE)
# A step longer than this is spoken in parts (step_parts); the recipe's own
# step numbering stays. Before 2026-10-06 long steps became separate steps.
_PART_IF_WORDS = 30
_PART_IF_SENTENCES = 2
_PART_NEVER_WORDS = 20   # three short sentences are still one breath
_PART_MAX_WORDS = 25
_PART_SHORT = 6          # "Mix well." rides along with its neighbour
_ENOUGH = 5          # parsed recipes worth waiting for before answering

_LDJSON_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
_SITE_NAME_RE = re.compile(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_ISO_DUR_RE = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", re.I)
_UNICODE_FRACTIONS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4",
                      "⅕": "1/5", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8", "⅙": "1/6"}
_UNITS = [
    (r"tbsps?|tbs|tbl|T(?=\s)", "tablespoon"), (r"tsps?|t(?=\s)", "teaspoon"),
    (r"oz", "ounce"), (r"lbs?", "pound"), (r"g|grams?", "gram"), (r"kg", "kilogram"),
    (r"ml|mL", "milliliter"), (r"l|L", "liter"), (r"pkgs?", "package"), (r"c(?=\s)", "cup"),
    (r"qt", "quart"), (r"pt", "pint"), (r"gal", "gallon"),
]
_QTY = r"(?P<q>\d+(?:\s+\d+/\d+|/\d+|\.\d+)?)"
_UNIT_RES = [(re.compile(_QTY + r"\s*(?:" + pat + r")\.?(?=[\s,)]|$)"), word) for pat, word in _UNITS]
# Metric or blog-chatter parentheticals that only clutter speech.
_DROP_PAREN_RE = re.compile(r"\s*\((?:[^()]*\b(?:grams?|g|ml|mL|cm|mm|kg|°C|C|liters?|see\s+note|notes?|"
                            r"affiliate|link|I\s+(?:use|used|like|recommend|prefer)|about|from|such\s+as|"
                            r"at\s+room\s+temperature|room\s+temp)\b[^()]*|[^()]*\d+\s*°?\s*C\b[^()]*)\)",
                            re.IGNORECASE)
_EQUIPMENT_RE = re.compile(
    r"\b(?P<size>\d+(?:\s*(?:or|to|-)\s*\d+)?(?:-|\s)?(?:inch|in\.?|\")\s+)?"
    r"(?P<tool>springform\s+pan|bundt\s+pan|loaf\s+pan|tube\s+pan|cake\s+pans?|pie\s+(?:dish|plate|pan)|"
    r"tart\s+pan|muffin\s+(?:tin|pan)|baking\s+dish|casserole\s+dish|sheet\s+pan|baking\s+sheet|"
    r"dutch\s+oven|stock\s*pot|stand\s+mixer|hand\s+mixer|electric\s+mixer|food\s+processor|blender|"
    r"slow\s+cooker|instant\s+pot|pressure\s+cooker|cast[- ]iron\s+(?:skillet|pan)|roasting\s+pan|"
    r"(?:candy|meat|instant[- ]read)\s+thermometer|rolling\s+pin|waffle\s+iron|air\s+fryer)\b",
    re.IGNORECASE)
_NOT_INGREDIENT_RE = re.compile(r"^(?:special\s+)?(?:equipment|tools?|you(?:'ll|\s+will)\s+need)\b", re.I)
_WAIT_RE = re.compile(
    r"\b(?P<verb>chill|refrigerate|freeze|rise|marinate|rest|set\s+up|soak|cool)\w*\b[^.]{0,60}?"
    r"\b(?P<span>overnight|(?:at\s+least\s+)?\d+(?:\s*(?:to|-|or)\s*\d+)?\s*hours?)",
    re.IGNORECASE)


@dataclass
class Recipe:
    title: str
    source: str
    url: str
    ingredients: list                     # speakable lines, in order
    groups: list = field(default_factory=list)   # [[name, first_idx, last_idx]]
    steps: list = field(default_factory=list)    # speakable, split for listening
    servings: str = ""
    total_min: int = 0
    rating: float = 0.0
    rating_count: int = 0
    equipment: list = field(default_factory=list)
    wait: str = ""                        # "chill overnight" / "chill at least 4 hours"
    yield_n: float = 0.0                  # base servings number, 0 when unknown
    yield_hi: float = 0.0                 # high end of "serves 8 to 10"
    yield_unit: str = ""                  # "loaf" for "makes 1 loaf": servings scaling off
    amounts: list = field(default_factory=list)   # parse_amount per ingredient line
    kind: str = "recipe"                  # "howto": a household guide (howto.py), supplies as ingredients
    caution: str = ""                     # howto: the fixed caution line, "" when nothing's risky

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Recipe":
        r = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        # Recipes cached before amounts/yield existed: parse them now —
        # the speakable lines carry everything parse_amount needs.
        if not r.amounts or len(r.amounts) != len(r.ingredients):
            r.amounts = [parse_amount(i) for i in r.ingredients]
        if "yield_n" not in d:
            r.yield_n, r.yield_hi, r.yield_unit = parse_yield(r.servings)
        return r

    def size(self) -> int:
        return len(self.ingredients) + len(self.steps)


# ── Text ─────────────────────────────────────────────────────────────────────


def _text(value) -> str:
    """HTML-ish string -> plain text, mojibake and all."""
    s = str(value or "")
    if "Â" in s or "â€" in s:
        try:
            s = s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            s = s.replace("Â", "")
    s = _html.unescape(_TAG_RE.sub(" ", _html.unescape(s)))
    for u, a in _UNICODE_FRACTIONS.items():
        s = re.sub(r"(\d)\s*" + u, r"\1 " + a, s).replace(u, a)
    s = s.replace(" ", " ").replace(" ", " ").replace("⁄", "/")
    return re.sub(r"\s+", " ", s).strip()


_FRACTIONS = [(0, ""), (1 / 8, "1/8"), (1 / 6, "1/6"), (1 / 4, "1/4"), (1 / 3, "1/3"), (3 / 8, "3/8"),
              (1 / 2, "1/2"), (5 / 8, "5/8"), (2 / 3, "2/3"), (3 / 4, "3/4"), (5 / 6, "5/6"),
              (7 / 8, "7/8"), (1, "")]


def _decimal_fraction(m) -> str:
    """Allrecipes writes 2.3333332538605 cups; say 2 1/3."""
    value = float(m.group(0))
    whole = int(value)
    frac = value - whole
    near = min(_FRACTIONS, key=lambda f: abs(f[0] - frac))
    if abs(near[0] - frac) > 0.03:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if near[0] == 1:
        whole += 1
    if not near[1]:
        return str(whole)
    return f"{whole} {near[1]}" if whole else near[1]


def _decimals(s: str) -> str:
    return re.sub(r"(?<![\d.])\d*\.\d+(?![\d.])", _decimal_fraction, s)


def _plural(word: str, qty: str) -> str:
    return word if qty.strip() in ("1",) or re.fullmatch(r"\d+/\d+", qty.strip()) else word + "s"


def _units(s: str) -> str:
    for rx, word in _UNIT_RES:
        s = rx.sub(lambda m, w=word: f"{m.group('q')} {_plural(w, m.group('q'))}", s)
    return s


_PAREN_RE = re.compile(r"\s*\(([^()]*)\)")
_SIZE_PAREN_RE = re.compile(r"^\s*\d[\d./ ]*\s*-?\s*(?:ounces?|oz|inch|pounds?|lbs?)\.?\s*$", re.I)


def _paren_ingredient(m) -> str:
    """'(8 ounce)' stays, '(melted)' becomes ', melted', grams and chatter go."""
    inner = m.group(1).strip()
    if _SIZE_PAREN_RE.match(inner):
        return f" \x00{inner}\x01"     # kept; brackets restored after the loop
    if (inner and len(inner.split()) <= 3 and not re.search(r"\d", inner)
            and not re.search(r"\b(?:optional|see|note|room\s+temp)", inner, re.I)):
        return f", {inner}"
    return ""


def speakable_ingredient(line: str) -> str:
    s = _text(line)
    s = _decimals(re.sub(r"(\d)-(\d+/\d+)", r"\1 \2", s))   # "2-1/2 pounds", "0.25 teaspoon"
    optional = bool(re.search(r"\boptional\b", s, re.I))
    s = re.sub(r"^optional\s*:\s*", "", s, flags=re.I)
    while _PAREN_RE.search(s):          # innermost first: "(room temp (use more))"
        s = _PAREN_RE.sub(_paren_ingredient, s)
    s = s.replace("(", "").replace(")", "").replace("\x00", "(").replace("\x01", ")")
    s = _units(s)
    s = re.sub(r"\s+,", ",", re.sub(r"\s+", " ", s)).strip(" ,;")
    s = re.sub(r"(?:,\s*)+,", ",", s)
    if optional and not re.search(r"\boptional\b", s, re.I):
        s += ", optional"
    return s


def speakable_step(text: str) -> str:
    s = _text(text)
    s = _decimals(re.sub(r"(\d)-(\d+/\d+)", r"\1 \2", s))
    s = re.sub(r"(\d)\s*degrees?\s*\(?F\)?", r"\1 degrees", s)
    s = re.sub(r"(\d)\s*°\s*F\b", r"\1 degrees", s)
    s = _DROP_PAREN_RE.sub("", s)
    s = re.sub(r"\(([A-Z][^()]*[.!?])\)", r"\1", s)      # a whole sentence in brackets
    s = re.sub(r"(\d+)\s*(?:-|to)\s*(\d+)(?:-|\s)?in\.?\b", r"\1 to \2 inch", s)
    s = re.sub(r"(\d+)(?:-|\s)?in\.?(?=\s)", r"\1 inch", s)
    s = re.sub(r'(\d+)"', r"\1 inch", s)
    s = _units(s)
    return re.sub(r"\s+([,.;])", r"\1", re.sub(r"\s+", " ", s)).strip()


_PREP_CLAUSE_RE = re.compile(
    r",\s*(?:(?:finely|roughly|coarsely|thinly|freshly|lightly|well)\s+)?(?:chopped|minced|diced|sliced|"
    r"melted|softened|grated|shredded|peeled|crushed|cubed|beaten|drained|rinsed|trimmed|halved|quartered|"
    r"julienned|zested|juiced|cut\s+into\s+[^,]+|at\s+room\s+temperature|room\s+temperature|"
    r"to\s+taste|divided(?:\s+use)?|(?:plus\s+|or\s+)?more[^,]*|packed|sifted|cooled|warmed|uncooked|"
    r"taco\s+size|for\s+(?:the\s+)?\w+)(?:\s+and\s+\w+)?\b",
    re.IGNORECASE)


def listed(line: str) -> str:
    """An ingredient as read in the list: amount and name, no prep notes
    ("1 large onion, chopped" -> "1 large onion"). "How much" answers and
    the steps keep the full line."""
    s = _PREP_CLAUSE_RE.sub("", line)
    s = re.sub(r"\b([a-z]+)/([a-z]+)\b", r"\1 or \2", s)
    s = re.sub(r"\s*,\s*(?=,|$)", "", s).strip(" ,")
    return s or line


# ── Scaling servings ──────────────────────────────────────────────────────────
# Pure arithmetic on the published amounts (docs/recipe_questions_and_scaling_
# plan.md §2). Times, temperatures and pan sizes are never scaled.


_AMT_NUM = r"\d+(?:\s+(?:and\s+)?\d+/\d+)?|\d+/\d+|\d+\.\d+"
_UNIT_WORDS = ("cups?|tablespoons?|teaspoons?|ounces?|pounds?|grams?|kilograms?|"
               "milliliters?|liters?|litres?|quarts?|pints?|packages?|cans?|cloves?|"
               "sticks?|pinch(?:es)?|dashes?|slices?|bunch(?:es)?|heads?|sprigs?|"
               "leaves?|ribs?|ears?|strips?|wedges?")
_AMOUNT_RE = re.compile(
    r"^(?:"
    r"(?P<qty>" + _AMT_NUM + r")(?=\s|\(|$)"                      # "4 cloves", "1 to 2 cups"
    r"(?:\s*(?:to|-|–|or)\s*(?P<hi>" + _AMT_NUM + r"))?"
    r"|(?P<mwhole>\d+)-(?P<mfrac>\d+/\d+)(?=\s|\(|$)"             # "1-1/2 cups": one and a half
    r"|(?P<qty2>\d+(?:\.\d+)?)\s*[-–]\s*(?P<hi2>\d+(?:\.\d+)?)(?=\s|\(|$)"  # "2-3 pounds"
    r")"
    r"(?:\s*\((?P<paren>[^)]*)\)\s*)?"
    r"(?:\s*(?P<unit>" + _UNIT_WORDS + r"))?"
    r"\s*(?P<rest>.*)$", re.IGNORECASE)
_UNIT_CANON = {"cups": "cup", "tablespoons": "tablespoon", "teaspoons": "teaspoon",
               "ounces": "ounce", "pounds": "pound", "grams": "gram", "kilograms": "kilogram",
               "milliliters": "milliliter", "liters": "liter", "litres": "liter",
               "quarts": "quart", "pints": "pint", "packages": "package", "cans": "can",
               "cloves": "clove", "sticks": "stick", "pinches": "pinch", "dashes": "dash",
               "slices": "slice", "bunches": "bunch", "heads": "head", "sprigs": "sprig",
               "leaves": "leaf", "ribs": "rib", "ears": "ear", "strips": "strip",
               "wedges": "wedge"}
# Measure units only: step amounts in these are candidates for scaling;
# inches, minutes and degrees never are.
MEASURE_UNITS = {"cup", "tablespoon", "teaspoon", "ounce", "pound", "gram", "kilogram",
                 "milliliter", "liter", "quart", "pint"}
# Count plurals for "make it for 1": "large eggs" -> "large egg".
_SINGULARS = {"eggs": "egg", "cloves": "clove", "cans": "can", "packages": "package",
              "sticks": "stick", "leaves": "leaf", "sprigs": "sprig", "ribs": "rib",
              "carrots": "carrot", "onions": "onion", "bananas": "banana",
              "potatoes": "potato", "tomatoes": "tomato", "slices": "slice",
              "thighs": "thigh", "breasts": "breast", "fillets": "fillet", "ears": "ear",
              "wedges": "wedge", "strips": "strip", "anchovies": "anchovy",
              "cherries": "cherry", "peppers": "pepper", "mushrooms": "mushroom",
              "loaves": "loaf", "chickens": "chicken", "steaks": "steak"}


def _to_f(text: str) -> float:
    """'1 1/4' / '1 and 1/2' / '0.75' / '1/2' -> float."""
    parts = re.sub(r"\s+and\s+", " ", text.strip()).split()
    if len(parts) == 2:
        whole, num, den = int(parts[0]), *parts[1].split("/")
        return whole + int(num) / int(den)
    if "/" in parts[0]:
        num, den = parts[0].split("/")
        return int(num) / int(den)
    return float(parts[0])


def _canon_unit(word: str) -> str:
    w = word.lower()
    return _UNIT_CANON.get(w, w)


def parse_amount(line: str) -> dict:
    """The leading amount of an ingredient line: {"qty", "qty_hi", "unit",
    "paren", "rest"}. qty None means the line is never scaled ("salt and
    pepper, to taste"; "8-ounce can", where the 8 is an adjective).
    Ranges ("1 to 2", "2-3", "2 or 3") and hyphenated mixed numbers
    ("1-1/2") both parse. "(8 ounce)" in `paren` is kept but never scaled."""
    m = _AMOUNT_RE.match(line.strip())
    if not m or not (m.group("qty") or m.group("mwhole") or m.group("qty2")):
        return {"qty": None, "qty_hi": None, "unit": None, "paren": "", "rest": line}
    qty = (_to_f(m.group("qty")) if m.group("qty") else
           float(m.group("mwhole")) + _to_f(m.group("mfrac")) if m.group("mwhole") else
           float(m.group("qty2")))
    hi = (_to_f(m.group("hi")) if m.group("hi") else
          float(m.group("hi2")) if m.group("hi2") else None)
    return {"qty": qty,
            "qty_hi": hi,
            "unit": _canon_unit(m.group("unit")) if m.group("unit") else None,
            "paren": m.group("paren") or "",
            "rest": (m.group("rest") or "").strip()}


def parse_yield(servings: str) -> tuple:
    """("serves 12") -> (12, 0, ""); ("serves 8 to 10") -> (8, 10, "");
    ("makes 1 loaf") -> (1, 0, "loaf"); "" -> (0, 0, "")."""
    m = re.match(r"^serves (\d+)(?: to (\d+))?$", servings or "")
    if m:
        return float(m.group(1)), float(m.group(2) or 0), ""
    m = re.match(r"^makes (\d+(?:\.\d+)?)\s+(.+?)\s*$", servings or "")
    if m and not re.search(r"\d", m.group(2)):
        return float(m.group(1)), 0.0, m.group(2)
    return 0.0, 0.0, ""


def fmt_qty(v: float) -> str:
    """A scaled amount as people say it: 2.5 -> "2 1/2", 0.333 -> "1/3"."""
    whole = math.floor(v)
    frac = v - whole
    near = min(_FRACTIONS, key=lambda f: abs(f[0] - frac))
    if abs(near[0] - frac) <= 0.05:
        if near[0] == 1:
            whole += 1
        frac_s = "" if near[0] in (0, 1) else near[1]
    else:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if frac_s:
        return f"{whole} {frac_s}" if whole else frac_s
    return str(whole)


def _tidy_unit(qty: float, unit: str) -> tuple:
    """3 teaspoons -> 1 tablespoon, 16 tablespoons -> 1 cup, 16 ounces ->
    1 pound, and the small-direction equivalents. (qty, unit) back."""
    if unit == "teaspoon" and qty >= 2.9 and abs(qty / 3 - round(qty / 3)) < 0.06:
        qty, unit = round(qty / 3), "tablespoon"
    if unit == "tablespoon" and qty >= 3.9 and abs(qty / 4 - round(qty / 4)) < 0.06:
        qty, unit = qty / 16, "cup"
    if unit == "cup" and qty <= 0.13:
        return qty * 16, "tablespoon"
    if unit == "ounce" and qty >= 15.9 and abs(qty / 16 - round(qty / 16)) < 0.06:
        return qty / 16, "pound"
    return qty, unit


def _singular(rest: str) -> str:
    """"large eggs" -> "large egg", for a count that scaled down to one."""
    words = rest.split()
    for i, w in enumerate(words[:3]):
        s = _SINGULARS.get(w.strip(",").lower())
        if s:
            words[i] = w.replace(w.strip(","), s)
            return " ".join(words)
    return rest


_PLURAL_WORD = {v: k for k, v in _SINGULARS.items()}    # egg -> eggs


def _plural_rest(rest: str) -> str:
    """"large onion, chopped" -> "large onions, chopped": a count scaled up
    from one needs its noun plural. Only words we know the plural of;
    anything else is left as the recipe wrote it."""
    head, sep, tail = rest.partition(",")
    words = head.split()
    for i in range(len(words) - 1, -1, -1):
        w = words[i].lower().strip(".")
        if w in _SINGULARS:          # already plural ("4 garlic cloves")
            return rest
        if w in _PLURAL_WORD:
            words[i] = words[i].replace(words[i].strip("."), _PLURAL_WORD[w])
            return " ".join(words) + sep + tail
    return rest


# Units that are whole things: they round when scaled, they don't take
# fractions the way cups do.
COUNT_UNITS = {"package", "can", "clove", "stick", "slice", "sprig", "leaf",
               "rib", "head", "bunch", "ear", "strip", "wedge", "pinch", "dash"}


def _round_count(qty: float, unit: str) -> str:
    """"4 cloves" scaled to 1.33 -> "about 1 clove"; 2.5 -> "3"."""
    n = int(qty + 0.5)
    if n < 1:
        s = fmt_qty(qty)
        return f"{s} {_plural(unit, s)}"
    word = _plural(unit, str(n))
    if abs(n - qty) / max(qty, 1e-9) >= 0.24:
        return f"about {n} {word}"
    return f"{n} {word}"


def scale_measure(qty: float, unit: str) -> str:
    """A scaled <amount> <unit> phrase, as it is written inside a step:
    1 cup x2 -> "2 cups"; under 1/8 teaspoon -> "a pinch"."""
    if unit == "teaspoon" and qty < 0.12:
        return "a pinch"
    q, u = _tidy_unit(qty, unit)
    s = fmt_qty(q)
    return f"{s} {_plural(u, s)}"


_STEP_AMOUNT_RE = re.compile(
    r"(?P<q>\d+(?:\s+\d+/\d+)?|\d+/\d+)\s+"
    r"(?P<u>cups?|tablespoons?|teaspoons?|ounces?|pounds?|grams?|kilograms?|milliliters?|"
    r"liters?|quarts?|pints?)\b", re.IGNORECASE)


def scale_step_text(text: str, amounts: list, factor: float) -> str:
    """Amounts written inside a step ("pour in 2 cups of the broth") are
    scaled when they match an ingredient's amount exactly; any other
    measure is flagged once as the original recipe's. Times, temperatures
    and pan sizes never match the pattern and are left alone."""
    if factor == 1:
        return text
    want = {(a["qty"], a["unit"]) for a in amounts
            if a.get("qty") is not None and a.get("unit") in MEASURE_UNITS}
    out, pos, flagged = [], 0, False
    for m in _STEP_AMOUNT_RE.finditer(text):
        qty, unit = _to_f(m.group("q")), _canon_unit(m.group("u"))
        if (qty, unit) in want:
            out.append(text[pos:m.start()])
            out.append(scale_measure(qty * factor, unit))
            pos = m.end()
        elif not flagged:
            out.append(text[pos:m.end()] + " (that's for the original recipe)")
            pos, flagged = m.end(), True
    out.append(text[pos:])
    return "".join(out)


def scale_line(line: str, amount: dict, factor: float) -> tuple:
    """One ingredient line scaled by `factor`: (new line, one-time note or
    None). The note says what rounding did to an uneven count (eggs)."""
    if not amount or amount.get("qty") is None or factor == 1:
        return line, None
    a = amount
    qty, hi = a["qty"] * factor, a["qty_hi"] * factor if a["qty_hi"] else None
    unit, rest, note = a["unit"], a["rest"], None
    if unit in COUNT_UNITS:
        qty, unit = _tidy_unit(qty, unit)
        lead = _round_count(qty, unit)
        if hi:
            h = _round_count(hi, unit)
            lead = lead if h == lead else f"{lead} to {h}"
    elif unit:
        if unit == "teaspoon" and qty < 0.12:
            return ("a pinch of " + rest).strip(), note
        qty, unit = _tidy_unit(qty, unit)
        s = fmt_qty(qty)
        if hi:
            hq, hu = _tidy_unit(hi, unit)
            if hu == unit:
                lead = f"{fmt_qty(qty)} to {fmt_qty(hq)} {_plural(unit, fmt_qty(hq))}"
            else:
                lead = f"{fmt_qty(qty)} {_plural(unit, s)} to {fmt_qty(hq)} {_plural(hu, fmt_qty(hq))}"
        else:
            lead = f"{s} {_plural(unit, s)}"
    else:
        # A counted thing ("4 large eggs", "2 or 3 anchovies"): round to a
        # whole number, and say it once when that changes the amount.
        n = int(qty + 0.5)
        lead = str(n)
        if hi:
            n_hi = int(hi + 0.5)
            lead = lead if n_hi == n else f"{n} to {n_hi}"
        else:
            frac = qty - math.floor(qty)
            is_egg = bool(re.search(r"\beggs?\b", rest, re.I)
                          and not re.search(r"egg\s+whites?\b", rest, re.I))
            if abs(frac) > 0.01:
                if is_egg and abs(frac - 0.5) <= 0.09:
                    whole = math.floor(qty)
                    tip = (f"use {whole} eggs plus 1 yolk" if whole > 1 else
                           "use 1 egg plus 1 yolk" if whole else "use 1 yolk")
                    note = f"the exact amount is {fmt_qty(qty)}; {tip}"
                elif abs(n - qty) / max(qty, 1e-9) >= 0.24:
                    lead = f"about {n}"
                    if is_egg:
                        note = f"the exact amount is {fmt_qty(qty)}"
        if re.fullmatch(r"(?:about )?1", lead):
            rest = _singular(rest)
        elif a["qty"] == 1 and n >= 2:
            rest = _plural_rest(rest)
    # Original order: amount, "(8 ounce)" package size, unit, the rest.
    parts = [lead] + ([f"({a['paren']})"] if a["paren"] else [])
    return (" ".join(parts) + " " + rest).strip(), note


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])|(?<=[.!?][)])\s+(?=[A-Z])")


def step_parts(s: str) -> list:
    """A long step, split at sentence boundaries into parts short enough to
    hold in your head while your hands are busy. Every word stays the
    recipe's; a short step is one part."""
    sentences = [x for x in _SENTENCE_RE.split(s) if x.strip()]
    n = len(s.split())
    if n <= _PART_NEVER_WORDS or (len(sentences) <= _PART_IF_SENTENCES and n <= _PART_IF_WORDS):
        return [s]
    out, cur, n = [], "", 0
    for sent in sentences:
        cw, sw = len(cur.split()), len(sent.split())
        full = cw + sw > _PART_MAX_WORDS or n >= 2
        if cur and full and cw >= _PART_SHORT and sw >= _PART_SHORT:
            out.append(cur)
            cur, n = sent, 1
        else:
            cur, n = (cur + " " + sent).strip(), n + 1
    if cur:
        if out and len(cur.split()) < _PART_SHORT:
            out[-1] += " " + cur
        else:
            out.append(cur)
    return out


def minutes(iso) -> int:
    m = _ISO_DUR_RE.fullmatch(str(iso or "").strip())
    if not m or not any(m.groups()):
        return 0
    d, h, mi, _s = (int(x or 0) for x in m.groups())
    return d * 1440 + h * 60 + mi


def say_minutes(m: int) -> str:
    if m < 60:
        return f"{m} minutes"
    h, rest = divmod(m, 60)
    rest = int(round(rest / 5.0) * 5)
    if rest == 60:
        h, rest = h + 1, 0
    hours = "an hour" if h == 1 else f"{h} hours"
    if rest == 0:
        return hours
    if rest == 30:
        return "an hour and a half" if h == 1 else f"{h} and a half hours"
    return f"{hours} and {rest} minutes"


# ── Parsing a page ───────────────────────────────────────────────────────────


def _find_recipe(node):
    if isinstance(node, list):
        for item in node:
            r = _find_recipe(item)
            if r:
                return r
    elif isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Recipe" in types:
            return node
        for key in ("@graph", "mainEntity", "mainEntityOfPage"):
            if key in node:
                r = _find_recipe(node[key])
                if r:
                    return r
    return None


def _flatten_steps(ins) -> list:
    """[(section_name or None, text)] from any recipeInstructions shape."""
    out = []
    if isinstance(ins, str):
        raw = re.sub(r"</p>|</li>|<br\s*/?>", "\n", _html.unescape(ins), flags=re.I)
        parts = [_text(p) for p in re.split(r"\n+|\s(?=\d+\.\s+[A-Z])", raw)]
        return [(None, re.sub(r"^\d+[.)]\s*", "", p)) for p in parts if p]
    if isinstance(ins, dict):
        ins = [ins]
    for item in ins or []:
        if isinstance(item, str):
            out.append((None, _text(item)))
        elif isinstance(item, dict):
            types = item.get("@type")
            types = types if isinstance(types, list) else [types]
            if "HowToSection" in types:
                name = _text(item.get("name")).rstrip(":") or None
                for _, text in _flatten_steps(item.get("itemListElement")):
                    out.append((name, text))
            else:
                text = item.get("text") or item.get("name") or ""
                if isinstance(text, list):
                    text = " ".join(map(str, text))
                out.append((None, _text(text)))
        elif isinstance(item, list):
            out.extend(_flatten_steps(item))
    return [(n, t) for n, t in out if t]


def _section_phrase(name: str) -> str:
    name = re.sub(r"^(?:for\s+(?:the\s+)?|make\s+the\s+|to\s+make\s+the\s+)", "", name.strip(" :-–—"), flags=re.I)
    return f"For the {name.lower()}:" if name else ""


def _servings(y) -> str:
    vals = [re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", "", _text(v))).strip()
            for v in (y if isinstance(y, list) else [y]) if v]
    words = [v for v in vals if re.search(r"[A-Za-z]", re.sub(r"\bto\b", "", v))]
    for v in words:
        m = re.match(r"^(\d+(?:\s*(?:-|to)\s*\d+)?)\s*(?:servings?|people|portions?|persons?)\b", v, re.I)
        if m:
            return "serves " + m.group(1).replace("-", " to ")
    if words:
        return "makes " + words[0]
    for v in sorted(vals, key=len, reverse=True):      # "8 to 10" over "8"
        if re.fullmatch(r"\d+(?:\s*(?:-|to)\s*\d+)?", v):
            return "serves " + v.replace("-", " to ")
    return ""


def _source(page: str, data: dict, url: str) -> str:
    m = _SITE_NAME_RE.search(page)
    pub = data.get("publisher")
    if m:
        name = _text(m.group(1))
    elif isinstance(pub, dict) and pub.get("name"):
        name = _text(pub["name"])
    else:
        name = re.sub(r"^www\.", "", urlparse(url).hostname or "")
    # "Inspired Taste - Easy Recipes for Home Cooks" -> "Inspired Taste"
    return re.split(r"\s+[-–—|:]\s+", name, maxsplit=1)[0].strip() or name


def _ingredient_groups(page: str, count: int) -> list:
    """WP Recipe Maker pages group ingredients ("Crust", "Filling") in their
    HTML only; the JSON-LD list is flat but in the same order."""
    chunks = re.split(r'class="wprm-recipe-ingredient-group"', page)[1:]
    if len(chunks) < 2:
        return []
    groups, start = [], 0
    for chunk in chunks:
        name_m = re.search(r'wprm-recipe-group-name[^>]*>(.*?)</', chunk, re.S)
        n = len(re.findall(r'<li[^>]+class="wprm-recipe-ingredient"', chunk))
        if not n:
            continue
        name = _text(name_m.group(1)).strip(" :-–—") if name_m else ""
        groups.append([name, start, start + n - 1])
        start += n
    return groups if start == count and any(g[0] for g in groups) else []


def parse_recipe_html(page: str, url: str) -> Recipe | None:
    data = None
    for block in _LDJSON_RE.findall(page):
        if "Recipe" not in block:
            continue
        try:
            data = _find_recipe(json.loads(block.strip(), strict=False))
        except ValueError:
            continue
        if data:
            break
    if not data:
        return None
    raw_ing = [i for i in (data.get("recipeIngredient") or data.get("ingredients") or []) if _text(i)]
    keep = [(n, speakable_ingredient(i)) for n, i in enumerate(raw_ing)
            if not _NOT_INGREDIENT_RE.match(_text(i))]
    keep = [(n, i) for n, i in keep if i]
    ingredients = [i for _, i in keep]
    new_index = {n: k for k, (n, _) in enumerate(keep)}
    groups = []
    for name, a, b in _ingredient_groups(page, len(raw_ing)):
        kept = [new_index[n] for n in range(a, b + 1) if n in new_index]
        if kept:
            groups.append([name, kept[0], kept[-1]])
    steps_raw = _flatten_steps(data.get("recipeInstructions"))
    if len(ingredients) < 2 or not steps_raw:
        return None
    sections = {n for n, _ in steps_raw if n}
    steps, last_section = [], None
    for name, text in steps_raw:
        step = speakable_step(text)
        if len(sections) > 1 and name and name != last_section:
            step = f"{_section_phrase(name)} {step}"
            last_section = name
        steps.append(step)
    rating = data.get("aggregateRating") or {}
    try:
        stars = float(rating.get("ratingValue") or 0)
        count = int(float(rating.get("ratingCount") or rating.get("reviewCount") or 0))
    except (TypeError, ValueError, AttributeError):
        stars, count = 0.0, 0
    total = minutes(data.get("totalTime")) or minutes(data.get("prepTime")) + minutes(data.get("cookTime"))
    all_steps = " ".join(steps)
    equipment = []
    for m in _EQUIPMENT_RE.finditer(all_steps):
        tool = m.group("tool").lower()
        if any(tool in e for e in equipment):
            continue
        size = (m.group("size") or "").strip()
        size = re.sub(r'(?:-|\s)?(?:inch|in\.?|")$', "-inch", size) if size else ""
        equipment.append(f"{size} {tool}".strip())
    wait_m = _WAIT_RE.search(all_steps)
    wait_phrase = ""
    if wait_m:
        verb = wait_m.group("verb").lower()
        verb = {"refrigerate": "chill", "set up": "set"}.get(verb, verb)
        wait_phrase = f"{verb} {wait_m.group('span').lower()}"
    servings = _servings(data.get("recipeYield"))
    yield_n, yield_hi, yield_unit = parse_yield(servings)
    return Recipe(
        title=_text(data.get("name")) or "that recipe",
        source=_source(page, data, url), url=url,
        ingredients=ingredients, groups=groups,
        steps=steps, servings=servings, total_min=total,
        rating=stars, rating_count=count, equipment=equipment[:3], wait=wait_phrase,
        yield_n=yield_n, yield_hi=yield_hi, yield_unit=yield_unit,
        amounts=[parse_amount(i) for i in ingredients],
    )


def spoken_title(title: str) -> str:
    """'The Best Classic Cheesecake Recipe' -> 'classic cheesecake'."""
    t = re.sub(r"\s*[|:–—-]\s.*$", "", title)
    t = re.sub(r"\s*\([^)]*\)|[®™©]", "", t)
    t = re.sub(r"\b(?:recipe|recipes)\b", "", t, flags=re.I)
    t = re.sub(r"^(?:(?:the|my|our|best|the\s+best|easy|easiest|perfect|ultimate|amazing|favorite|"
               r"favourite|world'?s|famous|homemade|simple|quick)\s+)+", "", t.strip(), flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" ,")
    return t.lower() if t else title


# ── Finding and ranking ──────────────────────────────────────────────────────


def _plain(text: str) -> str:
    """'Bánh Mì' -> 'Banh Mi': accents off before matching."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _words(text: str) -> set:
    text = _plain(text)
    return {w.rstrip("s") for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2}


def rank(recipes: list, dish: str) -> list:
    want = _words(dish) - {"recipe", "homemade", "easy", "best", "the", "and", "with"}
    scored = []
    for order, r in enumerate(recipes):
        title = _words(r.title)
        fit = len(want & title) / len(want) if want else 1.0
        if want and fit == 0:
            continue
        # "white chicken enchiladas" is a different dish from the one asked for.
        extra = _words(spoken_title(r.title)) - want
        score = fit * 3 - min(len(extra), 3) * 0.6
        if r.rating and r.rating_count:
            score += r.rating * math.log10(1 + r.rating_count) / 3
        score += max(0, 8 - order) * 0.15
        score -= max(0, len(r.ingredients) - 15) * 0.35 + max(0, len(r.steps) - 14) * 0.25
        scored.append((score, order, r))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [r for _, _, r in scored]


# ── The cookbook ─────────────────────────────────────────────────────────────
# Popular recipes kept for good (2026-10-06): instant, and still there when
# the internet isn't. Filled by scripts/build_cookbook.py from a dish list and
# by every search whose results clear the bar. Still real recipes from real
# pages — only stored, never written.

COOKBOOK_MIN_RATING = 4.5
COOKBOOK_MIN_RATINGS = 50     # a 5.0 from 3 people isn't "popular"
COOKBOOK_KEEP = 3             # per dish, so "another recipe" has somewhere to go
_KEY_FILLER = {"recipe", "recipes", "homemade", "easy", "best", "the", "a", "an", "classic", "simple",
               "quick", "authentic", "traditional", "good", "some", "my", "perfect", "ultimate", "make"}


def popular(r: Recipe) -> bool:
    return r.rating >= COOKBOOK_MIN_RATING and r.rating_count >= COOKBOOK_MIN_RATINGS


def dish_key(dish: str) -> str:
    """'Homemade Pancakes' and 'pancake' -> 'pancake'."""
    ws = [w.rstrip("s") if len(w) > 3 and not w.endswith("ss") else w
          for w in re.findall(r"[a-z0-9]+", _plain(dish).lower()) if w not in _KEY_FILLER]
    return "-".join(ws)[:80]


class Cookbook:
    """data/cookbook/<key>.json: {"dish", "aliases", "added", "recipes"}."""

    def __init__(self, path=None):
        self._dir = path or settings.DATA_DIR / "cookbook"
        self._index = None        # key or alias key -> file key
        self._mtime = None        # the folder's, so the builder's additions show up

    def _load_index(self) -> dict:
        try:
            mtime = self._dir.stat().st_mtime
        except OSError:
            mtime = None
        if self._index is None or mtime != self._mtime:
            self._mtime = mtime
            idx = {}
            for f in sorted(self._dir.glob("*.json")) if self._dir.exists() else []:
                try:
                    d = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                idx[f.stem] = f.stem
                for a in d.get("aliases", []):
                    idx.setdefault(dish_key(a), f.stem)
            self._index = idx
        return self._index

    def get(self, dish: str) -> list:
        key = self._load_index().get(dish_key(dish))
        if not key:
            return []
        try:
            d = json.loads((self._dir / f"{key}.json").read_text())
            return [Recipe.from_dict(r) for r in d["recipes"]]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def has(self, dish: str) -> bool:
        return dish_key(dish) in self._load_index()

    def put(self, dish: str, recipes: list, aliases=()) -> int:
        """Keep the popular ones (merged with what's there, best first);
        returns how many the entry now holds."""
        key = dish_key(dish)
        if not key:
            return 0
        path = self._dir / f"{key}.json"
        try:
            old = json.loads(path.read_text())
        except (OSError, ValueError):
            old = {"dish": dish, "aliases": [], "added": time.time(), "recipes": []}
        have = {r["url"]: r for r in old["recipes"]}
        for r in recipes:
            if popular(r):
                have[r.url] = r.to_dict()
        if not have:
            return 0
        best = rank([Recipe.from_dict(r) for r in have.values()], old["dish"])[:COOKBOOK_KEEP]
        if not best:
            return 0
        old["recipes"] = [r.to_dict() for r in best]
        old["aliases"] = sorted(set(old.get("aliases", [])) | {a for a in aliases if dish_key(a) != key})
        old["updated"] = time.time()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(old))
        except OSError as e:
            log.warning("Cookbook write failed: %s", e)
            return 0
        self._index = None
        return len(best)


class RecipeFinder:
    """find(dish) -> ranked [Recipe]: the cookbook first, then a search,
    cached on disk per dish."""

    def __init__(self, cache_days: int = 14, cookbook: Cookbook | None = None):
        self._cache_s = cache_days * 86400
        self._dir = settings.DATA_DIR / "recipes"
        self.cookbook = cookbook or Cookbook()

    def _cache_path(self, dish: str):
        slug = re.sub(r"[^a-z0-9]+", "-", dish.lower()).strip("-")[:80] or "recipe"
        return self._dir / f"{slug}.json"

    def find(self, dish: str) -> list:
        kept = self.cookbook.get(dish)
        if kept:
            log.info("Recipe %r from the cookbook (%d)", dish, len(kept))
            return kept          # stored best first, ranked by the entry's own dish name
        return self.search(dish)

    def search(self, dish: str, enough: int = _ENOUGH, fresh: bool = False) -> list:
        """The web (or the 14-day cache), skipping the cookbook; what clears
        the popularity bar is added to it. The cookbook builder waits for
        every page (`enough`) so it has the most to choose from."""
        path = self._cache_path(dish)
        try:
            cached = json.loads(path.read_text())
            if not fresh and time.time() - cached["fetched"] < self._cache_s and cached["recipes"]:
                return rank([Recipe.from_dict(d) for d in cached["recipes"]], dish)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        t0 = time.time()
        urls = self._search(dish)
        recipes = self._fetch_all(urls, enough)
        ranked = rank(recipes, dish)
        log.info("Recipe search %r: %d urls, %d parsed, %d kept in %.1fs",
                 dish, len(urls), len(recipes), len(ranked), time.time() - t0)
        if ranked:
            added = self.cookbook.put(dish, ranked)
            if added:
                log.info("Cookbook: %r keeps %d popular recipe(s)", dish, added)
            try:
                # Search order, not ranked: rank() runs again on every load.
                self._dir.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"fetched": time.time(), "dish": dish,
                                            "recipes": [r.to_dict() for r in recipes]}))
            except OSError as e:
                log.warning("Recipe cache write failed: %s", e)
        return ranked

    def _search(self, dish: str) -> list:
        r = requests.get(f"{settings.SEARCH_URL}/search", timeout=settings.SEARCH_TIMEOUT, params={
            "q": f"{dish} recipe", "format": "json", "engines": settings.SEARCH_ENGINES,
            "categories": "general", "language": "en-US"})
        r.raise_for_status()
        urls, seen = [], set()
        for res in r.json().get("results", []):
            url = res.get("url") or ""
            host = urlparse(url).hostname or ""
            if not url.startswith("http") or _SKIP_HOSTS.search(host) or url in seen:
                continue
            seen.add(url)
            urls.append(url)
        return urls[:settings.RECIPE_CANDIDATES]

    def _fetch_one(self, url: str):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=settings.RECIPE_FETCH_TIMEOUT)
            if r.status_code != 200:
                log.info("Recipe fetch %s: HTTP %s", url, r.status_code)
                return None
            page = r.content.decode("utf-8", errors="replace")
            recipe = parse_recipe_html(page, url)
            if recipe is None:
                log.info("Recipe fetch %s: no recipe data", url)
            return recipe
        except Exception as e:
            log.info("Recipe fetch %s failed: %s", url, e)
            return None

    def _fetch_all(self, urls: list, enough: int = _ENOUGH) -> list:
        """Fetch in parallel; stop at the deadline, or early once enough good
        recipes are in (slow sites shouldn't hold up the answer)."""
        if not urls:
            return []
        pool = ThreadPoolExecutor(max_workers=len(urls))
        futures = [pool.submit(self._fetch_one, u) for u in urls]
        deadline = time.time() + settings.RECIPE_FETCH_TIMEOUT
        pending = set(futures)
        while pending and time.time() < deadline:
            _done, pending = wait(pending, timeout=deadline - time.time(), return_when=FIRST_COMPLETED)
            got = sum(1 for f in futures if f.done() and f.result() is not None)
            if got >= enough:
                break
        pool.shutdown(wait=False, cancel_futures=True)
        # Search order, so rank() can still favor what the engines put first.
        return [f.result() for f in futures if f.done() and f.result() is not None]

    def cached(self, dish: str) -> bool:
        if self.cookbook.has(dish):
            return True
        try:
            d = json.loads(self._cache_path(dish).read_text())
            return time.time() - d["fetched"] < self._cache_s and bool(d["recipes"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
