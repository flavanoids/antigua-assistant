# Recipe Skill Plan

**Date:** 2026-09-30
**Status:** Built 2026-09-30 on `feat/recipes`: Phase 1 plus most of Phase 2 (substitutions, the optional/essential rule, shopping list, timer offers, equipment, warnings about long waits, the one-at-a-time checklist), plus scaling from `docs/recipe_questions_and_scaling_plan.md` Phase A (2026-09-30, on `development`): "make it for 4" / "double it" rescale the published amounts in code; times, temperatures and pan sizes never scale. Phase B (free-form questions answered from the recipe, `recipe_claims_ok` number filter) is also built on `development` (2026-09-30, not deployed). All decisions settled with the recommended options. Not built yet: the rest of Phase 3 (display on PinedaDisplay, Spanish). Real-mic testing not done. Details in `ANTIGUA_SKILLS/recipes/README.md`.

> **Hard rule (owner, 2026-09-30):** every recipe comes from a real online source and is read as published. The LLM never writes a recipe, never fills in a missing quantity or step, and never "improves" one. If you don't like the sound of a recipe, ask for another one.
**Scope:** Hands-free, voice-only cooking. Antigua finds a recipe and reads the ingredients. She checks that you have them and suggests substitutes for what's missing. Then she walks through the steps one at a time, waiting for you after each one, and answers questions along the way ("how much salt?", "how long in the oven again?").

---

## Where things stand (checked 2026-09-30)

| Area | Today | What it means for recipes |
|---|---|---|
| "recipe for X" / "how to make X" | Matches the `search` route (`intents/search.py:36-37`). SearXNG snippets go to the LLM with 220 tokens. | You get one squashed paragraph, often cut off, with no state and no steps. |
| Substitutions | The `substitution` route (`intents/substitution.py`, `pipeline._handle_substitution`) searches SearXNG and grounds the LLM. | Can be reused as the fallback when the curated table doesn't know an ingredient. |
| Multi-turn state | `_pending_*` dicts are keyed by `conversation_id`. `_last_knowledge` is household-wide with a 15-minute TTL, because the mics reset the conversation id after every follow-up window. | A cooking session runs 30 minutes to several hours, so it must be **household-wide** with a long TTL and must survive a server restart. |
| Follow-ups | Kitchen bridge: 8 s follow-up window, `FOLLOW_UP_SPEECH_TRIGGER = False` (echo loop, see memory). | Every turn is "Antigua, next". That's fine for cooking. Speech-triggered follow-ups stay off. |
| "next" | `music_intents.py:103`: a bare `next` / `skip` skips the song. | This conflicts with "next" meaning the next step (Decision 2). |
| TTS text | `tts_text._expand_fractions` already speaks "1 1/2 cups" as "one and a half cups". | Units ("tbsp", "°F", "oz") need a few more expansions. |
| Timers / lists | Timer skill with labels. `list_add` for the shopping list. | "Bake 45 minutes" can offer a timer, and a missing ingredient can go on the shopping list. |

## The conversation, end to end

```
You:  Antigua, how do I make cheesecake?
A:    I found "Classic New York Cheesecake" from Simply Recipes. It serves 12
      and takes about an hour and a half, plus at least 4 hours chilling.
      You'll need 9 ingredients.
      For the crust: 1 and a half cups graham cracker crumbs, ...
      For the filling: 4 packages cream cheese, ...
      You'll also need a 9-inch springform pan.
      Do you have everything?
You:  Antigua, I don't have sour cream.
A:    You can use the same amount of plain Greek yogurt instead. Do you have that?
You:  Antigua, yes.
A:    Great, Greek yogurt it is. Anything else missing?
You:  Antigua, no, that's it.
A:    Ready for step one?
You:  Antigua, ready.
A:    Step 1 of 8. Heat the oven to 325 degrees and wrap the outside of the
      pan in foil. Let me know when you're ready for the next step.
You:  Antigua, next.
...
A:    Step 6 of 8. Bake 55 to 60 minutes, until the center barely jiggles.
      Want me to set a 55-minute timer?
You:  Antigua, yes.                          → timer "cheesecake" set
You:  Antigua, how much sugar was it?
A:    One cup of sugar.                       (the step pointer doesn't move)
You:  Antigua, what was step 3?
You:  Antigua, go back.  /  repeat that.  /  go to step 4.  /  how many steps left?
You:  Antigua, I'm done cooking.             → session ends
```

## Design

### 1. Where recipes come from: structured recipe data from the web (recommended)
Most recipe sites embed schema.org `Recipe` JSON-LD for Google. It has `recipeIngredient` (one string per line, with quantities), `recipeInstructions` (`HowToStep` / `HowToSection`), `recipeYield`, `prepTime`/`cookTime`/`totalTime`, and `aggregateRating`. The data is exact and deterministic, and the 4B model never invents a quantity.

- Search SearXNG for `"<dish> recipe"`, fetch the top ~5 result pages in parallel (4 s budget), and pull `<script type="application/ld+json">` blocks. Handle `@graph`, list-valued `@type`, and `HowToSection` nesting.
- Rank candidates by: parsed cleanly > rating × log(review count) > voice-friendly size (penalize more than 15 ingredients or more than 12 steps) > trusted-domain bonus. No per-dish special cases, same rule as knowledge.
- Cache the parsed recipe in `data/recipes/<slug>.json`, so "make that cheesecake again" works offline later.
- If no page has JSON-LD, say "I couldn't find a recipe online for that" and stop. **No LLM-invented recipes, ever** (hard rule). No search summary either, since a snippet summary is the LLM paraphrasing a recipe.
- Always name the source ("from Simply Recipes"), so you know it's a real recipe.
- **Another recipe:** keep the whole ranked candidate list on the session. "Another recipe" / "a different one" / "something simpler" / "one without nuts" / "I don't like that one" moves to the next candidate (with a filter for simpler = fewer ingredients/steps, and for without X = no ingredient line matching X), reads its summary and ingredients, and asks again. It works at FOUND, INGREDIENTS and SUBSTITUTE, and after "no substitute" too. When the list runs out, fetch the next page of search results, then say "That's all the recipes I found for X."
- **Where the LLM is allowed:** only to answer a free-form question *about* the recipe being cooked (§5), grounded in its text. It never produces ingredients, amounts, steps, times or temperatures that aren't in the source. For those it says "the recipe doesn't say".

### 2. Making it speakable
- **Ingredients:** keep the site's text for quantities and run it through `tts_text`. Add unit expansions: tbsp, tsp, oz, lb, g, ml, °F/°C, "(8 oz) package". Strip parentheticals like "(about 2 lemons)" unless the line is just a count.
- **Groups:** use `HowToSection` names or "For the crust:" headings when they exist. With more than 8 ingredients and no groups, say "You'll need N ingredients" first, then read them in two breaths.
- **Equipment:** regex over the steps for common tools (springform/loaf/sheet pan with sizes, stand mixer, food processor, Dutch oven, thermometer). Say "You'll also need…".
- **Heads-up times:** if `totalTime` is over 2 hours, or a step says chill/rest/rise/marinate/overnight, say so before the ingredients.
- **Steps:** a site step over ~45 words is split at sentence boundaries into two spoken steps. Spoken steps are numbered: "Step 3 of 9". Every step ends with a short prompt, alternated so it doesn't get repetitive: "Let me know when you're ready for the next step." / "Tell me when you're ready." Nothing is added after the last step.
- **Substituted ingredients in steps:** if a step names an ingredient that was swapped, add the note: "Add the sour cream — your Greek yogurt."

### 3. The cooking session (state machine)
One household-wide `RecipeSession` in `antigua_core/recipe.py`, saved to `data/recipe_session.json` after every change so a restart or deploy mid-bake doesn't lose your place.

```
FOUND ─► INGREDIENTS ─► (MISSING ⇄ SUBSTITUTE) ─► READY? ─► STEP(i) ─► DONE
            ▲ "read them again" / "one at a time"          ▲ next/back/repeat/goto
```

| Stage | Understands (deterministic regex, `intents/recipe.py`) |
|---|---|
| FOUND / INGREDIENTS | "another recipe" / "a different one" / "something simpler" / "one without X" → next candidate · yes / "I have everything" · no → "What are you missing?" · "I don't have X (and Y)" · "read them again" · "one at a time" (checklist mode: reads one, waits for "got it" / "don't have it") · "what was the fourth one" · "a different recipe" |
| SUBSTITUTE | yes (substitute accepted) · no → next alternative · "skip it" |
| READY? | yes / ready / "let's go" / "start" |
| STEP(i) | next / "next step" / "I'm ready" / "done" / "okay what's next" · back / "previous step" · repeat / "say that again" · "go to step N" / "what was step N" (read only, pointer stays) · "start over" · "how many steps left" · "read the ingredients again" |
| any | "stop the recipe" / "I'm done cooking" / "cancel the recipe" → end · "what step am I on" |

- **Precedence:** a `_handle_recipe_session(t)` pre-route handler runs next to the other `_pending_*` handlers, before the garbage filter, so a bare "yes" or "next" works. It only claims short replies when the session is **waiting** for one. A session that has been untouched for 20 minutes stops claiming bare "yes" but still claims "next step" and step questions. Whole session TTL: 4 hours since the last recipe turn.
- **What stays with the other skills:** timers, music ("next song", "pause"), volume, lights. "Stop" stays music/alarm. Ending a recipe needs "stop the recipe" or "done cooking".
- A new "how do I make X" while a session is active: "You're on step 4 of the cheesecake. Switch to X?"

### 4. Missing ingredients and substitutes
1. **Curated table** `server/antigua_core/data/substitutions.yaml`, about 80 common items: buttermilk, sour cream, eggs, butter, brown sugar, cake flour, baking powder, heavy cream, fresh/dried herbs (1 tbsp fresh = 1 tsp dried), garlic, onion, lemon juice, wine, stock, individual spices. Each entry is an ordered list of `{use, ratio, note}`. It's deterministic, quantities are correct, and it answers instantly.
2. **Not in the table:** reuse `_handle_substitution` (SearXNG + LLM), passing the dish name so the answer fits the recipe ("in a cheesecake").
3. **Offer one alternative at a time.** "Do you have that?" → no → the next alternative.
4. **Nothing left** (the "I'm not sure" case):
   - **Optional** (the line says "optional", "for garnish", "to taste", or it's a garnish/herb/spice, or the dish name doesn't mention it): "You can leave it out; it'll just be a bit less X." Move on.
   - **Essential** (flour in bread, cream cheese in cheesecake, chicken in chicken noodle soup, i.e. named in the dish, or a structural ingredient: flour, eggs, fat, leavening in baking): "That one's hard to do without. Want me to add it to the shopping list?" After that: "Want a different recipe instead, or keep going anyway?"
5. Every accepted swap is recorded on the session so steps mention it (§2) and "how much yogurt?" works.

### 5. Questions mid-recipe
- **Deterministic first:** "how much / how many X": fuzzy-match X against ingredient lines (and swaps) and answer with the line. "How long / what temperature": look in the current step, then the nearest step that has a time or °F. "What's next after this": read step i+1 without moving the pointer.
- **Everything else** ("can I use a hand mixer?", "why a water bath?", "is it done if it jiggles?"): the LLM, grounded in the whole recipe plus "the user is on step i", ~130 tokens. It answers from the recipe text. Any amount, time or temperature it states must appear in the source, checked against the recipe numbers after generation; if one doesn't match, the reply becomes "The recipe doesn't say." 
- The pointer never moves on a question. Afterwards: "…Say next when you're ready."

### 6. Timers and the shopping list
- A step with a single clear duration of 5 minutes or more ("bake 55 to 60 minutes" → use the lower bound) ends with "Want me to set a 55-minute timer?" A yes sets a timer labeled with the dish. Two minutes of "stir constantly" doesn't get a timer.
- A missing essential goes on the shopping list via the existing `list_add`.

## Phases

### Phase 1: follow a recipe (the core)
- `intents/recipe.py`: request parser ("how do I make X", "recipe for X", "let's cook X", "I want to make X"), the session phrases above, and a `recipe` route in `classify.py` placed before `search`. "How to make" moves from search to recipe only when X looks like food. Otherwise search keeps it: "how to make a bed" is still search. The test is a small food lexicon plus "does the JSON-LD search find a Recipe".
- `recipe.py`: fetch → JSON-LD parse → rank → speakable ingredients/steps → `RecipeSession`, persisted.
- Pipeline: `_handle_recipe` + `_handle_recipe_session` pre-route.
- Ingredients → "have everything?" (yes/no/missing X without substitutes yet: "noted") → ready → steps with next/back/repeat/goto/start over/how many left/stop. "How much X" deterministic.
- Scaling (2026-09-30): "<dish> for 4 people" and mid-recipe "make it for 4" / "double it" / "halve it" / "back to the original" rescale the amounts in code; "how many does it serve?" says the current size; a "for N" carries into "another recipe", a plain factor resets. Design: `docs/recipe_questions_and_scaling_plan.md`.
- Tests: saved JSON-LD **HTML fixtures** from ~10 real sites (text only, no audio) for parsing; a scripted text dialogue through `dispatch_text` for the state machine; regression checks that "next song", "pause", "set a timer" and "how to make a bed" still route where they used to.

### Phase 2: substitutes, questions, helpers
- Substitution table + fallback + optional/essential rule + shopping list.
- LLM questions grounded in the recipe.
- Timer offers, equipment callout, heads-up for long waits, swap notes in steps.
- Checklist ("one at a time") ingredient mode.

### Phase 3: polish (later)
- "Make that cheesecake again" / "what did I cook last week" from the cache.
- Show the current step on PinedaDisplay (behind `display.enabled`).
- Spanish recipes (after Spanish Phase 3).
- Fallback server (noedeb02): not supported at first. It answers "recipes need the main server."

## Risks
- **Sites blocking fetches (403/Cloudflare):** fetch several candidates in parallel with a normal browser UA and keep whichever parse. Recipes are cached, so each dish is fetched once.
- **Long ingredient lists read aloud:** mitigated by groups, the count first, "read them again", and checklist mode.
- **False claims of "yes"/"next" from TV or echo:** follow-ups are wake-word only, and bare replies are only claimed while the session is waiting (§3).
- **Dishes with no structured recipe online** (rare or regional dishes): with the hard rule, the answer is an honest "couldn't find one". Phase 1 logs these (`recipe_not_found`) so parsing for pages without JSON-LD (microdata, WordPress recipe-card HTML) can be added where it's worth it.
- **Latency:** the first request does search + fetch + parse, target under 4 s. Say "Let me find a good one…" through the existing filler path if it runs longer. Every later turn is local and instant.

## Decisions for the owner

1. ~~Recipe source~~ **Settled 2026-09-30:** real online sources only, never LLM-made. If you don't like one, "another recipe".
2. **Settled 2026-09-30 (recommended option): "next" during a recipe while music plays.** While a recipe is on its steps, a bare "next" means the next step, and you'd say "next song" to skip. Alternative: bare "next" stays with music whenever something is playing.
3. **Settled 2026-09-30 (recommended option): reading ingredients.** Read the whole list (grouped) and then ask "Do you have everything?", with "one at a time" available on request. Alternative: always go through them one at a time.
4. **Settled 2026-09-30 (recommended option): essential ingredient with no substitute.** Offer to add it to the shopping list, then offer a different recipe or keep going anyway.
