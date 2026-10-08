# Recipe Skill: Free-Form Questions and Scaling Servings

**Date:** 2026-09-30 17:36 CDT
**Status:** Decided 2026-09-30 — all four open decisions taken as recommended
(see the end). **Phase A (scaling) built the same day on `development`:**
`parse_amount`/`scale_line`/`scale_step_text`, the `scale` command, the
"for N people" suffix, servings carry-over, and parser/snapshot/dialogue/
persistence tests in `tests/test_recipe.py`. **Phase B (questions) also
built on `development`, 2026-09-30, not deployed:** `question_context()`,
the `_recipe_question` pipeline hook (route label `recipe_question`,
`RECIPE_QA_MAX_TOKENS` 110), the deterministic `skip_q` / `looks_q` answers
(§1.3), and `grounding.recipe_claims_ok` wired into both servers' sentence
filters (§1.4), with claim/routing/filter tests in `tests/test_recipe.py`.
Remaining: live `recipes.qa_max_tokens` in `server.yaml` on noegpu01, a
`/pipeline_text` check, then the real mic.
**Builds on:** the recipe skill deployed 2026-09-30 (merge `f324d73`, feature
commit `499c5af`). Its original plan is [`recipe_skill_plan.md`](recipe_skill_plan.md);
how it works now is in [`ANTIGUA_SKILLS/recipes/README.md`](../ANTIGUA_SKILLS/recipes/README.md).
**Scope:** the two gaps left after that release:

1. **Open-ended questions mid-recipe**: "Can I use a hand mixer?", "Why a
   water bath?", "Is it done if it jiggles?", "Can I make this the day before?".
   These are answered from the recipe being cooked, not from the 4B model's
   memory.
2. **Scaling servings**: "Make it for 4", "Double it", "Half the recipe". The
   ingredient amounts are rescaled and spoken naturally.

---

## The hard rule, and what it means here

The owner set this rule on 2026-09-30: **recipes come from real online sources,
never from the LLM.** Both features must keep to it:

- **Questions:** the LLM may explain or reassure ("A hand mixer works; it
  just takes longer"). It may never supply an amount, time, temperature or
  ingredient that isn't in the recipe. If the recipe doesn't cover something,
  she says so. This is enforced in code, not only by the prompt (§1.4).
- **Scaling:** pure arithmetic on the published amounts. No LLM involved.
  Times, temperatures and pan sizes are **not** scaled, since that would be
  inventing cooking guidance. She says so instead (§2.5).

---

## Where things stand (checked 2026-09-30)

| Area | Today | Relevance |
|---|---|---|
| Session state | `recipe_session.CookSession` (`server/antigua_core/recipe_session.py`). One household-wide session, saved to `data/recipe_session.json`, 4-hour TTL. `handle()` returns a `Reply` or `None`. | Both features hang off the session. |
| Unclaimed turns | When `CookSession.handle()` returns `None`, the turn goes on to normal routing (`dispatch_text` in `pipeline.py`). An open question like "can I use a hand mixer?" usually classifies as `llm`, and the LLM answers **with no recipe context**. | This is gap 1: the model answers from memory and could invent. |
| Deterministic Q&A | `how_much()` (ingredient lines), `how_long()` / `temperature()` via `_search_steps()` (sentence search, current step first). | Keep these first. The LLM only gets what they can't answer. |
| Pattern to copy | `_knowledge_followup()` (`pipeline.py` ~line 1611, called at ~2076). If the turn looks like a follow-up, it sets `t.extra_context = t.grounding = <context>`, sets `t.max_tokens`, and lets `_llm_tail()` stream a grounded answer. | The recipe Q&A hook is the same shape. |
| Grounding guard | `grounding.unsupported_claims(sentence, context)`, applied in `antigua_server.py` (~612 non-streaming, ~749/771 streaming) whenever `grounding_context` is set. It drops sentences with capitalized names, or numbers of **2+ digits**, that aren't in the context. | Too weak for recipes: "bake 5 more minutes" (one digit) and "five minutes" (spelled out) both get through. It needs a recipe-specific check (§1.4). |
| LLM | Qwen3.5-4B via Ollama, `max_tokens: 65` default, `num_ctx` 8192 pinned. Knowledge follow-ups use 130 tokens at temperature 0.1 when grounded. | A recipe is ~500–1,500 tokens of context, which fits easily. |
| Amounts | `Recipe.ingredients` stores **speakable strings** ("1 1/4 cups graham cracker crumbs", "4 (8 ounce) packages cream cheese, softened"). No parsed quantity is kept. `Recipe.servings` is a phrase ("serves 12", "serves 8 to 10", "makes 1 loaf"). | Scaling needs the quantity parsed out of each line, plus a numeric base yield. |
| Places amounts are spoken | `ingredient_list()` (intro and "read the ingredients"), `_check_item()` (checklist), `how_much()`, `_notes()`, substitution texts in `recipe_subs.SUBSTITUTES` ("1 cup of milk … for each cup"), and amounts written into steps ("add 1 cup of the sugar"). | Every one of these has to honor the scale. |
| Speech | `tts_text._expand_fractions` speaks "1 1/2" as "one and a half". `recipe._decimals` turns 0.333 into 1/3. | The scaled output should be written as fractions, so this keeps working. |
| Echo / follow-up | Wake word on every turn (speech follow-ups are off because of the self-reply loop). | Unchanged. Every question is "Antigua, …". |

---

## 1. Open-ended questions answered from the recipe

### 1.1 When a turn counts as a recipe question

It must be a turn the session didn't claim (`CookSession.handle()` → `None`),
with a session that exists and is in `ingredients`, `steps` or `finished`. It
must also have been routed to `llm`, `search` or `memory_query`, the same routes
`_knowledge_followup` accepts, so timers, music, lights, weather and lists are
never taken. On top of that, it must look like it's about the cooking. At least
one of these:

- It names an ingredient in the recipe (`recipe_subs.match_ingredient`) or a
  piece of equipment (`recipe._EQUIPMENT_RE` plus common tools: mixer, whisk,
  pan, pot, oven, stove, microwave, air fryer, blender, foil, parchment).
- It uses cooking vocabulary: done, ready, burn, overcook, undercook, jiggle,
  golden, thick, thin, runny, lumpy, curdle, crack, rise, sink, set, bake,
  simmer, boil, whisk, fold, beat, mix, knead, chill, freeze, reheat, store,
  leftover, ahead, substitute, instead, skip, vegan, gluten, dairy.
- It uses a pronoun about the food with a question shape: "is it supposed to
  look like this", "can I leave it", "should it be".

Precedence mirrors `_knowledge_followup`. Headlines or a memory discussed
**since** the last recipe turn win "tell me more" and "what did she say".
The recipe hook runs before `_knowledge_followup`, because a recipe in
progress is the stronger context in the kitchen. If a knowledge topic is
newer than the last recipe turn, knowledge wins.

**Tests:** "can I use a hand mixer", "why do I need a water bath", "is it done
if the middle jiggles", "can I make this the day before", "what if I don't have
a springform pan", "can I freeze it" all go to recipe Q&A. "Who was Frida
Kahlo", "what's the weather", "tell me a joke", "turn on the lights", "how
many people live in Texas" do not.

### 1.2 The context given to the LLM

`CookSession.question_context(question) -> str`, built only from session data:

```
Recipe being cooked: <title> from <source> (<servings, scaled if scaled>)
Ingredients:
- <each line, scaled and with swaps noted: "1/2 cup sour cream (using plain yogurt instead)">
Equipment: <list>
Steps:
1. ...
N. ...
The cook is on step <i> of <N>. [They left out: celery.]

Answer the cook's question in one to three short spoken sentences, using ONLY
the recipe above plus general kitchen technique. Never state an amount, time,
temperature, pan size or ingredient that is not written in the recipe. If the
recipe doesn't say, say "The recipe doesn't say" and, if useful, one general
tip without numbers. No lists, no headings.
```

Settings: `t.extra_context = t.grounding = context`,
`t.max_tokens = RECIPE_QA_MAX_TOKENS` (default 110), temperature 0.1 (already
automatic when `t.grounding` is set). New route label for logs and
`_llm_tail`: `recipe_question`. The session's `at` is touched so a long
question-and-answer stretch doesn't expire the recipe.

### 1.3 Deterministic answers still come first

Before the LLM, extend what `handle()` can answer outright:

- "Can I use X instead of Y" / "what if I don't have X" → already
  `sub_q` / missing. Keep them.
- "Can I skip X" / "do I need X" → the optional/essential rule
  (`recipe_subs.is_essential`): "You can leave it out; it'll just taste a
  little different" or "That one's hard to do without."
- "What's it supposed to look like" / "how do I know it's done" → the
  current step's sentence with doneness cues (until, when, should, golden,
  set, jiggle, bubbles, tender, clean), found the same way `_search_steps`
  finds times. Only if nothing matches does it go to the LLM.

### 1.4 Enforcing "no invented numbers"

`unsupported_claims` misses single digits and spelled-out numbers. Add
`grounding.recipe_claims_ok(sentence, context) -> bool`, used when the route is
`recipe_question`:

- Normalize the sentence: number words to digits ("five" → 5, "a half" →
  1/2, "an hour" → 1 hour), and "°" / "degrees" to one form.
- Every number in the sentence, digits or words, including single digits,
  must appear in the context. A number is allowed through when it isn't a
  measurement (an ordinal like "step 3", when step 3 exists).
- Every unit phrase (<number> <minute|hour|degree|cup|tablespoon|teaspoon|
  ounce|pound|gram|inch>) must appear in the context with the same number.
- A sentence that fails is dropped. If every sentence fails, the answer
  becomes "The recipe doesn't say." (not the generic "I couldn't find a clear
  answer").

Wire-up: `_llm_tail` passes a `grounding_mode="recipe"` flag (or the route)
through `ask_llm_stream`, and both servers' sentence filters call
`recipe_claims_ok` in addition to `unsupported_claims`. The fallback server
needs no changes: it has no `RecipeFinder`, so there's never a session there.

### 1.5 Examples (the kind of answer to aim for)

| Question (step 5 of the test cheesecake) | Answer |
|---|---|
| "Can I use a hand mixer?" | "Yes, a hand mixer works; beat the cream cheese until it's completely smooth, it'll just take a little longer." |
| "Why the water bath?" | "The recipe doesn't say. A water bath generally keeps a cheesecake from cracking by baking it gently." |
| "Is it done if the middle jiggles?" | "The recipe says to bake it for 30 minutes at 325 degrees, then leave it in the oven with the heat off for 30 minutes more." (numbers only from the recipe) |
| "Can I bake it hotter to speed it up?" | "The recipe says 325 degrees for the filling, so I'd stick with that." |
| "Can I make this the day before?" | "Yes, the recipe says to refrigerate it at least 6 hours or overnight." |

### 1.6 Risks

- **Over-claiming turns:** the cooking-vocabulary test could take a general
  question asked in the kitchen ("is the store open"). Keep the word list tight,
  and log every claimed turn as `recipe_question: <text>` for review, the same
  way `weather_intent_miss` is logged.
- **The 4B model echoing numbers from the wrong step:** it's grounded, so the
  numbers are real, but they could come from the crust step when the question
  is about the filling. The prompt names the current step. Acceptable for a
  first version; review the logs.
- **Latency:** ~1–2 s for a grounded 110-token answer. Same as knowledge
  follow-ups.

---

## 2. Scaling servings

### 2.1 What the user can say

| Utterance | Meaning |
|---|---|
| "Make it for 4" / "I'm cooking for 6 people" / "scale it to 8 servings" | target servings |
| "Double it" / "double the recipe" / "twice as much" | ×2 |
| "Triple it" | ×3 |
| "Half the recipe" / "halve it" / "make half" | ×½ |
| "Make one and a half times" | ×1.5 |
| "Back to the original" / "normal amount" / "undo the scaling" | ×1 |
| "How many does it serve?" | current servings (scaled if scaled) |

These are always claimed while a session exists (the parser lives in
`intents/recipe.py`, as `("scale", factor or servings)`). They work at any
stage. Scaling mid-steps is allowed: she re-reads nothing and just confirms.
Asking for the servings up front ("how do I make cheesecake for 4 people")
sets the scale when the recipe starts.

### 2.2 Parsing amounts: a structured quantity for each ingredient line

Add to `Recipe` (both `to_dict`/`from_dict`, with defaults so the existing
14-day cache still loads):

- `yield_n: float` is the base servings number. Parse it from `recipeYield`:
  the first number, or the low end of a range ("8 to 10" → 8, and she says "8
  to 10" scaled as "16 to 20"). "Makes 1 loaf" / "1 9-inch cheesecake" gives
  `yield_n = 1` with `yield_unit = "loaf"`. Serving-count scaling is then
  unavailable; only ×factor works, and she says so ("This recipe makes one loaf.
  I can double or halve it.").
- `amounts: list` holds one entry per ingredient line:
  `{"qty": float|None, "qty_hi": float|None, "unit": str|None, "rest": str}`,
  parsed by `recipe.parse_amount(line)`:
  - Leading mixed numbers, fractions and decimals: "1 1/4", "1/2", "0.75",
    "1 and 1/2".
  - Ranges: "1 to 2", "2-3" (`qty_hi`).
  - Package sizes: "4 (8 ounce) packages": scale the 4, never the "(8 ounce)".
  - Units from `recipe._UNITS` (cup, tablespoon, teaspoon, ounce, pound, gram,
    kilogram, milliliter, liter, quart, pint, package, can, clove, stick, pinch,
    dash, slice, large/medium/small as count adjectives).
  - Unparseable lines ("salt and pepper, to taste", "berry sauce, for
    serving, optional", "a handful of…") get `qty = None` and are never scaled.
    They're spoken as-is.

  Compute this at parse time in `parse_recipe_html`. For recipes already in
  the cache, fill it in lazily on load (the ingredient strings are enough to
  parse).

### 2.3 Arithmetic and natural amounts

`recipe.scale_line(line, amount, factor) -> str`:

- Multiply `qty` (and `qty_hi`).
- **Fractions:** snap to the nearest of 1/8, 1/4, 1/3, 3/8, 1/2, 5/8, 2/3, 3/4,
  7/8 (reuse `_FRACTIONS` / `_decimal_fraction`). Write it as "1 1/2", not
  "1.5", so `tts_text` speaks it.
- **Unit tidy-up (US volume):** 3 teaspoons → 1 tablespoon; 16 tablespoons →
  1 cup; 4 tablespoons → 1/4 cup when that's closer to how people measure.
  Going down: 1/8 cup → 2 tablespoons; 1/16 cup → 1 tablespoon. Weights: 16
  ounces → 1 pound, only when the original unit was ounces and the result is
  a whole number of pounds.
- **Counts** (eggs, cloves, cans, packages, sticks): round to the nearest
  whole number. When rounding changes the amount by 25% or more, say it: "3 eggs
  (the exact amount is 2 and a half; use 2 eggs plus 1 yolk)". Eggs get that
  yolk tip; for other counts just say "about".
- **Tiny amounts:** under 1/8 teaspoon becomes "a pinch".
- **Singular or plural** follows the new amount (`recipe._plural`).

### 2.4 Where the scale applies

Store `self.scale: float = 1.0` and the requested servings (when the scale
came from "for N", not a factor) on `CookSession`, both saved with the
session. On "another recipe" a servings request reapplies to the new
recipe's own yield; a pure factor ("double it") resets to 1.0 (Decision 3).
Everything that speaks an amount
goes through one helper, `self.line(i) -> str`, which returns the scaled,
speakable ingredient line:

- `ingredient_list()`, `_check_item()`, `how_much()`: use `self.line(i)`.
- **Steps:** amounts inside step text ("add 1 cup of the sugar", "pour in 2
  cups of the broth") are scaled only when they match an ingredient's amount
  and unit exactly (a whole-number or fraction match against `amounts`).
  Anything else in a step ("2 inches of water", "a 9-inch pan", times,
  temperatures) is left alone. When a step has an unscaled amount and the
  scale isn't 1, add "(that's for the original recipe)" after it.
- **Substitution texts** that say "the same amount" stay correct as they are.
  Texts with fixed amounts ("1 cup of milk with 1 tablespoon of lemon juice…
  for each cup") are per-unit, so they also stay correct. No change needed.
  Add a test to keep it that way.
- **Intro servings phrase:** "It serves 12" becomes "It serves 4 (scaled from
  12)".
- **Q&A context (§1.2):** give the scaled ingredient lines, plus a note:
  "Amounts are scaled ×1/3 from the original; times and temperatures are the
  original."

### 2.5 What is deliberately not scaled

- **Times and temperatures:** a doubled cheesecake in a bigger pan bakes
  differently, and the recipe doesn't say how. The confirmation says: "I've
  doubled the ingredients. Cooking times and pan size are from the original
  recipe, so keep an eye on it."
- **Pan and equipment sizes:** never changed. When the factor is ≥ 1.5 or ≤
  0.67 and the recipe names a pan, add: "You may need a bigger pan" / "a smaller
  pan".
- **Timer offers:** unchanged (they come from the step's text).

### 2.6 Confirmation replies

- "Make it for 4" on a recipe that serves 12: "Okay, scaled for 4, a third of
  the recipe. Cooking times and pan size stay the same, so keep an eye on it.
  Want me to read the new amounts?" A yes reads `ingredient_list()`. If on the
  steps, a no carries on.
- Rounding warnings go in the confirmation once ("Eggs don't divide
  evenly: use 1 egg plus 1 yolk"), not again on every read.
- Nothing to scale (every line is unparseable) or no numeric yield with a
  servings request: "This recipe doesn't list amounts I can scale" / "This
  recipe makes one loaf. I can double or halve it."
- Limits: factors outside 1/8–8 get "That's a big change; I'd find a recipe
  sized for that instead." The session is unchanged.

---

## Phases

### Phase A: scaling (deterministic, no LLM; do this first)
- `recipe.parse_amount`, `scale_line`, the unit tidy-up, `Recipe.yield_n` and
  `amounts` (with cache backfill).
- `CookSession.scale`, `line(i)`, the scale command in `parse_cook_command`,
  and the "for N people" suffix on `parse_recipe_request`.
- Tests: the parser on ~40 real-world ingredient shapes (mixed numbers,
  ranges, packages, to-taste, decimals, "1 and 1/2"). Scaling snapshots
  (×2, ×½, ×⅓, for 4 of 12, 8-to-10 ranges, eggs rounding, tsp→tbsp→cup).
  A dialogue through `dispatch_text` ("make it for 4" → how much sugar →
  scaled; a step amount scaled; the substitution text unchanged). Persistence
  across restart.

### Phase B: questions answered from the recipe
- `CookSession.question_context()`, the pipeline hook `_recipe_question()`
  next to `_knowledge_followup()`, route label `recipe_question`,
  `RECIPE_QA_MAX_TOKENS`.
- The extra deterministic answers in §1.3.
- `grounding.recipe_claims_ok` and the server-side wire-up.
- Tests: claim/no-claim routing cases (§1.1). The context contains the swaps,
  the scale note and the current step. A fake LLM returning an invented "bake 5
  more minutes" and "five more minutes" must be filtered to "The recipe doesn't
  say." A grounded "325 degrees" passes.
- Then a live check through `/pipeline_text` (quiet), then the real mic.

### Docs to update with each phase
`ANTIGUA_SKILLS/recipes/README.md`, `SKILLS.md` (Recipes section),
`CHANGELOG.md`, `server.example.yaml` + live `server.yaml`
(`recipes.qa_max_tokens`), `docs/recipe_skill_plan.md` status line.

---

## Decisions (decided 2026-09-30, all as recommended)

1. **General kitchen knowledge in answers: ALLOWED, behind the code filter.**
   She may add general technique ("a hand mixer works, it just takes
   longer") as long as no amount, time or temperature comes from outside
   the recipe. `grounding.recipe_claims_ok` (§1.4) enforces this; it is not
   just prompt discipline.
2. **Uneven counts when scaling: ROUND, SAY THE EXACT AMOUNT ONCE.**
   Round to whole counts, with the egg-yolk tip for eggs ("use 2 eggs plus
   1 yolk") and "about" for other counts. The exact amount is spoken once in
   the confirmation, not on every read.
3. **Scale on "another recipe": SERVINGS PERSIST, FACTOR RESETS.**
   "For 4" stays "for 4" on the next recipe; "double it" resets to the new
   recipe's own size. `CookSession` remembers the requested servings, not
   just the factor.
4. **Amounts inside steps: SCALE EXACT MATCHES ONLY.**
   A step amount is scaled only when it exactly matches an ingredient's
   amount and unit; anything else in a step gets "(that's for the original
   recipe)" when the scale isn't 1.
