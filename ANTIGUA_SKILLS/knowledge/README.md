# Skill: Knowledge (people, history, events)

**Status:** Active
**Pipeline stage:** Context injection — Wikipedia article + Wikidata facts into the LLM tail, grounded

---

## What It Does

Answers "who was / tell me about" questions with a short but substantive
overview (3–4 sentences) taken from the subject's Wikipedia article, then
keeps that article on hand so follow-ups — "Antigua, was she married?",
"where was she from?", "what did he win?", "how did it end?", "tell me more" —
are answered from the passages that cover them. The 4B model never answers
these from memory.

- **Overview**: the article's lead section, capped at `lead_chars`.
- **Follow-ups**: Wikidata facts (spouse, birthplace, dates, cause of death,
  awards, notable works, parties, participants, casualties…) plus the
  2–4 article passages that best match the question. "Married" also looks for
  wife/husband/spouse/divorce; "hometown" looks for born/childhood/raised; etc.
- **"Tell me more"**: passages not spoken yet, in article order.
- **Spanish**: a Spanish question ("quién fue…", "háblame de…") reads
  es.wikipedia, and pronoun-less follow-ups ("¿estaba casada?") count.

Both answers pass the `unsupported_claims` filter in `grounding.py`: a
sentence naming a person, place or number that isn't in the supplied text is
dropped before TTS.

---

## How Users Trigger It

**Overview:**
- "Who was Frida Kahlo?" / "Who is Bad Bunny?" / "Who were the Beatles?"
- "Tell me about Cesar Chavez" / "Tell me about the Cuban Missile Crisis"
- "What was Watergate?" / "What was the Battle of Hastings?"
- "What happened at Chernobyl?" / "The history of Mexico"
- "Quién fue Benito Juárez" / "Háblame de Selena"

**One question about a named subject** (looks the subject up, answers only that):
- "Who was Frida Kahlo's husband?"

**Follow-ups** (within `topic_ttl_seconds`, default 15 min, with the wake word):
- "Was she married?" / "What's her hometown?" / "What are her notable achievements?"
- "How did she die?" / "Did they have kids?" / "How did it end?"
- "Was Frida married?" (names the subject instead of a pronoun)
- "Tell me more"

**Does NOT fire on** (these stay on `search`):
- Role questions: "Who is the CEO of Starbucks", "Who was the first president of Mexico"
- Live facts: "Who is Taylor Swift dating", "Who is playing tonight", "latest…"
- Definitions and facts: "What is the capital of France", "What is a black hole" (LLM)
- The assistant or the house: "Tell me about yourself", "Who is <household member>"

---

## Code

| Piece | Where |
|---|---|
| Subject parsing, follow-up detection | `server/antigua_core/intents/knowledge.py` |
| Wikipedia/Wikidata fetch, article splitting, passage selection, prompts | `server/antigua_core/knowledge.py` (`WikiKnowledge`) |
| Route + handler, follow-up hook | `classify.py` (`knowledge`, before `search`), `pipeline.py` (`_handle_knowledge`, `_knowledge_followup`) |
| Tests | `tests/test_knowledge.py`, `tests/fixtures/routing.yaml` |

**Requests**: one Wikipedia search + one article fetch (~0.3–0.5s total),
cached per subject for an hour. Wikidata (two calls) loads on a background
thread while the overview is spoken; a follow-up waits up to
`facts_wait_seconds` for it.

**Picking the article** is general rules, never a list of names. Tested on
62 people and groups, with 62 right (one network timeout on the first try):

1. *Candidates* come from two Wikipedia searches run in parallel: by page
   views (so a bare surname is the famous one: "Mamdani" is Zohran, not his
   father) and by relevance, as a backstop.
2. *The name has to fit.* A Whisper mishearing still returns *some* article,
   so at least half the words asked must be in the title, and the rest must
   be near-misses ("Frida Callo" → Kahlo). Initials count ("AOC", "JFK",
   "RBG"). An alias the search matched counts only when it *is* what was
   asked: "The Rock (wrestler)" fits "the Rock", but "Prince Charles, Prince
   of Wales" doesn't fit "Prince". If nothing fits, one retry uses
   Wikipedia's did-you-mean.
3. *"Who" questions need the right kind of article*, judged from the page's
   categories. Any "who" question drops creative works (films, albums,
   songs…), so "who were the Black Panthers" is the party, not the film.
   "Who is/was" also requires a person (birth/death or "Living people"
   categories), so "who is the Rock" can't be rock music. "Who were the
   Aztecs" may be a people or a movement.
4. *Ranking*: an article titled plainly what was asked is Wikipedia's
   primary topic for that name and wins ("who was Selena" is Selena, though
   Selena Gomez is read more). Otherwise the most-read article in the last 30
   days wins: "Frida" is Kahlo, not ABBA's Anni-Frid "Frida" Lyngstad;
   "Kamala" is Harris, not the wrestler; "MJ" is Michael Jordan, not MJ
   Lenderman.

If nothing survives, or Wikipedia is unreachable, the turn falls back to
plain web search and any previous subject is cleared.

**Facts carry dates.** Wikidata's start and end qualifiers ride along
("Spouse: Doug Emhoff (from 2014)", "Academy Award for Best Actor (1994)"). With
only the passages, the model borrowed a nearby year: it said Harris married
in 2024, a year her article uses for her campaign.

**Follow-up state** is household-wide (`pipeline._last_knowledge`), not per
conversation. The mics drop the conversation id when their follow-up window
closes, so "Antigua, was she married?" a minute later arrives as a new
conversation. Follow-ups are only considered for turns routed `llm`, `search`
or `memory_query`. For `memory_query` ("how did she die" matches its "did
she" pattern), the article gets the question unless it names someone in the
house or asks about pills, meals or today. Headlines or a memory discussed
more recently keep "tell me more" for themselves.

---

## Config (`server.yaml`)

```yaml
knowledge:
  enabled: true
  topic_ttl_seconds: 900
  max_tokens: 220
  max_tokens_followup: 130
  overview_sentences: "3 to 4"
  timeout_seconds: 4
  # also: lead_chars, passage_chars, context_chars, facts_wait_seconds
```
