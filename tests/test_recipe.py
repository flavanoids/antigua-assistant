#!/usr/bin/env python3
"""Tests for the recipe skill: finding a real recipe, then cooking it by voice.

No network: recipe pages are synthetic, in the shapes real recipe sites
publish (JSON-LD sections, WP Recipe Maker ingredient groups, decimals,
plain-text instructions), and the pipeline test swaps in a finder that
serves them. The dialogue goes
through dispatch_text with a new conversation id per turn, the way the mics
reset it after every follow-up window.

Scaling ("make it for 4", "double it") is covered here too: pure arithmetic
on the published amounts — times, temperatures and pan sizes are never
scaled, and every scaled sentence is built from what the recipe says.

Run: python3 tests/test_recipe.py   (also works under pytest)
"""

import json
import re
import sys
import tempfile
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

settings.configure({"household": [{"name": "Alex"}]})

from antigua_core import pipeline, recipe_session  # noqa: E402
from antigua_core.classify import classify  # noqa: E402
from antigua_core.grounding import recipe_claims_ok  # noqa: E402
from antigua_core.intents.recipe import (parse_cook_command, parse_recipe_request,  # noqa: E402
                                         requested_servings)
from antigua_core.recipe import (Recipe, fmt_qty, parse_amount, parse_recipe_html, parse_yield,  # noqa: E402
                                 rank, scale_line, scale_step_text, spoken_title, step_parts)
from antigua_core.recipe_session import CookSession, timer_minutes  # noqa: E402
from antigua_core.recipe_subs import core_name, match_ingredient  # noqa: E402
from antigua_core.stores import ListStore, MemoryStore, TimerManager  # noqa: E402

def _page(site, recipe, groups=()):
    """A recipe page as sites publish it: og:site_name, the JSON-LD, and (for
    WP Recipe Maker) ingredient groups that only exist in the HTML."""
    html = [f'<meta property="og:site_name" content="{site}" />',
            '<script type="application/ld+json">'
            + json.dumps({"@context": "https://schema.org", "@graph": [
                {"@type": "WebPage", "name": "x"}, dict(recipe, **{"@type": "Recipe"})]})
            + "</script>"]
    for name, n in groups:
        html.append('<div class="wprm-recipe-ingredient-group">'
                    f'<h4 class="wprm-recipe-group-name">{name}</h4>'
                    + '<li class="wprm-recipe-ingredient"></li>' * n + "</div>")
    return "\n".join(html)


def _steps(*texts):
    return [{"@type": "HowToStep", "text": t} for t in texts]


# Synthetic pages in the shapes real sites use (the text is ours).
PAGES = {
    "cheesecake": _page("Test Kitchen - Recipes for Home Cooks", {
        "name": "Classic Cheesecake Recipe",
        "recipeYield": ["12", "12 servings"], "totalTime": "PT10H45M",
        "aggregateRating": {"ratingValue": "4.87", "ratingCount": "1193"},
        "recipeIngredient": [
            "1 1/4 cups graham cracker crumbs", "4 tablespoons granulated sugar",
            "5 tablespoons butter, melted",
            "4 (8 ounce) packages cream cheese, softened",
            "1 1/2 cups (300 grams) granulated sugar", "1/2 cup sour cream",
            "2 tsp vanilla extract", "4 large eggs (room temperature)",
            "Berry sauce, for serving (optional)",
            "Special equipment: 9-inch springform pan"],
        "recipeInstructions": [
            {"@type": "HowToSection", "name": "For the Crust:", "itemListElement": _steps(
                "Preheat the oven to 350&deg;F (175&deg;C). Wrap the outside of a 9-inch springform pan in foil.",
                "Stir the graham cracker crumbs, sugar and melted butter together until the mixture looks like damp sand.",
                "Press the crumbs into the bottom of the pan. Bake 7 minutes, then set aside.")},
            {"@type": "HowToSection", "name": "For the Filling", "itemListElement": _steps(
                "Reduce the oven temperature to 325&deg;F.",
                "Beat the cream cheese until smooth, about 1 minute. Add the sugar, sour cream and vanilla and beat until combined.",
                "Add the eggs one at a time, mixing on low speed after each one until just incorporated. "
                "Scrape down the sides and the bottom of the bowl with a rubber spatula so there are no lumps "
                "left anywhere in the batter. (If a few small lumps remain, press them against the side of the bowl.) "
                "Tap the bowl on the counter to release air bubbles before pouring.",
                "Pour the filling into the crust. Bake for 30 minutes at 325&deg;F, then turn the oven off and "
                "leave the cheesecake inside for 30 minutes more.",
                "Refrigerate at least 6 hours or overnight before slicing.")},
        ]}, groups=[("Crust", 3), ("Filling", 7)]),
    "new_york_cheesecake": _page("Other Kitchen", {
        "name": "Classic New York Cheesecake",
        "recipeYield": ["8", "8 to 10"], "totalTime": "PT2H25M",
        "aggregateRating": {"ratingValue": "4.81", "ratingCount": "1154"},
        "recipeIngredient": ["1 1/2 cups graham cracker crumbs", "32 ounces cream cheese",
                             "2 cups sugar", "6 large eggs"],
        "recipeInstructions": _steps("Make the crust.", "Make the filling.", "Bake and chill.")}),
    "soup": _page("Soup Place", {
        "name": "The Best Chicken Noodle Soup",
        "recipeYield": ["6", "6 servings"], "totalTime": "PT40M",
        "aggregateRating": {"ratingValue": "4.9", "ratingCount": "298"},
        "recipeIngredient": [
            "2 tablespoons butter", "1 large onion, chopped", "2 large carrots, chopped",
            "2 ribs celery, chopped", "4 garlic cloves, minced", "2 bay leaves",
            "3 fresh thyme sprigs or 1/2 teaspoon dried thyme",
            "8 cups low-sodium chicken stock or homemade chicken stock",
            "Fine sea salt and freshly ground black pepper, to taste",
            "1 pound boneless, skinless chicken thighs",
            "5 ounces egg noodles or pasta of choice", "1/4 cup finely chopped fresh parsley"],
        "recipeInstructions": _steps(
            "Melt the butter in a large pot over medium heat. Add the onion, carrots and celery and cook, stirring, until soft, about 5 minutes.",
            "Stir in the garlic, bay leaves and thyme and cook until fragrant, about 1 minute.",
            "Pour in the 8 cups of chicken stock, bring to a simmer and season with salt and pepper.",
            "Add the chicken thighs, cover partially and simmer until the chicken is tender, about 20 minutes.",
            "Move the chicken to a plate. Stir the noodles into the soup and cook until tender, 6 to 10 minutes.",
            "Shred the chicken with two forks, return it to the pot and stir in the parsley.")}),
    "banana_bread": _page("Bread Site", {
        "name": "Banana Banana Bread",
        "recipeYield": ["10", "1 (9x5-inch) loaf"], "prepTime": "PT15M", "cookTime": "PT1H",
        "recipeIngredient": ["2 cups all-purpose flour", "0.25 teaspoon salt", "0.75 cup brown sugar",
                             "2.3333332538605 cups mashed overripe bananas"],
        "recipeInstructions": "1. Preheat oven to 350 degrees F. 2. Mix the flour and salt. "
                              "3. Beat in the sugar and bananas.\nBake for 60 minutes."}),
}


def _load(name):
    return parse_recipe_html(PAGES[name], f"https://example.com/{name}")


REQUEST_CASES = [
    ("how do I make cheesecake", "cheesecake"),
    ("How do you make chicken noodle soup?", "chicken noodle soup"),
    ("give me a recipe for banana bread", "banana bread"),
    ("recipe for tamales", "tamales"),
    ("what's a good recipe for pozole", "pozole"),
    ("cheesecake recipe please", "cheesecake"),
    ("let's bake some cookies", "cookies"),
    ("I want to make chicken enchiladas tonight", "chicken enchiladas"),
    ("how to cook a steak", "steak"),
    ("walk me through making pancakes", "pancakes"),
    ("how do I make a classic New York cheesecake from scratch", "New York cheesecake"),
    # "for N people" is the dish's size, not part of the dish.
    ("how do I make cheesecake for 4 people", "cheesecake"),
    ("let's make pancakes for four", "pancakes"),
    ("give me a recipe for lasagna for 10", "lasagna"),
    # Not recipes.
    ("how do I make money", None),
    ("how do I make money for 20 years", None),
    ("how to make a bed", None),
    ("how do I make the lights brighter", None),
    ("another recipe", None),
    ("what can I use instead of butter", None),
    ("set a timer for ten minutes", None),
]

# The "for 4 people" that rides a recipe request (0 = no size asked for).
SERVINGS_CASES = [
    ("how do I make cheesecake for 4 people", 4),
    ("let's make pancakes for four", 4),
    ("give me a recipe for lasagna for 10", 10),
    ("how do I make money for 20 years", 0),
    ("cheesecake recipe please", 0),
]

COMMAND_CASES = [
    ("next", ("next", None)),
    ("next step please", ("next", None)),
    ("okay I'm ready", ("next", None)),
    ("done, what's next", ("next", None)),
    ("go back", ("back", None)),
    ("previous step", ("back", None)),
    ("repeat that", ("repeat", None)),
    ("say that again", ("repeat", None)),
    ("what was step 3", ("goto", (3, False))),
    ("go to step four", ("goto", (4, True))),
    ("skip to the last step", ("goto", (-1, True))),
    ("start over", ("start_over", None)),
    ("how many steps are left", ("status", None)),
    ("what step am I on", ("status", None)),
    ("read me the ingredients again", ("ingredients", None)),
    ("one at a time", ("checklist", None)),
    ("can you go one by one", ("checklist", None)),
    ("how much salt was I supposed to add", ("how_much", "salt")),
    ("how many eggs do I need", ("how_much", "eggs")),
    ("how much sugar again", ("how_much", "sugar")),
    ("how long in the oven again", ("how_long", "how long in the oven again")),
    ("what temperature is the oven", ("temp", "what temperature is the oven")),
    ("yes", ("yes", False)),
    ("yeah I have everything", ("yes", True)),
    ("no", ("no", None)),
    ("nope that's it", ("no", None)),
    ("I don't have that either", ("no", None)),
    ("no, I don't have sour cream", ("missing", ("sour cream", ["sour cream"]))),
    ("I'm out of eggs and butter", ("missing", ("eggs and butter", ["eggs", "butter"]))),
    ("another recipe", ("another", {"filter": None, "without": None})),
    ("something simpler", ("another", {"filter": "simpler", "without": None})),
    ("is there one without nuts", ("another", {"filter": None, "without": "nuts"})),
    ("I don't like that one", ("another", {"filter": None, "without": None})),
    ("stop the recipe", ("end", None)),
    ("I'm done cooking", ("end", None)),
    ("what can I use instead of sour cream", ("sub_q", "sour cream")),
    # Scaling (docs/recipe_questions_and_scaling_plan.md §2.1).
    ("make it for 4", ("scale", {"servings": 4})),
    ("make the recipe for six people", ("scale", {"servings": 6})),
    ("we're cooking for eight", ("scale", {"servings": 8})),
    ("I'm feeding six people", ("scale", {"servings": 6})),
    ("scale it to 8 servings", ("scale", {"servings": 8})),
    ("double it", ("scale", {"factor": 2.0})),
    ("double the recipe", ("scale", {"factor": 2.0})),
    ("triple it", ("scale", {"factor": 3.0})),
    ("halve it", ("scale", {"factor": 0.5})),
    ("half the recipe", ("scale", {"factor": 0.5})),
    ("make half the recipe", ("scale", {"factor": 0.5})),
    ("make it one and a half times", ("scale", {"factor": 1.5})),
    ("back to the original", ("scale", {"factor": 1.0})),
    ("normal amount", ("scale", {"factor": 1.0})),
    ("how many does it serve", ("scale", {"ask": True})),
    ("how many servings does it make", ("scale", {"ask": True})),
    # Free-form questions (docs/recipe_questions_and_scaling_plan.md §1.3).
    ("can I skip the vanilla extract", ("skip_q", "vanilla extract")),
    ("can I leave the sour cream out", ("skip_q", "sour cream")),
    ("can I make this without the sour cream", ("skip_q", "sour cream")),
    ("do I really need the eggs", ("skip_q", "eggs")),
    ("do I have to use the springform pan", ("skip_q", "springform pan")),
    ("is the vanilla extract optional", ("skip_q", "vanilla extract")),
    ("how do I know when it's done", ("looks_q", "how do i know when it's done")),
    ("is it done yet", ("looks_q", "is it done yet")),
    ("what's it supposed to look like", ("looks_q", "what's it supposed to look like")),
    # Not scaling talk: "for" that isn't people, doubling that isn't food.
    ("make it for me", None),
    ("set a timer for 5 minutes", None),
    ("double the tv volume", None),
    # Not cooking talk.
    ("turn off the lights", None),
    ("play some Bad Bunny", None),
    ("next song", None),
]


# Real-world ingredient lines: (line, qty, qty_hi, unit). qty None = never
# scaled, spoken exactly as written.
AMOUNT_CASES = [
    ("1 1/4 cups graham cracker crumbs", 1.25, None, "cup"),
    ("4 tablespoons granulated sugar", 4.0, None, "tablespoon"),
    ("2 teaspoons vanilla extract", 2.0, None, "teaspoon"),
    ("0.25 teaspoon salt", 0.25, None, "teaspoon"),
    ("1 and 1/2 cups flour", 1.5, None, "cup"),
    ("1-1/2 cups all-purpose flour", 1.5, None, "cup"),
    ("1 to 2 cups shredded cheese", 1.0, 2.0, "cup"),
    ("2-3 pounds tomatoes", 2.0, 3.0, "pound"),
    ("2 - 3 lemons", 2.0, 3.0, None),
    ("2 or 3 anchovies", 2.0, 3.0, None),
    ("4 (8 ounce) packages cream cheese, softened", 4.0, None, "package"),
    ("1 (14.5 ounce) can diced tomatoes", 1.0, None, "can"),
    ("8 cups low-sodium chicken stock or homemade chicken stock", 8.0, None, "cup"),
    ("1/2 cup sour cream", 0.5, None, "cup"),
    ("1/3 cup water", 1 / 3, None, "cup"),
    ("2 1/3 cups mashed overripe bananas", 7 / 3, None, "cup"),
    ("16 ounces cream cheese", 16.0, None, "ounce"),
    ("5 ounces egg noodles or pasta of choice", 5.0, None, "ounce"),
    ("1 pound boneless, skinless chicken thighs", 1.0, None, "pound"),
    ("1 clove garlic, minced", 1.0, None, "clove"),
    ("4 garlic cloves, minced", 4.0, None, None),
    ("2 bay leaves", 2.0, None, None),
    ("3 fresh thyme sprigs or 1/2 teaspoon dried thyme", 3.0, None, None),
    ("1 bunch parsley, stems removed", 1.0, None, "bunch"),
    ("1 head garlic, halved", 1.0, None, "head"),
    ("2 ribs celery, chopped", 2.0, None, "rib"),
    ("2 tablespoons butter", 2.0, None, "tablespoon"),
    ("1 large onion, chopped", 1.0, None, None),
    ("4 large eggs", 4.0, None, None),
    ("1/4 teaspoon salt", 0.25, None, "teaspoon"),
    ("1/2 teaspoon dried thyme", 0.5, None, "teaspoon"),
    # Never scaled.
    ("Fine sea salt and freshly ground black pepper, to taste", None, None, None),
    ("Berry sauce, for serving (optional)", None, None, None),
    ("salt and pepper, to taste", None, None, None),
    ("a handful of walnuts", None, None, None),
    ("8-ounce can tomato sauce", None, None, None),
]

# (line, factor, scaled line, one-time note). Fractions snap to eighths and
# sixths; 3 teaspoons become 1 tablespoon; tiny amounts become a pinch;
# whole things round, with the eggs' yolk tip when they split.
SCALE_CASES = [
    ("1 1/4 cups graham cracker crumbs", 2, "2 1/2 cups graham cracker crumbs", None),
    ("4 tablespoons granulated sugar", 2, "1/2 cup granulated sugar", None),
    ("4 tablespoons granulated sugar", 3, "3/4 cup granulated sugar", None),
    ("2 teaspoons vanilla extract", 3, "2 tablespoons vanilla extract", None),
    ("2 teaspoons vanilla extract", 2, "4 teaspoons vanilla extract", None),
    ("1/2 cup sour cream", 1 / 3, "1/6 cup sour cream", None),
    ("1/2 cup sour cream", 2, "1 cup sour cream", None),
    ("1/4 cup finely chopped fresh parsley", 0.5, "2 tablespoons finely chopped fresh parsley", None),
    ("1/4 teaspoon salt", 0.25, "a pinch of salt", None),
    ("1/4 teaspoon salt", 2, "1/2 teaspoon salt", None),
    ("1 pound boneless, skinless chicken thighs", 0.5,
     "1/2 pound boneless, skinless chicken thighs", None),
    ("16 ounces cream cheese", 2, "2 pounds cream cheese", None),
    ("8 cups low-sodium chicken stock", 0.5, "4 cups low-sodium chicken stock", None),
    ("4 (8 ounce) packages cream cheese, softened", 2,
     "8 packages (8 ounce) cream cheese, softened", None),
    ("4 (8 ounce) packages cream cheese, softened", 0.5,
     "2 packages (8 ounce) cream cheese, softened", None),
    ("1 to 2 cups shredded cheese", 2, "2 to 4 cups shredded cheese", None),
    ("1 to 2 cups shredded cheese", 0.5, "1/2 to 1 cup shredded cheese", None),
    ("2-3 pounds tomatoes", 0.5, "1 to 1 1/2 pounds tomatoes", None),
    ("2 or 3 anchovies", 2, "4 to 6 anchovies", None),
    ("2 or 3 anchovies", 0.5, "1 to 2 anchovies", None),
    ("4 large eggs", 2, "8 large eggs", None),
    ("4 large eggs", 0.5, "2 large eggs", None),
    ("4 large eggs", 0.625, "3 large eggs", "the exact amount is 2 1/2; use 2 eggs plus 1 yolk"),
    ("4 large eggs", 1 / 3, "about 1 large egg", "the exact amount is 1 1/3"),
    ("1 large onion, chopped", 2, "2 large onions, chopped", None),
    ("1 large onion, chopped", 0.5, "about 1 large onion, chopped", None),
    ("1 bay leaf", 2, "2 bay leaves", None),
    ("2 bay leaves", 0.5, "1 bay leaf", None),
    ("2 bay leaves", 1 / 3, "about 1 bay leaf", None),
    ("1 whole chicken, cut into pieces", 2, "2 whole chickens, cut into pieces", None),
    ("2 large carrots, chopped", 0.5, "1 large carrot, chopped", None),
    ("1 clove garlic, minced", 1 / 3, "1/3 clove garlic, minced", None),
    ("4 garlic cloves, minced", 0.5, "2 garlic cloves, minced", None),
    ("3 fresh thyme sprigs or 1/2 teaspoon dried thyme", 0.5,
     "about 2 fresh thyme sprigs or 1/2 teaspoon dried thyme", None),
    ("2 cups all-purpose flour", 0.125, "1/4 cup all-purpose flour", None),
    ("Fine sea salt and freshly ground black pepper, to taste", 2,
     "Fine sea salt and freshly ground black pepper, to taste", None),
]


def _check_parsers():
    failed = 0
    for text, want in REQUEST_CASES:
        got = parse_recipe_request(text)
        if got != want:
            failed += 1
            print(f"[FAIL] request {text!r}: {got!r}, expected {want!r}")
    for text, want in SERVINGS_CASES:
        got = requested_servings(text)
        if got != want:
            failed += 1
            print(f"[FAIL] servings {text!r}: {got!r}, expected {want!r}")
    for text, want in COMMAND_CASES:
        got = parse_cook_command(text)
        if got != want:
            failed += 1
            print(f"[FAIL] command {text!r}: {got!r}, expected {want!r}")
    return failed


def _check_amounts():
    failed = 0
    for line, qty, hi, unit in AMOUNT_CASES:
        a = parse_amount(line)
        if (a["qty"], a["qty_hi"], a["unit"]) != (qty, hi, unit):
            failed += 1
            print(f"[FAIL] amount {line!r}: {(a['qty'], a['qty_hi'], a['unit'])}, "
                  f"expected {(qty, hi, unit)}")
    for line, factor, want, note in SCALE_CASES:
        got = scale_line(line, parse_amount(line), factor)
        if got != (want, note):
            failed += 1
            print(f"[FAIL] scale {line!r} x{factor}: {got!r}, expected {(want, note)!r}")
    for v, want in ((2.5, "2 1/2"), (1 / 6, "1/6"), (4, "4"), (1 / 3, "1/3"),
                    (0.75, "3/4"), (0.4166667, "3/8"), (0, "0")):
        if fmt_qty(v) != want:
            failed += 1
            print(f"[FAIL] fmt_qty({v}): {fmt_qty(v)!r}, expected {want!r}")
    for s, want in (("serves 12", (12.0, 0.0, "")), ("serves 8 to 10", (8.0, 10.0, "")),
                    ("makes 1 loaf", (1.0, 0.0, "loaf")), ("", (0.0, 0.0, ""))):
        if parse_yield(s) != want:
            failed += 1
            print(f"[FAIL] parse_yield({s!r}): {parse_yield(s)}, expected {want}")
    # Amounts written inside a step: scaled when they match an ingredient
    # exactly, flagged once when they don't, never when they're time or heat.
    amounts = [parse_amount(i) for i in ("2 tablespoons butter", "8 cups chicken stock")]
    for text, factor, want in (
        ("Pour in the 8 cups of chicken stock and simmer.", 0.5,
         "Pour in the 4 cups of chicken stock and simmer."),
        ("Add 3 cups of water to the 8 cups of stock.", 0.5,
         "Add 3 cups (that's for the original recipe) of water to the 4 cups of stock."),
        ("Bake for 30 minutes at 350 degrees in a 9-inch pan.", 2,
         "Bake for 30 minutes at 350 degrees in a 9-inch pan."),
    ):
        got = scale_step_text(text, amounts, factor)
        if got != want:
            failed += 1
            print(f"[FAIL] step {text!r} x{factor}: {got!r}, expected {want!r}")
    return failed


def _check_pages():
    cake = _load("cheesecake")
    assert cake.source == "Test Kitchen", cake.source          # tagline dropped
    assert spoken_title(cake.title) == "classic cheesecake", cake.title
    assert cake.servings == "serves 12" and cake.total_min == 645, (cake.servings, cake.total_min)
    assert cake.groups == [["Crust", 0, 2], ["Filling", 3, 8]], cake.groups
    assert cake.ingredients[0] == "1 1/4 cups graham cracker crumbs", cake.ingredients[0]
    assert "4 (8 ounce) packages cream cheese, softened" in cake.ingredients
    assert "2 teaspoons vanilla extract" in cake.ingredients
    assert "Berry sauce, for serving, optional" in cake.ingredients, cake.ingredients
    # Equipment is not an ingredient; grams and asides are not spoken.
    assert not any("equipment" in i.lower() for i in cake.ingredients)
    assert "1 1/2 cups granulated sugar" in cake.ingredients, cake.ingredients
    assert "4 large eggs" in cake.ingredients, cake.ingredients
    assert cake.equipment == ["9-inch springform pan"], cake.equipment
    assert cake.wait == "chill at least 6 hours", cake.wait
    assert cake.steps[0].startswith("For the crust: Preheat the oven to 350 degrees."), cake.steps[0]
    assert "175" not in cake.steps[0], cake.steps[0]
    assert cake.steps[3].startswith("For the filling: Reduce"), cake.steps[3]
    # The recipe's own steps, numbered as on the page; the long one is
    # spoken in parts, at sentence boundaries, every word kept.
    assert len(cake.steps) == 8, cake.steps
    parts = step_parts(cake.steps[5])
    assert len(parts) == 4 and parts[2].startswith("If a few small lumps remain"), parts
    assert " ".join(parts) == cake.steps[5]
    assert step_parts(cake.steps[0]) == [cake.steps[0]]

    bread = _load("banana_bread")
    assert bread.ingredients[1:] == ["1/4 teaspoon salt", "3/4 cup brown sugar",
                                     "2 1/3 cups mashed overripe bananas"], bread.ingredients
    assert bread.servings == "makes 1 loaf" and bread.total_min == 75, (bread.servings, bread.total_min)
    assert bread.steps == ["Preheat oven to 350 degrees.", "Mix the flour and salt.",
                           "Beat in the sugar and bananas.", "Bake for 60 minutes."], bread.steps

    soup = _load("soup")
    assert soup.servings == "serves 6" and soup.total_min == 40, (soup.servings, soup.total_min)
    assert core_name("1 pound boneless, skinless chicken thighs") == "chicken thighs"
    assert core_name("5 ounces egg noodles or pasta of choice") == "egg noodles"
    assert match_ingredient("noodles", soup.ingredients) == [10]
    # "stir about 5 minutes" is watched, not timed; 20 minutes of simmering is.
    assert timer_minutes(soup.steps[0]) is None, soup.steps[0]
    assert timer_minutes(soup.steps[3]) == 20, soup.steps[3]

    # The yield and per-line amounts the scaling works from (§2.2).
    assert (cake.yield_n, cake.yield_hi, cake.yield_unit) == (12.0, 0.0, ""), cake.servings
    assert (ny_y := _load("new_york_cheesecake")).yield_n == 8 and ny_y.yield_hi == 10
    assert bread.yield_n == 1 and bread.yield_unit == "loaf", bread.servings
    assert soup.yield_n == 6 and len(soup.amounts) == len(soup.ingredients)
    assert cake.amounts[0]["qty"] == 1.25 and cake.amounts[0]["unit"] == "cup"
    # Recipes cached before amounts/yield existed still scale: the missing
    # fields are parsed back in from the speakable lines.
    old = Recipe.from_dict({k: v for k, v in cake.to_dict().items()
                            if k not in ("amounts", "yield_n", "yield_hi", "yield_unit")})
    assert old.yield_n == 12 and len(old.amounts) == len(cake.ingredients)
    assert old.amounts[3]["qty"] == 4.0 and old.amounts[3]["unit"] == "package"  # 4 (8 ounce)

    # Ranking prefers a title that is what was asked for.
    ny = _load("new_york_cheesecake")
    assert ny.servings == "serves 8 to 10", ny.servings
    assert rank([ny, cake], "cheesecake")[0] is cake
    assert rank([soup], "cheesecake") == []
    return 0


class _FakeFinder:
    def __init__(self):
        self.asked = []
        self.pages = {
            "cheesecake": [_load("new_york_cheesecake"), _load("cheesecake")],
            "chicken noodle soup": [_load("soup")],
            "banana bread": [_load("banana_bread")],
        }

    def cached(self, dish):
        return True

    def find(self, dish):
        self.asked.append(dish)
        return rank(self.pages.get(dish.lower(), []), dish)


def _check_dialogue():
    tmp = Path(tempfile.mkdtemp(prefix="antigua_test_"))
    llm = []

    def ask_llm_stream(transcript, **kw):
        llm.append(transcript)
        yield "Okay."

    def synthesize(text, lang="en"):
        p = tmp / "out.wav"
        p.write_bytes(b"RIFF")
        return str(p)

    lists = ListStore(path=tmp / "lists.json")
    timers = TimerManager()
    finder = _FakeFinder()
    pipeline.init(pipeline.Backend(
        transcribe=lambda p: {"text": "", "time_s": 0.0},
        synthesize=synthesize,
        ask_llm_stream=ask_llm_stream,
        audio_url_base=lambda: "http://test:0",
        memory_store=MemoryStore(path=tmp / "memories.json"),
        list_store=lists,
        timers=timers,
        weather_cache=None,
        news_cache=None,
        recipes=finder,
    ))
    settings.SEARCH_ENABLED = True
    n = [0]
    asked = [False]   # the last turn's expects_reply

    def say(text):
        n[0] += 1
        llm.clear()
        r = pipeline.dispatch_text(text, conversation_id=f"c{n[0]}", quiet=True)
        asked[0] = r.get("expects_reply", False)
        return r.get("response", "")

    def expect(text, *parts):
        got = say(text)
        for p in parts:
            assert p.lower() in got.lower(), f"{text!r} -> {got!r}; wanted {p!r}"
        assert not llm, f"{text!r} went to the LLM"
        return got

    recipe_session.clear()
    got = expect("how do I make cheesecake", "recipe for classic cheesecake from Test Kitchen",
                 "serves 12", "For the crust:", "For the filling:", "a 9-inch springform pan",
                 "Do you have everything?")
    assert "Heads up: it needs to chill at least 6 hours" in got, got
    assert asked[0], "a recipe question takes its answer without the wake word"

    expect("no, I don't have sour cream",
           "Instead of the sour cream, you can use the same amount of plain Greek yogurt")
    expect("no", "Or you can use the same amount of plain yogurt")
    expect("yes", "plain yogurt it is", "Anything else missing?")
    expect("I'm out of eggs", "flaxseed")
    expect("I don't have that either", "applesauce")
    expect("nope", "hard to do without", "shopping list")
    expect("yes", "Added eggs to your shopping list", "keep going anyway")
    assert "eggs" in lists.read("shopping"), lists.read("shopping")
    expect("keep going", "Anything else missing?")
    expect("no that's it", "Ready for step one?")
    assert any(a.startswith("Step 1 of 8.") for a in recipe_session.likely_next()), \
        recipe_session.likely_next()
    expect("yes", "Step 1 of 8.", "Preheat the oven to 350 degrees", "say Alexa, next")
    assert asked[0], "while a recipe is open, a step takes the next word without the wake word"
    # "Okay" / "not yet" to the step prompt: acknowledged, no move, no "didn't catch that".
    expect("okay", "Okay.")
    expect("not yet", "take your time")
    expect("repeat that", "Step 1 of 8.")
    # Prefetch: what "next" will say, worked out without moving the session.
    ahead = recipe_session.likely_next()
    assert len(ahead) == 1 and ahead[0].startswith("Step 2 of 8."), ahead
    assert recipe_session.current().step == 0, "likely_next moved the session"
    assert expect("next", "Step 2 of 8.") == ahead[0], "prefetched text must match what's spoken"
    expect("go back", "Step 1 of 8.")
    expect("what was step 5", "Step 5:", "sour cream", "plain yogurt instead of the sour cream")
    expect("next", "Step 2 of 8.")
    got = expect("how much sugar", "4 tablespoons granulated sugar for the crust",
                 "1 1/2 cups granulated sugar for the filling")
    expect("how much sour cream was it", "1/2 cup sour cream", "using plain yogurt instead")
    expect("what temperature is the oven", "350 degrees")
    expect("how many steps are left", "step 2 of 8", "6 more")
    expect("go to step 7", "Step 7 of 8.", "Want me to set a timer for 30 minutes?")
    expect("yes", "Timer set")
    active = timers.list_active()
    assert any("cheesecake" in (r.get("label") or "") for r in active), active
    expect("how long in the oven again", "Bake for 30 minutes at 325 degrees")

    # Other skills still work mid-recipe.
    say("set a timer for 5 minutes")
    assert len(timers.list_active()) == 2, timers.list_active()
    for other in ("how long is left on the timer", "what temperature is it outside",
                  "how many people live in Texas", "stop"):
        got = say(other)
        assert "recipe" not in got.lower() and "step" not in got.lower(), (other, got)
    assert recipe_session.current() is not None, "a bare stop doesn't close the recipe"

    # A new dish mid-recipe asks first.
    expect("how do I make chicken noodle soup", "You're on step 7 of the classic cheesecake",
           "Switch to chicken noodle soup?")
    expect("no", "staying with the classic cheesecake")
    expect("how do I make chicken noodle soup", "Switch to chicken noodle soup?")
    expect("yes", "recipe for chicken noodle soup from Soup Place", "Do you have everything?")
    assert finder.asked[-1] == "chicken noodle soup", finder.asked
    # Onion has substitutes; celery can be left out once they run out.
    expect("I'm missing celery", "fennel")
    expect("no", "more onion")
    expect("no", "leave out the celery", "Anything else missing?")
    expect("another recipe", "That's all the chicken noodle soup recipes I found")
    expect("that's all", "Ready for step one?")
    expect("ready", "Step 1 of 6.", "leaving out the celery")

    expect("stop the recipe", "closed the chicken noodle soup recipe")
    assert recipe_session.current() is None
    assert not asked[0], "a closed recipe doesn't keep the mic open"

    # With no session, cooking words go back to the other skills.
    say("next")
    assert recipe_session.current() is None

    # No recipe online: say so, never make one up.
    got = say("how do I make unicorn stew")
    assert "couldn't find a recipe online for unicorn stew" in got, got
    assert not llm and recipe_session.current() is None

    # One at a time: got it / don't have it, then the missing ones.
    expect("how do I make chicken noodle soup", "Do you have everything?")
    expect("one at a time", "one at a time", "2 tablespoons butter.")
    expect("got it", "1 large onion.")
    expect("I don't have it", "2 large carrots.")
    for _ in range(9):
        say("yes")
    expect("yes", "Instead of the onion, you can use 1 tablespoon of onion powder")
    recipe_session.clear()

    # Persistence: a restart mid-recipe keeps the place.
    expect("how do I make cheesecake", "Do you have everything?")
    expect("yes", "Ready for step one?")
    expect("yes", "Step 1 of 8.")
    expect("next", "Step 2 of 8.")
    recipe_session._loaded = False
    recipe_session._session = None
    expect("repeat that", "Step 2 of 8.")
    recipe_session.clear()
    return 0


def _check_session_math():
    cake, ny, bread = _load("cheesecake"), _load("new_york_cheesecake"), _load("banana_bread")
    # Decision 3: a factor doesn't outlive a recipe change; a servings
    # request does, re-applied to whatever the next recipe serves.
    s = CookSession("cheesecake", [cake, ny])
    s._cmd_scale({"factor": 2.0})
    s.idx = 1
    s._carry_scale()
    assert s.scale == 1.0 and s.target_servings == 0, (s.scale, s.target_servings)
    s._cmd_scale({"servings": 4})             # the New York one serves 8
    assert abs(s.scale - 0.5) < 1e-9 and s.target_servings == 4
    s.idx = 0
    s._carry_scale()                          # ...and the classic one serves 12
    assert abs(s.scale - 1 / 3) < 1e-9 and s.target_servings == 4
    # Eggs don't divide evenly: the note says what rounding did.
    r = s._cmd_scale({"factor": 0.625})
    assert "Eggs don't divide evenly" in r.text and "use 2 eggs plus 1 yolk" in r.text, r.text
    assert "Back to the original amounts." == s._cmd_scale({"factor": 1.0}).text
    # A loaf can't be scaled by servings, but it doubles.
    b = CookSession("banana bread", [bread])
    assert "makes one loaf" in b._cmd_scale({"servings": 4}).text
    assert "I've doubled" in b._cmd_scale({"factor": 2.0}).text
    assert b._cmd_scale({"ask": True}).text == "It makes 2 loaves, scaled from 1 loaf."
    # A last step that already says "Enjoy!" doesn't get a second one.
    b.recipe.steps[-1] = "Slice and serve. Enjoy!"
    last = b._say_step(len(b.recipe.steps) - 1).text
    assert last.lower().count("enjoy") == 1, last
    # Substitutions read as "you can use <thing>", never "use for each egg, ...".
    from antigua_core.recipe_subs import SUBSTITUTES
    for name, alts in SUBSTITUTES.items():
        for _, how in alts:
            for said in (how.values() if isinstance(how, dict) else [how]):
                assert not (said or "").lower().startswith(("for each", "per ")), (name, said)
    return 0


def _check_scaling():
    """The whole scaling dialogue, through dispatch_text like the mics hear it:
    a soup for 3, doubled, re-sized for 4, undone, restarted, then the
    "for 4" following into the next recipes. Substitution amounts stay
    per-unit and never get scaled."""
    tmp = Path(tempfile.mkdtemp(prefix="antigua_test_"))
    llm = []

    def ask_llm_stream(transcript, **kw):
        llm.append(transcript)
        yield "Okay."

    def synthesize(text, lang="en"):
        p = tmp / "out.wav"
        p.write_bytes(b"RIFF")
        return str(p)

    pipeline.init(pipeline.Backend(
        transcribe=lambda p: {"text": "", "time_s": 0.0},
        synthesize=synthesize,
        ask_llm_stream=ask_llm_stream,
        audio_url_base=lambda: "http://test:0",
        memory_store=MemoryStore(path=tmp / "memories.json"),
        list_store=ListStore(path=tmp / "lists.json"),
        timers=TimerManager(),
        weather_cache=None,
        news_cache=None,
        recipes=_FakeFinder(),
    ))
    settings.SEARCH_ENABLED = True
    n = [0]

    def say(text):
        n[0] += 1
        llm.clear()
        r = pipeline.dispatch_text(text, conversation_id=f"s{n[0]}", quiet=True)
        return r.get("response", "")

    def expect(text, *parts):
        got = say(text)
        for p in parts:
            assert p.lower() in got.lower(), f"{text!r} -> {got!r}; wanted {p!r}"
        assert not llm, f"{text!r} went to the LLM"
        return got

    recipe_session.clear()
    # "for 3 people" scales the recipe from the very first sentence, and the
    # intro says where times and pans come from.
    expect("how do I make chicken noodle soup for 3 people",
           "It serves 3, scaled from 6",
           "Cooking times and pan size are from the original recipe",
           "1 tablespoon butter", "1/2 pound boneless, skinless chicken thighs",
           "4 cups low-sodium chicken stock")
    expect("yes", "Ready for step one?")
    expect("yes", "Step 1 of 6.")
    # A step's own amount is scaled when it matches an ingredient exactly.
    got = expect("go to step 3", "Step 3 of 6.", "Pour in the 4 cups of chicken stock")
    assert "original recipe" not in got.lower(), got
    expect("how much butter", "1 tablespoon butter")
    # Substitution amounts are ratios, not recipe amounts: unchanged.
    expect("what can I use instead of butter", "three quarters as much oil")

    # Doubling mid-recipe: confirm what changed, then read it back on a yes.
    expect("double it", "I've doubled the ingredients",
           "Cooking times and pan size stay the same",
           "You may need a bigger pan", "Want me to read the new amounts?")
    expect("yes", "Here are the new amounts", "1/4 cup butter",
           "2 large onions", "16 cups low-sodium chicken stock")
    expect("how much butter", "1/4 cup butter")
    # A servings request replaces the factor; a no carries on with cooking.
    expect("make it for 4", "Okay, scaled for 4, two thirds of the recipe",
           "You may need a smaller pan", "Want me to read the new amounts?")
    expect("no", "Okay.")
    expect("how much butter", "1 1/3 tablespoons butter")
    expect("how many does it serve", "It serves 4, scaled from 6.")
    # Asking for 100 servings of a 6-serving soup: no.
    expect("make it for 100", "That's a big change")
    expect("back to the original", "Back to the original amounts.")
    expect("how many does it serve", "It serves 6.")
    # The size survives a restart, the way a mid-recipe "next" does.
    expect("double it", "I've doubled the ingredients")
    recipe_session._loaded = False
    recipe_session._session = None
    expect("how much butter", "1/4 cup butter")
    expect("how many does it serve", "It serves 12, scaled from 6.")

    # The "for 4" follows into a new dish, through the switch prompt.
    expect("make it for 4", "Okay, scaled for 4, two thirds of the recipe")
    expect("how do I make cheesecake", "Switch to cheesecake?")
    expect("yes", "recipe for classic cheesecake from Test Kitchen",
           "It serves 4, scaled from 12", "3/8 cup graham cracker crumbs",
           "eggs don't divide evenly: the exact amount is 1 1/3")
    # Swaps keep working on scaled lines.
    expect("I don't have sour cream",
           "you can use the same amount of plain Greek yogurt")
    expect("no", "Or you can use the same amount of plain yogurt")
    expect("yes", "plain yogurt it is", "Anything else missing?")
    expect("no that's it", "Ready for step one?")
    expect("how much sour cream", "1/6 cup sour cream", "using plain yogurt instead")
    # "another recipe" re-applies the 4 servings to the New York one (8 to 10).
    expect("another recipe", "Okay, here's another one",
           "It serves 4 to 5, scaled from 8 to 10",
           "3/4 cup graham cracker crumbs", "1 pound cream cheese", "3 large eggs")
    expect("stop the recipe", "closed the classic new york cheesecake recipe")

    # A loaf recipe says so rather than scaling by servings.
    expect("how do I make banana bread for 4 people", "It makes 1 loaf",
           "This recipe makes one loaf. I can double or halve it.")
    recipe_session.clear()
    return 0


def _check_claims():
    """recipe_claims_ok: an answer may only state numbers the recipe states."""
    s = CookSession("cheesecake", [_load("cheesecake")])
    s.stage, s.step = "steps", 1      # on step 2 of 8
    ctx = s.question_context()

    def ok(sent):
        return recipe_claims_ok(sent, ctx)

    # General kitchen technique with no numbers is allowed (decision 1).
    assert ok("A hand mixer works; it just takes longer.")
    assert ok("The recipe doesn't say.")
    # Numbers the recipe does state, however they're said.
    assert ok("Bake it at 325 degrees.")
    assert ok("Bake 7 minutes, then set aside.")
    assert ok("Refrigerate it at least 6 hours or overnight.")
    assert ok("Bake for 30 minutes at 325 degrees, then leave it in the oven for 30 minutes more.")
    assert ok("Beat in one and a half cups of the sugar.")
    assert ok("That's in step 3.")
    # Numbers the recipe doesn't state: invented amounts...
    assert not ok("Bake it for 5 more minutes.")
    assert not ok("About five more minutes.")          # spelled out
    assert not ok("Bake it at 400 degrees.")
    assert not ok("Bake for an hour.")                 # it says 30 minutes
    assert not ok("Use a quarter cup of the sugar.")   # a unit with the wrong value
    assert not ok("It makes 3 servings.")             # a bare number from nowhere
    assert not ok("That's in step 20.")                # there are 9 steps
    # Scaled: the context speaks the scaled amounts, so those are the ones
    # that pass.
    s._set_scale(0.5, 6)
    ctx = s.question_context()
    assert "Amounts are half of the original" in ctx
    assert "5/8 cup graham cracker crumbs" in ctx
    assert ok("Use 5/8 cup of the crumbs.")
    assert not ok("Use 1 1/4 cups of the crumbs.")
    return 0


def _check_questions():
    """Free-form questions: claimed and answered from the recipe, or not."""
    tmp = Path(tempfile.mkdtemp(prefix="antigua_test_"))
    llm = []

    def ask_llm_stream(transcript, **kw):
        llm.append((transcript, kw))
        yield "Okay."

    def synthesize(text, lang="en"):
        p = tmp / "out.wav"
        p.write_bytes(b"RIFF")
        return str(p)

    pipeline.init(pipeline.Backend(
        transcribe=lambda p: {"text": "", "time_s": 0.0},
        synthesize=synthesize,
        ask_llm_stream=ask_llm_stream,
        audio_url_base=lambda: "http://test:0",
        memory_store=MemoryStore(path=tmp / "memories.json"),
        list_store=ListStore(path=tmp / "lists.json"),
        timers=TimerManager(),
        weather_cache=None,
        news_cache=None,
        recipes=_FakeFinder(),
    ))
    settings.SEARCH_ENABLED = True
    n = [0]

    def say(text):
        n[0] += 1
        llm.clear()
        r = pipeline.dispatch_text(text, conversation_id=f"q{n[0]}", quiet=True)
        return r.get("response", "")

    def expect(text, *parts):
        got = say(text)
        for p in parts:
            assert p.lower() in got.lower(), f"{text!r} -> {got!r}; wanted {p!r}"
        assert not llm, f"{text!r} went to the LLM"
        return got

    recipe_session.clear()
    say("how do I make cheesecake")
    expect("I don't have sour cream", "Instead of the sour cream", "Greek yogurt")
    expect("no", "plain yogurt")
    expect("yes", "plain yogurt it is")
    expect("no that's it", "Ready for step one?")
    expect("ready", "Step 1 of 8.")

    # Deterministic answers (§1.3), no LLM.
    expect("can I skip the vanilla extract", "You can leave out the vanilla extract")
    expect("can I skip the sour cream", "You're using plain yogurt instead of the sour cream")
    expect("do I really need the eggs", "The eggs are hard to do without")
    expect("how do I know when it's done",
           "until the mixture looks like damp sand", "That's in step 2")
    expect("next", "Step 2 of 8.")
    got = expect("is it done yet", "damp sand")
    assert "That's in step" not in got, got      # the cue is in the current step

    # The rest go to the LLM grounded in the recipe (§1.1-§1.2).
    got = say("can I use a hand mixer")
    assert len(llm) == 1, llm
    kw = llm[0][1]
    assert kw.get("grounding_mode") == "recipe", kw
    assert kw.get("extra_context") == kw.get("grounding_context")
    qctx = kw["grounding_context"]
    assert "Recipe being cooked: Classic Cheesecake Recipe from Test Kitchen" in qctx, qctx
    assert "The cook is on step 2 of 8." in qctx, qctx
    assert "(using plain yogurt instead)" in qctx, qctx
    assert "springform pan" in qctx, qctx            # the equipment line
    assert kw.get("max_tokens_override") == settings.RECIPE_QA_MAX_TOKENS, kw
    assert got == "Okay.", got

    say("why do I need a water bath")
    assert llm and llm[-1][1].get("grounding_mode") == "recipe", llm
    say("can I bake it at 400 instead")
    assert llm and llm[-1][1].get("grounding_mode") == "recipe", llm

    # Not claimed: a question in the kitchen that isn't about the cooking.
    for other in ("tell me a joke", "who was Frida Kahlo",
                  "how many people live in Texas", "is the store open"):
        got = say(other)
        # (Jokes come from the curated bank now, so not every one reaches the LLM.)
        assert not any(kw.get("grounding_mode") for _, kw in llm), (other, llm)
        assert not re.search(r"\bstep \d", got, re.I), (other, got)

    # The Q&A didn't lose the cook's place.
    expect("next", "Step 3 of 8.")

    # The context carries the scale and the swaps (§2.4).
    expect("halve it", "I've halved the ingredients", "Want me to read the new amounts?")
    say("yes")
    qctx = recipe_session.current().question_context()
    assert "Amounts are half of the original" in qctx, qctx
    assert "5/8 cup graham cracker crumbs" in qctx, qctx
    assert "(using plain yogurt instead)" in qctx, qctx

    recipe_session.clear()
    return 0


def _check_spoken():
    """What Kokoro actually gets: fractions as words, openers left whole."""
    from antigua_core.tts_text import clean_for_tts
    failed = 0
    cases = [
        ("1/8 teaspoon cinnamon", "one eighth teaspoon cinnamon"),
        ("1/4 cup sugar", "one quarter cup sugar"),
        ("2 1/4 cups milk", "two and a quarter cups milk"),
        ("Great. Ready for step one?", "Great. Ready for step one?"),
        ("Sorry, I couldn't find that.", "Sorry, I couldn't find that."),
        ("Looks like it needs to chill.", "Looks like it needs to chill."),
        ("Sure, here it is.", "Here it is."),
    ]
    for text, want in cases:
        got = clean_for_tts(text)
        if got != want:
            failed += 1
            print(f"[FAIL] spoken {text!r}: {got!r}, expected {want!r}")
    return failed


def _check_routing():
    failed = 0
    cases = [
        ("how do I make cheesecake", "recipe"),
        ("recipe for tamales", "recipe"),
        ("how to make money", "llm"),       # unchanged: not food, not a recipe
        ("how to make a bed", "llm"),
        ("what can I use instead of butter", "substitution"),
        ("set a timer for ten minutes", "timer_set"),
    ]
    for text, want in cases:
        got = classify(text, search_enabled=True)
        if got != want:
            failed += 1
            print(f"[FAIL] route {text!r}: {got!r}, expected {want!r}")
    return failed


def _check_swaps_and_parts():
    """2026-10-06: "can I use X instead of Y" from the table, never the web or
    the model; synonyms; long steps spoken in parts."""
    from antigua_core.intents.recipe import parse_cook_command
    for text, want in [
        ("can I use olive oil instead of butter", ("swap_q", ("olive oil", "butter"))),
        ("can I substitute olive oil for butter", ("swap_q", ("olive oil", "butter"))),
        ("can I replace the butter with olive oil", ("swap_q", ("olive oil", "butter"))),
        ("would coconut oil work instead of butter", ("swap_q", ("coconut oil", "butter"))),
        ("olive oil instead of butter?", ("swap_q", ("olive oil", "butter"))),
        ("what can I use instead of butter", ("sub_q", "butter")),
        ("keep the butter", ("keep", "butter")),
        ("read the whole step", ("whole_step", None)),
    ]:
        assert parse_cook_command(text) == want, (text, parse_cook_command(text))
    assert (parse_cook_command("can I use a hand mixer for this") or ("",))[0] != "swap_q"

    cake = CookSession("cheesecake", [_load("cheesecake")])
    cake.stage, cake.step = "steps", 0
    said = cake.handle("can I use olive oil instead of butter").text
    assert said.startswith("Yes. Instead of the butter, use three quarters as much olive oil"), said
    assert "You're using olive oil instead of the butter" in cake._say_step(1).text
    # "swap butter for olive oil": the recipe's one is what gets replaced.
    assert "already using olive oil" in cake.handle("could I swap olive oil for butter").text
    assert cake.handle("keep the butter").text == "Okay, the butter it is, not the olive oil."
    assert not cake.swaps
    said = cake.handle("can I use honey instead of the sour cream").text
    assert said.startswith("I don't have a tested swap of honey for the sour cream. What I know works"), said
    assert cake.handle("can I use honey instead of maple syrup").text == "This recipe doesn't use maple syrup."

    # Creamed with the sugar: oil can't do what the butter does there.
    creamed = Recipe(title="Butter Cookies", source="Test", url="x",
                     ingredients=["1 cup butter, softened", "1 cup sugar", "2 cups flour"],
                     steps=["Preheat the oven to 350 degrees.", "Cream the butter and sugar until fluffy.",
                            "Mix in the flour. Bake 10 minutes."])
    s = CookSession("butter cookies", [creamed])
    said = s.handle("can I use olive oil instead of butter").text
    assert said.startswith("Not in this one: this recipe beats the butter with the sugar"), said
    assert "solid coconut oil" in said and not s.swaps, said

    # Synonyms: "broth" finds the stock.
    soup = CookSession("chicken noodle soup", [_load("soup")])
    assert "chicken stock" in (soup.how_much("broth") or ""), soup.how_much("broth")

    # Parts: step 6 of the cheesecake is four sentences, spoken one at a time.
    cake = CookSession("cheesecake", [_load("cheesecake")])
    first = cake.handle("go to step 6").text
    assert first.startswith("Step 6 of 8, in four parts. First: Add the eggs"), first
    assert "say Alexa, next" not in first, first          # no prompt between parts
    assert cake.handle("next").text.startswith("Then: Scrape down")
    assert cake.handle("repeat that").text.startswith("Then: Scrape down")
    assert cake.handle("go back").text.startswith("Step 6 of 8, in four parts. First:")
    whole = cake.handle("read the whole step").text
    assert whole.startswith("Step 6: Add the eggs") and "Tap the bowl" in whole, whole
    assert cake.handle("next").text.startswith("Step 7 of 8.")
    return 0


def _check_cookbook():
    """The offline cookbook: only popular recipes, found by name or alias,
    and a hit needs no network."""
    from antigua_core.recipe import Cookbook, RecipeFinder, dish_key
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server" / "scripts"))
    from build_cookbook import load_dishes
    assert dish_key("Homemade Pancakes") == dish_key("pancake") == "pancake"
    assert dish_key("the best egg drop soup") == "egg-drop-soup"
    assert dish_key("Bánh mì") == dish_key("banh mi") == "banh-mi"
    accented = _load("cheesecake")
    accented.title = "Bánh Mì (Vietnamese Sandwich)"
    assert rank([accented], "banh mi"), "accents in a title must not hide it"

    with tempfile.TemporaryDirectory() as tmp:
        book = Cookbook(Path(tmp))
        good, meh = _load("cheesecake"), _load("new_york_cheesecake")
        good.rating, good.rating_count = 4.8, 900
        meh.rating, meh.rating_count = 5.0, 3          # perfect, but 3 people isn't popular
        assert book.put("cheesecake", [meh]) == 0 and not book.has("cheesecake")
        assert book.put("cheesecake", [good, meh], aliases=["new york cheesecake"]) == 1
        assert book.has("Cheesecakes") and book.has("New York cheesecake")
        assert [r.url for r in book.get("new york cheesecake")] == [good.url]

        finder = RecipeFinder(cookbook=book)
        def offline(_dish):
            raise AssertionError("a cookbook hit went to the web")
        finder._search = offline
        assert finder.cached("cheesecake")
        assert finder.find("homemade cheesecake")[0].title == good.title

    dishes = load_dishes()
    keys = [dish_key(d) for d, _, _ in dishes]
    assert len(keys) == len(set(keys)) and len(dishes) > 300, len(dishes)
    assert any(d == "egg drop soup" for d, _, _ in dishes)
    return 0


if __name__ == "__main__":
    failed = (_check_parsers() + _check_amounts() + _check_pages() + _check_session_math()
              + _check_claims() + _check_routing() + _check_dialogue() + _check_scaling()
              + _check_questions() + _check_spoken() + _check_swaps_and_parts() + _check_cookbook())
    if failed:
        print(f"{failed} failure(s)")
        sys.exit(1)
    print("recipe: all passed")
