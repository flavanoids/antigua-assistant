# Recipe skill: proposed swaps and long steps — plan

2026-10-06. Follows the hands-free fix (7437787). Two friction points from the
dry run on the live server:

- **2. "Can I use olive oil instead of butter?"** mid-recipe → "I couldn't find a clear answer for that."
- **3. Long steps** are read out as one block, with no way to hear just part of one.

The hard rule still applies: every recipe fact comes from the recipe page or
the curated substitution table, never from the model.

## 2. Proposed swaps

### What happens today

`logs/server.log` 12:04:56: the cook session doesn't claim the turn, because
`_SUB_Q_RE` (intents/recipe.py:291) only knows "what can I use instead of Y",
"a substitute for Y", "can I substitute/replace/swap Y" and "instead of Y".
"Can I use X instead of Y" therefore drops into the general substitution
handler, which runs a web search and then the model. The model said "1:1 ratio",
`grounding` dropped that claim, and all that was left to say was "I couldn't find a clear answer."

Even if the session had claimed it, two things would still go wrong:

1. **The proposal gets lost.** The table answers "what can I use for Y", not
   "is X okay for Y". "Can I replace butter with olive oil" parses to
   item=`butter` and drops the olive oil. "Can I substitute olive oil for butter"
   parses to item=`olive oil for butter`.
2. **The table has no olive oil option for butter.** `butter → oil` says "three quarters as
   much vegetable oil, in cooking; in baking use melted coconut oil".

### Proposal

**a. Parse it as a pair: (proposed X, replaces Y).** Phrasings:
"use X instead of / in place of / for Y", "substitute X for Y", "replace Y with X",
"swap Y for X" / "swap X for Y", "X instead of Y?", "would X work instead of Y".
People mix up the word order of "swap" and "substitute". So the direction comes from the
recipe, not the grammar: whichever of the two the recipe actually uses is the one being replaced.

**b. While a recipe is open, the session always answers, never the web search or the model:**

| Case | Reply |
|---|---|
| Y not in the recipe (nor X) | "This recipe doesn't use butter." |
| X is in Y's table entry | "Yes: three quarters as much olive oil. It'll taste a little of olive." + the baking caveat when it's baked |
| Table has Y, but not X | "I don't have a tested swap of olive oil for butter. What I know works here is melted coconut oil, the same amount." |
| No table entry for Y | "I don't have a tested substitute for the butter." + "You can leave it out" if it's optional |

**c. Oil families + synonyms.** `olive oil`, `canola`, `vegetable`, `avocado`, `neutral
oil` match a table entry written for "oil". The same alias step fixes the
**broth/stock gap** from 09-30: "how much broth" → "1 quart chicken stock".
It covers broth↔stock, scallion↔green onion, cilantro↔coriander leaves,
powdered↔confectioners'/icing sugar, and heavy cream↔heavy whipping cream.

**d. The table gets baking vs cooking notes** for fats (butter, oil, shortening,
margarine, coconut oil). I'll draft the entries from a published
substitution chart (King Arthur Baking's or similar) and cite the source in a
comment next to each one. You approve the wording before it ships.

### Decisions for you

1. **After a yes answer, record the swap?** (a) Ask "Want me to note that for the
   steps?" (b) record it automatically, so later steps say "You're using olive oil instead
   of the butter" (c) just answer. *Recommend (b).* That's what accepting a substitute during
   the ingredient check already does, and "no, keep the butter" would undo it.
2. **Butter → olive oil in baking:** many charts say ¾ as much in quick breads and
   muffins, but not in recipes that cream butter and sugar, like cookies and layer cakes.
   (a) Add that rule, keyed off "cream"/"beat … butter and sugar" in the steps.
   (b) Keep it simple: always ¾ plus "it'll change the texture" when baking.
   *Recommend (a).* Banana bread *does* beat the butter and sugar, so (b) would give the wrong answer here.
3. **With no recipe open**, should "can I use X instead of Y" also use the table
   first (falling back to today's web + model path only when the table has nothing)? *Recommend yes.* It's
   the same parser and doesn't need much more code.

## 3. Long steps

### Data

189 steps across the 5 cached dishes: median 23 words, 90th percentile 41, max 45;
up to 5 sentences. The sample is small and Allrecipes-heavy. Blog recipes often run
past 100 words a step. Banana bread step 2 is four separate
actions in one step: dry mix, cream, add the wet, combine.

### Proposal: speak long steps in parts

A step longer than **2 sentences or ~30 words** (never one of 20 words or fewer) is split at sentence
boundaries into parts of 1–2 sentences, kept together by meaning: short clauses
like "Mix well." merge with their neighbour.

- **Step numbers stay the recipe's.** "Step 2 of 6, in three parts. First: combine
  flour, baking soda and salt in a large bowl." Then "Next: …", "Last: …".
- **next** → the next part, then the next step. **repeat** → the current part.
  **back** → the previous part. "**Read the whole step**" → all of it.
  "Go to step 4" → the start of step 4.
- The **timer offer** goes with the part that names the time.
  **Swap notes** ("you're using flax egg…") go with the part that mentions the
  ingredient.
- "How many steps are left" still counts steps, not parts.

Each part is a short turn and stays in the hands-free answer window.
Splitting only at sentence boundaries keeps every word the recipe's own.

### Decisions for you

4. **Threshold:** (a) more than 2 sentences *or* more than 30 words (splits 72 of 189 steps,
   about 2 in 5, on the current data) (b) only more than 40 words (about 1 in 8) (c) always one sentence at a time.
   *Recommend (a).*
5. **Announce the parts?** (a) "Step 2 of 6, in three parts. First: …" (b) no
   count, just "Step 2 of 6. Combine…", then "Then: …" *Recommend (a).* Knowing
   there's more stops you saying "next" too early.
6. **Spoken prompt between parts:** (a) none. The green light and a short
   "Then:" on the next part are enough (b) the full "say Alexa, next" prompt every time. *Recommend
   (a).* The full prompt only at the end of the whole step.

## Order of work

1. Swap parser + session answers (2a, 2b) + tests from the dry-run phrasings.
2. Alias step (2c), with broth/stock as its test.
3. Table additions (2d), drafted and cited, for your review.
4. Step parts (3), with tests for next/back/repeat/goto/timer/swap notes across parts.
5. Quiet dry run on the live server (banana bread + chicken soup + one long blog recipe), then deploy.
