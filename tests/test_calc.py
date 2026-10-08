#!/usr/bin/env python3
"""Snapshot tests for the deterministic calculator / unit-conversion skill.

No network. Every canned reply is also pushed through clean_for_tts() and
asserted to contain no surviving digit, stray decimal point, or literal
"comma" — the pipeline speaks skill replies verbatim, so a phrasing
regression that would read "one comma five" out loud fails here first.

Run: python3 tests/test_calc.py   (also works under pytest)
"""

import re
import sys
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

settings.configure({})

from antigua_core import calc  # noqa: E402
from antigua_core.classify import classify  # noqa: E402
from antigua_core.tts_text import clean_for_tts  # noqa: E402

# ── answer() snapshots ──────────────────────────────────────────────────────

CASES = [
    # arithmetic
    ("what's 12 times 13", "That's 156."),
    ("what is 100 divided by 4", "That's 25."),
    ("what's 5 plus 8", "That's 13."),
    ("what's 7 minus 12", "That's negative 5."),
    ("what is 10 divided by 3", "That's about 3.33."),
    ("what's 2000 times 3", "That's 6000."),
    ("what is 10 divided by 0", "You can't divide by zero."),
    # powers and roots
    ("what is 5 squared", "That's 25."),
    ("what's 3 to the power of 4", "That's 81."),
    ("what's 2 to the 10th", "That's 1024."),
    ("10 cubed", "That's 1000."),
    ("what's 2 times 3 squared", "That's 18."),
    ("3 squared plus 4 squared", "That's 25."),
    ("what is the square root of 144", "The square root of 144 is 12."),
    ("square root of 2", "The square root of 2 is about 1.41."),
    ("cube root of negative 8", "The cube root of negative 8 is negative 2."),
    ("square root of 16 plus 2", "That's 6."),
    # spoken numbers
    ("nine times six", "That's 54."),
    ("what is twelve times twelve", "That's 144."),
    ("two to the tenth power", "That's 1024."),
    ("what is one hundred and five times three", "That's 315."),
    ("two point five times four", "That's 10."),
    ("what's 5 thousand divided by 8", "That's 625."),
    ("negative 5 plus 3", "That's negative 2."),
    ("what's twenty-five percent of eighty", "25 percent of 80 is 20."),
    # percentages
    ("what's 15% of 80", "15 percent of 80 is 12."),
    ("what is 20 percent of 250", "20 percent of 250 is 50."),
    ("what's 25% off 40 dollars", "25 percent off forty dollars is thirty dollars."),
    ("add 20% to 45", "45 plus 20 percent is 54."),
    # tips
    ("what's a 20% tip on a 47 dollar check",
     "A 20 percent tip on forty-seven dollars is nine dollars and forty cents, "
     "for a total of fifty-six dollars and forty cents."),
    ("18 percent tip on 60",
     "An 18 percent tip on sixty dollars is ten dollars and eighty cents, "
     "for a total of seventy dollars and eighty cents."),
    # fractions / doubling
    ("what's half of 250", "Half of 250 is 125."),
    ("double 1500", "Double 1500 is 3000."),
    ("three quarters of 200", "Three quarters of 200 is 150."),
    # length
    ("how many feet in a mile", "1 mile is 5280 feet."),
    ("5 miles in km", "5 miles is about 8.05 kilometers."),
    ("convert 3 feet to centimeters", "3 feet is about 91.4 centimeters."),
    # volume
    ("how many ml in 2 cups", "2 cups is about 473 milliliters."),
    ("how many tablespoons in a cup", "1 cup is 16 tablespoons."),
    # mass
    ("how many grams in 2 pounds", "2 pounds is about 907 grams."),
    # time
    ("how many minutes in 2 hours", "2 hours is 120 minutes."),
    # temperature
    ("350 fahrenheit in celsius", "350 degrees Fahrenheit is 176.7 degrees Celsius."),
    ("what is 20 c to f", "20 degrees Celsius is 68 degrees Fahrenheit."),
    ("convert 300 kelvin to celsius", "300 Kelvin is 26.9 degrees Celsius."),
]

# calc.answer() must decline these (they belong to the LLM / search)
DECLINES = [
    "how many calories in a banana",
    "how many days until christmas",
    "how much do you love me",
    "what's the meaning of life",
    "how many cups in a mile",
    "square root of negative 4",
    "what's 9 to the power of 9 to the power of 9",
]

# classify() must route these to "calc"
ROUTES_CALC = [
    "what's 12 times 13",
    "what is 100 divided by 4",
    "whats 15% of 80",
    "what's a 20% tip on a 47 dollar check",
    "how many ml in 2 cups",
    "how many cups are in a gallon",
    "5 miles in km",
    "convert 350 fahrenheit to celsius",
    "half of 250",
    "what is 5 squared",
    "what's 3 to the power of 4",
    "what is the square root of 144",
    "nine times six",
    "two to the tenth power",
]

# classify() must NOT route these to "calc" (neighbour skills own them)
ROUTES_NOT_CALC = [
    ("set a timer for 5 minutes", "timer_set"),
    ("add 5 minutes to the timer", "timer_add"),
    ("wake me up at 7", "alarm_set"),
    ("is it warmer than yesterday", "weather"),
    ("how many calories in a banana", "llm"),
    ("set a timer for five minutes", "timer_set"),
    ("add two eggs to the list", "list_add"),
    ("which one is better", "llm"),
]

_DIGIT_RE = re.compile(r"\d")


class _FakeRates:
    """Offline stand-in for calc_currency.RatesProvider."""

    _TABLE = {
        ("USD", "EUR"): 0.92, ("USD", "MXN"): 17.0, ("USD", "JPY"): 150.0,
        ("EUR", "USD"): 1.087, ("GBP", "USD"): 1.27,
    }

    def get_rate(self, src, dst):
        if src == dst:
            return (1.0, False)
        if (src, dst) in self._TABLE:
            return (self._TABLE[(src, dst)], False)
        return None


CCY_CASES = [
    ("50 dollars in euros", "fifty dollars is about forty-six euros."),
    ("how much is 20 dollars in pesos",
     "twenty dollars is about three hundred and forty pesos."),
    ("100 dollars to yen", "one hundred dollars is about fifteen thousand yen."),
    ("convert 10 euros to dollars",
     "ten euros is about ten dollars and eighty-seven cents."),
]
CCY_DECLINES = ["50 dollars in rubles", "what's the weather in euros"]


def run():
    failed = 0
    rates = _FakeRates()

    for text, expected in CCY_CASES:
        got = calc.answer(text, rates=rates)
        if got != expected:
            failed += 1
            print(f"[FAIL] {text!r}\n   expected: {expected!r}\n   got:      {got!r}")
        else:
            spoken = clean_for_tts(got)
            bad = _DIGIT_RE.search(spoken) or "comma" in spoken.lower()
            if bad:
                failed += 1
                print(f"[FAIL] {text!r} -> tts {spoken!r}")
            else:
                print(f"[ok] {text!r} -> {got!r}\n     tts: {spoken!r}")

    for text in CCY_DECLINES:
        if calc.answer(text, rates=rates) is not None:
            failed += 1
            print(f"[FAIL] {text!r} should decline (currency)")
        else:
            print(f"[ok] declined (currency): {text!r}")

    # No provider -> currency questions fall through.
    if calc.answer("50 dollars in euros", rates=None) is not None:
        failed += 1
        print("[FAIL] currency answered with rates=None")
    else:
        print("[ok] rates=None -> None for '50 dollars in euros'")

    if classify("50 dollars in euros") != "calc":
        failed += 1
        print(f"[FAIL] '50 dollars in euros' routed {classify('50 dollars in euros')!r}")
    else:
        print("[ok] routes calc: '50 dollars in euros'")

    for text, expected in CASES:
        got = calc.answer(text)
        if got != expected:
            failed += 1
            print(f"[FAIL] {text!r}\n   expected: {expected!r}\n   got:      {got!r}")
            continue
        spoken = clean_for_tts(got)
        problems = []
        if _DIGIT_RE.search(spoken):
            problems.append("digit survived clean_for_tts")
        if "comma" in spoken.lower():
            problems.append("literal 'comma'")
        if re.search(r"\w\s\.\s|\s\.\s\w", spoken):
            problems.append("stray decimal point")
        if problems:
            failed += 1
            print(f"[FAIL] {text!r} -> tts {spoken!r}: {', '.join(problems)}")
        else:
            print(f"[ok] {text!r} -> {got!r}")
            print(f"     tts: {spoken!r}")

    for text in DECLINES:
        got = calc.answer(text)
        if got is not None:
            failed += 1
            print(f"[FAIL] {text!r} should decline, got {got!r}")
        else:
            print(f"[ok] declined: {text!r}")

    for text in ROUTES_CALC:
        r = classify(text)
        if r != "calc":
            failed += 1
            print(f"[FAIL] {text!r} routed {r!r}, expected 'calc'")
        else:
            print(f"[ok] routes calc: {text!r}")

    for text, expected in ROUTES_NOT_CALC:
        r = classify(text)
        if r == "calc":
            failed += 1
            print(f"[FAIL] {text!r} routed 'calc', expected {expected!r}")
        else:
            print(f"[ok] not calc ({r}): {text!r}")

    if failed:
        print(f"\n{failed} check(s) failed")
        sys.exit(1)
    print(f"\nAll {len(CASES) + len(CCY_CASES)} snapshots + "
          f"{len(DECLINES) + len(CCY_DECLINES)} declines + routing checks passed")


if __name__ == "__main__":
    run()
