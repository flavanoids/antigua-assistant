"""Deterministic calculator & unit-conversion skill.

LLM bypass, same class as time_date / weather: pure Python, network-free.
Handles the "instant answer" arithmetic that otherwise falls to the 4b model:

  * arithmetic         — "what's 12 times 13", "100 divided by 4", "5 plus 8"
  * percentages        — "what's 15% of 80", "20% off 50", "add 20% to 45"
  * tips               — "20% tip on a 47 dollar check"
  * unit conversion    — "how many ml in 2 cups", "5 miles in km", "3 feet in cm"
  * temperature        — "350 fahrenheit in celsius", "20 c to f"

Currency conversion is deliberately out of scope here — it needs a rates feed
and belongs in a follow-up (calc_currency.py) wired in through answer().

Phrasing discipline (see the module docstring notes in tts_text.py):
every reply is built so clean_for_tts() speaks it cleanly — no thousands
separators, results rounded, money spelled with num2words and the right
currency word (tts_text hardcodes "dollars"). tests/test_calc.py runs every
canned reply through clean_for_tts() and fails on a surviving digit.
"""

import ast
import re

from num2words import num2words

from . import calc_currency

# ── spoken-number helpers ───────────────────────────────────────────────────


def _trim(x: float) -> str:
    """Round for speech and return a separator-free numeric string.

    clean_for_tts turns the digits into words; our job is never to hand it a
    thousands separator or a long float tail.
    """
    x = float(x)
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    ax = abs(x)
    if ax >= 100:
        return str(int(round(x)))
    s = f"{x:.1f}" if ax >= 10 else f"{x:.2f}"
    return s.rstrip("0").rstrip(".")


def _signed(x: float) -> str:
    s = _trim(abs(x))
    return f"negative {s}" if x < 0 else s


def _approx(x: float) -> str:
    """'about 8.05' when rounding lost something, plain '8' when it didn't."""
    shown = _trim(x)
    return shown if abs(abs(x) - float(shown)) < 1e-6 else f"about {shown}"


_CCY = {
    "USD": ("dollar", "dollars"), "EUR": ("euro", "euros"),
    "GBP": ("pound", "pounds"), "CAD": ("Canadian dollar", "Canadian dollars"),
    "AUD": ("Australian dollar", "Australian dollars"),
    "MXN": ("peso", "pesos"), "JPY": ("yen", "yen"),
}


def _money(amount: float, code: str = "USD") -> str:
    """Spell a currency amount in words, correct currency noun, cents split off.

    tts_text._cents_sub / currency_sub hardcode 'dollars', so a bare '€46.30'
    would be spoken as dollars — money is always phrased here instead.
    """
    sing, plur = _CCY.get(code, ("dollar", "dollars"))
    if code == "JPY":
        return f"{num2words(int(round(amount)))} yen"
    whole, cents = divmod(int(round(amount * 100)), 100)
    unit = sing if whole == 1 else plur
    if cents == 0:
        return f"{num2words(whole)} {unit}"
    return f"{num2words(whole)} {unit} and {num2words(cents)} cents"


# ── arithmetic ──────────────────────────────────────────────────────────────

_WORD_OPS = [
    (re.compile(r"\bmultiplied by\b", re.I), "*"),
    (re.compile(r"\bdivided by\b", re.I), "/"),
    (re.compile(r"\btimes\b", re.I), "*"),
    (re.compile(r"\bplus\b", re.I), "+"),
    (re.compile(r"\b(?:minus|less)\b", re.I), "-"),
    (re.compile(r"\bover\b", re.I), "/"),
    (re.compile(r"\bx\b", re.I), "*"),
]

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod,
    ast.USub, ast.UAdd,
)


def _safe_eval(expr: str):
    """Evaluate a bare +-*/ expression. Raises on anything else."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("non-numeric constant")
        elif not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"disallowed node: {type(node).__name__}")
    val = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})  # noqa: S307
    if isinstance(val, complex) or abs(val) > 1e12:
        raise ValueError("out of range")
    return float(val)


_PCT_OF_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+\$?(\d[\d,]*(?:\.\d+)?)", re.I)
_PCT_OFF_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+off\s+(?:of\s+)?\$?(\d[\d,]*(?:\.\d+)?)", re.I)
_PCT_ADD_RE = re.compile(
    r"(?:add\s+)?(\d+(?:\.\d+)?)\s*(?:%|percent)\s+(?:to|onto)\s+\$?(\d[\d,]*(?:\.\d+)?)"
    r"|\$?(\d[\d,]*(?:\.\d+)?)\s*(?:plus|\+)\s*(\d+(?:\.\d+)?)\s*(?:%|percent)", re.I)
_TIP_RE = re.compile(
    r"(?:(\d+(?:\.\d+)?)\s*(?:%|percent)\s+)?(?:tip|gratuity)"
    r"(?:\s+of\s+(\d+(?:\.\d+)?)\s*(?:%|percent))?\s+on\s+(?:a\s+)?\$?(\d[\d,]*(?:\.\d+)?)", re.I)
_FRAC_RE = re.compile(
    r"\b(half|a third|a quarter|two thirds|three quarters|double|triple|twice)\s+"
    r"(?:of\s+)?\$?(\d[\d,]*(?:\.\d+)?)", re.I)
_FRAC_FACTORS = {
    "half": 0.5, "a third": 1 / 3, "a quarter": 0.25,
    "two thirds": 2 / 3, "three quarters": 0.75,
    "double": 2.0, "triple": 3.0, "twice": 2.0,
}


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def _arith(text: str):
    s = text.lower().strip()
    s = re.sub(r"[?.!]+$", "", s).strip()

    m = _TIP_RE.search(s)
    if m:
        pct = float(m.group(1) or m.group(2) or 20)
        base = _f(m.group(3))
        tip = base * pct / 100
        art = "An" if num2words(int(round(pct)))[0] in "aeiou" else "A"
        return (f"{art} {_trim(pct)} percent tip on {_money(base)} is {_money(tip)}, "
                f"for a total of {_money(base + tip)}.")

    m = _PCT_OF_RE.search(s)
    if m:
        pct, base = float(m.group(1)), _f(m.group(2))
        money = "$" in s or "dollar" in s
        val = base * pct / 100
        got = _money(val) if money else _trim(val)
        base_s = _money(base) if money else _trim(base)
        return f"{_trim(pct)} percent of {base_s} is {got}."

    m = _PCT_OFF_RE.search(s)
    if m:
        pct, base = float(m.group(1)), _f(m.group(2))
        val = base * (1 - pct / 100)
        money = "$" in s or "dollar" in s
        return (f"{_trim(pct)} percent off {_money(base) if money else _trim(base)} "
                f"is {_money(val) if money else _trim(val)}.")

    m = _PCT_ADD_RE.search(s)
    if m:
        if m.group(1):
            pct, base = float(m.group(1)), _f(m.group(2))
        else:
            base, pct = _f(m.group(3)), float(m.group(4))
        val = base * (1 + pct / 100)
        money = "$" in s or "dollar" in s
        return (f"{_trim(base) if not money else _money(base)} plus {_trim(pct)} percent "
                f"is {_money(val) if money else _trim(val)}.")

    m = _FRAC_RE.search(s)
    if m:
        word, base = m.group(1).lower(), _f(m.group(2))
        val = base * _FRAC_FACTORS[word]
        lead = {"double": "Double", "triple": "Triple", "twice": "Twice"}.get(word)
        if lead:
            return f"{lead} {_trim(base)} is {_trim(val)}."
        return f"{word.capitalize()} of {_trim(base)} is {_trim(val)}."

    # General bare expression: words -> symbols, strip everything else. Bail if
    # a percent slipped past the branches above — stripping "percent" to a space
    # would turn "45 plus 8 percent tax" into "45 + 8" and answer confidently
    # wrong. Better to hand those to the LLM.
    if re.search(r"%|\bpercent\b", s):
        return None
    expr = s
    for pat, op in _WORD_OPS:
        expr = pat.sub(op, expr)
    expr = re.sub(r"(\d),(\d{3})\b", r"\1\2", expr)
    expr = re.sub(r"(?:%|percent)", " ", expr)
    expr = re.sub(r"[^0-9+\-*/().\s]", " ", expr).strip()
    if not re.search(r"\d\s*[-+*/]\s*[-(]*\s*\d", expr):
        return None
    try:
        val = _safe_eval(expr)
    except ZeroDivisionError:
        return "You can't divide by zero."
    except Exception:
        return None
    return f"That's {_approx(val) if val >= 0 else _signed(val)}."


# ── unit conversion ─────────────────────────────────────────────────────────

_LENGTH = {"meter": 1.0, "kilometer": 1000.0, "centimeter": 0.01,
           "millimeter": 0.001, "mile": 1609.344, "yard": 0.9144,
           "foot": 0.3048, "inch": 0.0254}
_MASS = {"gram": 1.0, "kilogram": 1000.0, "milligram": 0.001,
         "pound": 453.59237, "ounce": 28.349523125, "stone": 6350.29318,
         "ton": 1_000_000.0, "tonne": 1_000_000.0}
_VOLUME = {"milliliter": 1.0, "liter": 1000.0, "teaspoon": 4.928921594,
           "tablespoon": 14.78676478, "fluid ounce": 29.57352956,
           "cup": 236.5882365, "pint": 473.176473, "quart": 946.352946,
           "gallon": 3785.411784}
_TIME = {"second": 1.0, "minute": 60.0, "hour": 3600.0, "day": 86400.0,
         "week": 604800.0}
_SPEED = {"mile per hour": 0.44704, "kilometer per hour": 0.277777778,
          "meter per second": 1.0, "knot": 0.514444444}
_TABLES = (_LENGTH, _MASS, _VOLUME, _TIME, _SPEED)

_ALIAS = {
    "m": "meter", "metre": "meter", "meter": "meter",
    "km": "kilometer", "kilometre": "kilometer", "kilometer": "kilometer",
    "cm": "centimeter", "centimetre": "centimeter", "centimeter": "centimeter",
    "mm": "millimeter", "millimetre": "millimeter", "millimeter": "millimeter",
    "mi": "mile", "mile": "mile", "yd": "yard", "yard": "yard",
    "ft": "foot", "foot": "foot", "feet": "foot",
    "in": "inch", "inch": "inch", "inches": "inch",
    "g": "gram", "gram": "gram", "gramme": "gram",
    "kg": "kilogram", "kilo": "kilogram", "kilogram": "kilogram",
    "mg": "milligram", "milligram": "milligram",
    "lb": "pound", "lbs": "pound", "pound": "pound",
    "oz": "ounce", "ounce": "ounce",
    "fl oz": "fluid ounce", "fluid ounce": "fluid ounce",
    "stone": "stone", "ton": "ton", "tonne": "tonne",
    "ml": "milliliter", "millilitre": "milliliter", "milliliter": "milliliter",
    "l": "liter", "litre": "liter", "liter": "liter",
    "tsp": "teaspoon", "teaspoon": "teaspoon",
    "tbsp": "tablespoon", "tablespoon": "tablespoon",
    "cup": "cup", "pint": "pint", "quart": "quart", "gallon": "gallon",
    "sec": "second", "second": "second",
    "min": "minute", "minute": "minute",
    "hr": "hour", "hour": "hour",
    "day": "day", "week": "week",
    "mph": "mile per hour", "kph": "kilometer per hour",
    "km/h": "kilometer per hour", "kmh": "kilometer per hour",
    "knot": "knot",
}

# Spoken forms where canonical + "s" is wrong.
_SPOKEN = {
    "foot": ("foot", "feet"), "inch": ("inch", "inches"),
    "fluid ounce": ("fluid ounce", "fluid ounces"),
    "mile per hour": ("mile per hour", "miles per hour"),
    "kilometer per hour": ("kilometer per hour", "kilometers per hour"),
    "meter per second": ("meter per second", "meters per second"),
}


def _canon(u: str):
    u = re.sub(r"\s+", " ", u.strip().rstrip(".")).lower()
    if u in _ALIAS:
        return _ALIAS[u]
    if u.endswith("s") and u[:-1] in _ALIAS:
        return _ALIAS[u[:-1]]
    return None


def _unit_word(canonical: str, n: float) -> str:
    sing, plur = _SPOKEN.get(canonical, (canonical, canonical + "s"))
    return sing if abs(n - 1) < 1e-9 else plur


def _do_convert(qty: float, src: str, dst: str):
    cs, cd = _canon(src), _canon(dst)
    if not cs or not cd or cs == cd:
        return None
    for table in _TABLES:
        if cs in table and cd in table:
            val = qty * table[cs] / table[cd]
            return (f"{_trim(qty)} {_unit_word(cs, qty)} is "
                    f"{_approx(val)} {_unit_word(cd, val)}.")
    return None  # cross-category ("cups in a mile") — let the LLM decline it


_TEMP_UNITS = r"(fahrenheit|celsius|centigrade|kelvin|deg(?:ree)?s?\s*[fck]|[fck])"
_TEMP_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(?:degrees?\s*)?" + _TEMP_UNITS +
    r"\b.*?\b(?:in|to|into|as)\s+(?:degrees?\s*)?" + _TEMP_UNITS + r"\b", re.I)


def _temp_key(word: str) -> str:
    w = word.lower()
    if "fahren" in w:
        return "f"
    if "celsi" in w or "centigrade" in w:
        return "c"
    if "kelvin" in w:
        return "k"
    return w.strip()[-1]  # "deg f" / "f"


def _fmt_temp(x: float) -> str:
    """One decimal, kept even above 100 ('176.7'); trailing '.0' dropped."""
    x = round(x, 1)
    return str(int(x)) if abs(x - int(x)) < 1e-9 else f"{x:.1f}"


def _temp(v: float, frm: str, to: str):
    f, t = _temp_key(frm), _temp_key(to)
    if f == t:
        return None
    c = {"f": (v - 32) * 5 / 9, "c": v, "k": v - 273.15}[f]
    out = {"f": c * 9 / 5 + 32, "c": c, "k": c + 273.15}[t]
    names = {"f": "Fahrenheit", "c": "Celsius", "k": "Kelvin"}

    def lbl(x, k):
        return f"{_fmt_temp(x)} Kelvin" if k == "k" else f"{_fmt_temp(x)} degrees {names[k]}"

    return f"{lbl(v, f)} is {lbl(out, t)}."


_HOWMANY_RE = re.compile(
    r"how many ([a-z ]+?)\s+(?:are\s+)?(?:in|per)\s+"
    r"(?:(\d[\d,]*(?:\.\d+)?)\s+|an?\s+|one\s+)?([a-z ]+?)$", re.I)
_NX_RE = re.compile(
    r"(?:convert\s+)?(\d[\d,]*(?:\.\d+)?)\s+([a-z /]+?)\s+"
    r"(?:in|to|into|as)\s+([a-z /]+?)$", re.I)


def _convert(text: str):
    s = re.sub(r"[?.!]+$", "", text.strip()).strip()

    m = _TEMP_RE.search(s)
    if m:
        return _temp(float(m.group(1)), m.group(2), m.group(3))

    m = _HOWMANY_RE.search(s)
    if m:
        qty = _f(m.group(2)) if m.group(2) else 1.0
        return _do_convert(qty, m.group(3), m.group(1))

    m = _NX_RE.search(s)
    if m:
        return _do_convert(_f(m.group(1)), m.group(2), m.group(3))

    return None


# ── currency ────────────────────────────────────────────────────────────────

_CCY_ALT = "|".join(
    re.escape(k) for k in sorted(calc_currency.CURRENCY_ALIASES, key=len, reverse=True)
)
_CCY_RE = re.compile(
    r"(?:convert\s+|how much (?:is|would)\s+)?"
    r"(?:([$£€¥])\s*)?(\d[\d,]*(?:\.\d+)?)\s*"
    r"(?:(" + _CCY_ALT + r")\b)?\s*"
    r"(?:in|to|into|as|worth of|equal to|is that in|would that be in)\s+"
    r"(?:the\s+)?(" + _CCY_ALT + r")\b",
    re.I,
)


def _spell_currency(amount: float, code: str) -> str:
    sing, plur = calc_currency.CURRENCY_WORDS.get(code, ("unit", "units"))
    if code in calc_currency.NO_SUBUNIT:
        n = int(round(amount))
        return f"{num2words(n)} {sing if n == 1 else plur}"
    whole, cents = divmod(int(round(amount * 100)), 100)
    unit = sing if whole == 1 else plur
    if cents == 0:
        return f"{num2words(whole)} {unit}"
    return f"{num2words(whole)} {unit} and {num2words(cents)} cents"


def _currency(text: str, rates):
    if rates is None:
        return None
    m = _CCY_RE.search(re.sub(r"[?.!]+$", "", text.strip()))
    if not m:
        return None
    sym, amt_s, src_word, dst_word = m.groups()
    _SYM = {"$": "dollars", "£": "pounds", "€": "euros", "¥": "yen"}
    src = calc_currency.canonical_currency(src_word or _SYM.get(sym or "", ""))
    dst = calc_currency.canonical_currency(dst_word)
    if not src or not dst or src == dst:
        return None
    got = rates.get_rate(src, dst)
    if got is None:
        return None  # pair not covered — let the LLM field it
    rate, stale = got
    amount = _f(amt_s)
    value = amount * rate
    tail = " — my rates are a day or two old" if stale else ""
    return f"{_spell_currency(amount, src)} is about {_spell_currency(value, dst)}{tail}."


# ── entry point ─────────────────────────────────────────────────────────────


def answer(text: str, *, rates=None) -> str | None:
    """Deterministic calc/convert answer, or None if this isn't a calc turn.

    `rates` is a calc_currency.RatesProvider (Backend.rates_provider), or None
    on backends without one — currency questions then fall through to the LLM.
    """
    if not text:
        return None
    try:
        reply = _currency(text, rates)
    except Exception:  # noqa: BLE001
        reply = None
    if reply:
        return reply
    for fn in (_convert, _arith):
        try:
            reply = fn(text)
        except Exception:  # noqa: BLE001 — never let a parse bug break the turn
            reply = None
        if reply:
            return reply
    return None
