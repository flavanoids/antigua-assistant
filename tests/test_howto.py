"""Household how-tos (howto.py): what asks for one, the safety checks, and a
guide built from pages by the LLM, run by the cooking session.

Run: python3 tests/test_howto.py   (also works under pytest)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core

from antigua_core import howto, pineda, recipe_session, settings  # noqa: E402


def test_parse_request():
    last = "How do I fix a running toilet?"
    cases = {
        "walk me through cutting out drywall": "cutting out drywall",
        "can you walk me through how to clean a cast iron pan": "how to clean a cast iron pan",
        "show me how to get dog pee out of carpet": "how to get dog pee out of carpet",
        "how do I kill ants without chemicals step by step": "how to kill ants without chemicals",
        "walk me through it": "how to fix a running toilet",
        "show it on the screen": "how to fix a running toilet",
        "show me how to make lasagna": None,             # the recipe skill's
        "walk me through how to get to the airport": None,
        "how do I fix a running toilet": None,           # answered out loud
        "next": None,
    }
    for text, want in cases.items():
        assert howto.parse_request(text, last) == want, (text, howto.parse_request(text, last))
    assert howto.parse_request("walk me through it") is None   # nothing asked before


def test_safety():
    assert howto.never_together("Mix bleach and white vinegar in a spray bottle.") == "bleach"
    assert howto.never_together("Spray vinegar, then hydrogen peroxide") == "hydrogen peroxide"
    assert howto.never_together("Blot with vinegar and dish soap.") == ""
    c = howto.caution_for("Score the drywall with a utility knife, then cut with a jab saw "
                          "after you turn off the power at the breaker box.")
    assert "sharp blades" in c and "electrical work" in c and c.startswith("Heads up:"), c
    assert howto.caution_for("Blot with a towel and dish soap.") == ""
    assert howto.caution_for("Use 2 tsp of baking soda") == ""      # teaspoons aren't TSP


def _fake(pages, reply):
    howto.gather = lambda q: pages
    return lambda prompt: json.dumps(reply)


def test_build_and_walk_through():
    real = howto.gather
    try:
        llm = _fake([("https://www.bobvila.com/cut-drywall", "Score it. Snap it.")], {
            "title": "How to Cut Drywall", "source": "Bob Vila",
            "supplies": ["utility knife", "drywall T-square", "1. tape measure"],
            "steps": ["Step 1: Measure and mark the cut.", "Score the face paper with a utility knife.",
                      "Snap the board along the score.", "Cut the back paper."]})
        guide = howto.build("how to cut drywall", llm)
        assert guide.kind == "howto" and guide.source == "Bob Vila"
        assert guide.steps[0] == "Measure and mark the cut." and guide.ingredients[2] == "tape measure"
        assert "sharp blades" in guide.caution
        # Cached: a second build doesn't need the LLM.
        assert howto.build("how to cut drywall", None).title == "How to Cut Drywall"

        session, intro = recipe_session.start(guide.title, [guide])
        assert intro.startswith("Here's how to cut drywall, from Bob Vila. It's 4 steps. Heads up:"), intro
        assert "recipe" not in intro.lower() and intro.endswith("Do you have everything?"), intro
        reply = recipe_session.handle("I don't have a drywall T-square")
        assert "hardware store" in reply.text and "taste" not in reply.text, reply.text
        card = pineda.recipe_card(guide)
        assert card["kind"] == "howto" and card["title"] == "Cut Drywall" and card["caution"]
        assert recipe_session.handle("stop the recipe").text == "Okay, I've closed the guide."

        # Refused: the pages had it mixing bleach with vinegar.
        llm = _fake([("https://x.test", "...")], {"title": "Mold", "supplies": ["bleach", "vinegar"],
                    "steps": ["Mix bleach and vinegar.", "Spray.", "Wipe."]})
        try:
            howto.build("how to remove mold", llm)
        except howto.HowtoError as e:
            assert "toxic gas" in str(e)
        else:
            raise AssertionError("expected a refusal")
        # Too thin to walk through.
        llm = _fake([("https://x.test", "...")], {"steps": []})
        try:
            howto.build("how to do the thing", llm)
        except howto.HowtoError as e:
            assert "step-by-step" in str(e)
        else:
            raise AssertionError("expected no guide")
    finally:
        howto.gather = real
        recipe_session.clear()


def test_page_text():
    page = ("<html><nav>Menu Shop</nav><article>" + "<p>Score the paper with a knife.</p>" * 120 +
            "</article><footer>Copyright</footer><script>x()</script></html>")
    text = howto.page_text(page)
    assert "Score the paper" in text and "Menu" not in text and "Copyright" not in text and "x()" not in text


def main():
    settings.configure({})
    test_parse_request()
    test_safety()
    test_page_text()
    test_build_and_walk_through()
    print("PASS — howto suite")


if __name__ == "__main__":
    main()
