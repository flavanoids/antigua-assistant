# Skill: Recipes (step by step)

**Status:** Active (2026-09-30)
**Pipeline stage:** Bypass, with one grounded exception: free-form questions
are answered by the LLM from the recipe's own text, and every number it says
is checked against the recipe in code.
**Plan:** [`docs/recipe_skill_plan.md`](../../docs/recipe_skill_plan.md)

---

## Hard rule

Every recipe comes from a real recipe site and is read as published. The LLM
never writes one, never fills in a missing amount or step, and there's no
fallback to a search summary. If no page has a recipe, Antigua says she
couldn't find one online. If you don't like a recipe, ask for another.

---

## What It Does

1. **Find**: SearXNG searches "<dish> recipe". The top ~10 pages are fetched in
   parallel, and the ones with schema.org `Recipe` JSON-LD are parsed:
   ingredients, steps, times, servings, rating. They're ranked by how well the
   title fits the dish, then rating, then search order, with a penalty for very
   long recipes. Results are cached per dish for 14 days.
   **The cookbook** (`data/cookbook/`, 2026-10-06) is checked first: popular
   recipes kept for good, so they're instant and work without the internet.
   Only recipes rated 4.5+ by at least 50 people get in, up to 3 per dish.
   `server/scripts/build_cookbook.py` fills it from
   `server/config/cookbook_dishes.yaml` (~370 dishes across many cuisines,
   drinks, smoothies, desserts; "dish | alias" lines). Any search whose results
   clear the bar adds itself. `--list` shows what's in it. Don't run long
   SearXNG builds: ~450 searches in an hour got its engines suspended for the
   whole household (2026-10-06). For bulk work use `--urls found.json`
   (`{"dish": [page urls]}` found some other way), which fetches pages one at
   a time with `--site-gap` seconds between requests to one site. The
   SearXNG mode stops itself after 3 dishes in a row with no recipe pages.
2. **Introduce**: the source, servings, time, and a heads-up for long waits
   ("it needs to chill overnight"). Then the ingredients, grouped when the site
   groups them, and special equipment. Ends with "Do you have everything?"
3. **Missing ingredients**: substitutes from `recipe_subs.SUBSTITUTES`, one at a
   time. When they run out, an optional or flavoring ingredient can be left
   out. An essential one (named in the dish, the base like broth or pasta, or
   structural in baking) gets a shopping-list offer, then "a different recipe
   or keep going?".
4. **Steps**: one at a time, numbered as on the page. A long step (more than
   2 sentences or 30 words, never 20 words or fewer) is spoken in parts at
   sentence boundaries: "Step 2 of 6, in three parts. First: …", then "Then: …".
   next/back/repeat move by part; "read the whole step" reads all of it. The
   "say Alexa, next" prompt comes only at the end of the whole step. A timed
   part ("bake 45 minutes", 6 minutes or more) offers a timer labeled with the
   dish. Swapped or skipped ingredients are mentioned in the parts that use them.
   While a recipe is open, every recipe reply opens the mic's answer window
   (`recipe_mode`), so "next" right after a reply needs no wake word.
5. **Questions** (deterministic): "how much X" reads the ingredient line(s).
   "How long" and "what temperature" read the sentence from the current step,
   or the nearest step that has one. "Can I skip X?" / "do I need X?" uses the
   optional/essential rule without recording anything. "How do I know when
   it's done?" / "what's it supposed to look like?" reads the current step's
   doneness cue (until golden, jiggles, toothpick, springs back…) or the
   nearest step that has one.
6. **Free-form questions** (`docs/recipe_questions_and_scaling_plan.md` §1):
   "can I use a hand mixer?", "why a water bath?", "can I make this the day
   before?" go to the LLM grounded in `question_context()` — the recipe
   (scaled, swaps and left-outs noted), the equipment, the steps, the current
   step and a scale note — capped at `recipes.qa_max_tokens` (110). The hook
   only claims turns that name an ingredient or tool in the recipe, use
   cooking vocabulary or a question shape about the food, and never claims
   timers, weather, news or the like; a newer knowledge topic wins "tell me
   more". `grounding.recipe_claims_ok` then drops any sentence that states an
   amount, time, temperature, pan size or unit the recipe doesn't state —
   digits or spelled out ("five more minutes" fails) — and if every sentence
   fails, the answer is "The recipe doesn't say." General technique with no
   numbers is allowed through. A long Q&A stretch doesn't expire the session.
7. **Proposed swaps** ("can I use olive oil instead of butter?", "substitute X
   for Y", "replace Y with X", "would X work instead of Y"): answered from the
   table only. Which one is being replaced is decided by which the recipe uses.
   Table entries can depend on the recipe: cooking, baking, or "creamed" (the
   ingredient is beaten with the sugar, where oil can't stand in for butter).
   A yes is recorded as a swap; "keep the butter" undoes it. A swap the table
   doesn't list gets "I don't have a tested swap… what I know works here is…".
   With no recipe open, the same question gets the table's general answer,
   falling back to the web search only when the table has nothing. Synonyms
   (broth/stock, scallion/green onion, …) live in `recipe_subs._ALIASES`.
8. **Scaling** (docs/recipe_questions_and_scaling_plan.md §2): "<dish> for
   4 people" scales the recipe from its first sentence, and mid-recipe "make
   it for 4", "double it", "halve it", "back to the original" rescale it.
   Pure arithmetic on the published amounts: fractions snap to eighths and
   sixths, 3 tsp becomes 1 tbsp and 16 oz 1 lb, whole things round ("about 2
   bay leaves"), eggs that split get a yolk tip ("use 2 eggs plus 1 yolk"),
   and under 1/8 tsp is "a pinch". Times, temperatures and pan sizes are
   never scaled — she says they're from the original recipe and warns when
   the pan may need to be bigger or smaller. A step's own amount scales only
   when it matches an ingredient's amount exactly; anything else is flagged
   once as the original recipe's. A "for N" request carries into "another
   recipe"; a plain factor resets. "How many does it serve?" says the
   current size.

---

## State

`recipe_session.py` holds **one household-wide session**, saved to
`data/recipe_session.json` on every change. It's household-wide because the
mics start a new conversation id after every follow-up window, and a bake can
run for hours between turns. It expires 4 hours after the last recipe turn.

`_handle_recipe_session` runs before routing and before the garbage filter.
The session decides what it can claim:

- **Always** (while a session exists): "stop the recipe", "another recipe",
  "go to / what was step N", "read the ingredients", "how much X" (X in the
  recipe), substitution questions for its ingredients.
- **While on the steps**: bare "next", "back", "repeat", "how long",
  "what temperature". This includes while music plays; "next song" still
  skips.
- **Only as the answer to the question just asked** (up to 10 minutes old):
  bare "yes", "no", "I don't have X", a bare list of items.

A new "how do I make X" while on the steps asks "Switch to X?" first.

---

## Files

| File | What |
|---|---|
| `server/antigua_core/intents/recipe.py` | `parse_recipe_request()` (dish or None; "make" needs a food word), `requested_servings()`, `parse_cook_command()` with the scale commands |
| `server/antigua_core/recipe.py` | `RecipeFinder` (cookbook, search, fetch, parse, rank, cache), `Cookbook`, `step_parts()`, speakable text, `parse_amount()` / `scale_line()` / `scale_step_text()` |
| `server/scripts/build_cookbook.py` + `server/config/cookbook_dishes.yaml` | fills the offline cookbook |
| `server/antigua_core/recipe_session.py` | `CookSession` state machine (including scale state and carry-over); `handle()`, `start()`, persistence |
| `server/antigua_core/recipe_subs.py` | substitution table (with cook/bake/creamed variants), aliases, ingredient matching, `swap_verdict()`, optional/essential rule |
| `server/antigua_core/pipeline.py` | `_handle_recipe`, `_handle_recipe_session`, `_start_recipe`, `_recipe_question` (the grounded Q&A hook) |
| `server/antigua_core/grounding.py` | `recipe_claims_ok` — the recipe-mode number/unit check behind the LLM answer |
| `tests/test_recipe.py` | parsers, amount/snapshot cases, fixture pages, spoken dialogues (plain and scaled) through `dispatch_text`, `recipe_claims_ok` cases, Q&A routing |
| `tests/fixtures/recipes/` | trimmed real pages (JSON-LD and ingredient-group markup only) |

Config: the `recipes:` block in `server.yaml` (see `server.example.yaml`).
Only the primary server builds a `RecipeFinder`. On the backup, the route
answers that recipes need the main server.

---

## Known limits

- Some big sites (Allrecipes at times, Sally's Baking Addiction, Preppy
  Kitchen) return 403 to plain requests. The finder ranks whatever loads.
- The first request for a dish not in the cookbook takes 3–7 s. A "Let me find
  a good X recipe" line plays while it runs. Cookbook and cached dishes answer
  at once.
- `recipe_not_found` in the log marks dishes with no structured recipe
  online.
- "One at a time" reads the ingredients one by one ("got it" / "I don't have it"),
  then works through the missing ones.
- Not built yet: display on PinedaDisplay, Spanish. The free-form Q&A
  (Phase B) is coded on `development` but not yet deployed to noegpu01.
