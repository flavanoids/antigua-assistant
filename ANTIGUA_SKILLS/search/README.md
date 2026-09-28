# Search Skill

**Status:** Active  
**Pipeline stage:** Context injection  
**Machine:** primary (SearXNG runs locally; skill code in `antigua_core/caches.py` and `pipeline.py`)

## What it does

Queries a local SearXNG instance and injects the top N result snippets as `extra_context` into `ask_llm_stream()`. The LLM synthesizes a 1-2 sentence spoken answer from the snippets.

## Trigger examples

- "Search for the best pasta recipe"
- "Look up the price of a PS5"
- "Who is the CEO of Nvidia?"
- "What are the hours for Home Depot?"
- "How to reset a Nest thermostat"
- "Latest iPhone features"
- "Is Whole Foods open right now?"

## Detection

`is_search_request()` runs five regex layers in order:

| Layer | Regex / helper | Catches |
|---|---|---|
| Explicit | `_SEARCH_EXPLICIT_RE` | "search for", "look it up", "google it", "find info about" |
| Factual | `_SEARCH_FACTUAL_RE` | "who is/was", "how tall is", "business hours for", "recipe for", "how to fix/install" |
| Temporal | `_SEARCH_TEMPORAL_RE` | "latest X", "current X", "newest X" (negative lookahead skips weather/news/time) |
| Live event | `is_live_event_query()` | "when is the next Dynamo game", "who won last night", "what time does the Rockets game start", "next SpaceX launch" |
| Local news | `is_local_news_query()` | "news in NYC", "what's happening in Chicago" — news scoped to a place or subject the RSS feeds can't serve |

Guards: skips if weather, time/date, timer, or memory context already claimed the
slot, or if `search.enabled: false`. **Generic** news ("what's the news", "tech
headlines") still goes to the RSS path; only place/subject-scoped news is handed
to the web, because the feeds are BBC/Reuters world and national wires.

## The LLM router (the long tail)

The regex layers are a whitelist and always will be — "what's the rotten tomatoes
rating for the new Dune movie" and "how do I install a Moen 1225 cartridge" are
exactly the shapes they can't enumerate. When **every** skill and every regex layer
has declined, `route_search_with_llm()` asks qwen3.5:4b whether the question needs
the web, and for the query to run.

- Costs ~0.5–1.3s, and only on questions nothing faster could answer.
- Measured 16/17 correct on a mixed set (10 search-worthy, 7 chit-chat).
- Falls back to answering from weights on timeout (`router_timeout_seconds: 3`)
  or any error — a router failure is never user-visible.
- Skips transcripts under 3 words ("hello", "thanks").

Two hard-won details:

1. **`"think": false` is required.** Without it the model spends its entire
   `num_predict` budget on reasoning tokens and returns an empty `response`,
   so every question routes to NONE. This is on `/api/generate`; the chat path
   already sets it.
2. **Quotes are stripped from the returned query.** The model reaches for phrase
   search on its own, and a wrong phrase (`"Dune 2024"`) returns nothing at all
   instead of something close. Keywords degrade gracefully; phrases don't.

## Grounding

Three layers, because the first two are not enough on their own.

1. **Temperature 0.1** for snippet answers instead of the usual 0.4.
2. **The prompt** forbids stating any name, date, year, time, venue, score or
   number absent from the snippets, and says to admit it couldn't find the answer
   rather than fall back on memory. This applies to *every* search answer. It was
   originally only on live queries, and that gap is exactly how "who won the World
   Cup" returned an invented 2025 date, opponent, scorer and stadium — the router
   path had taken the weaker instruction.
3. **`unsupported_claims()`** — the deterministic backstop. Every sentence's proper
   nouns and numbers are checked against the context that was actually supplied;
   unsupported ones never reach TTS, and if nothing survives, Antigua says "I
   couldn't find a clear answer for that."

Layer 3 exists because layer 2 demonstrably fails. Asked who won the World Cup,
the model answered *"Spain beat Argentina ... 62 million viewers across Fox and
Telemundo"* with none of it in the snippets, then admitted in the very next
sentence that it had no results for this year.

The check tolerates morphology — a token counts as supported if it shares a
3-letter prefix with any context word, so "Spanish" passes against a context
saying "Spain" — because dropping a correct sentence is worse than letting a
near-miss through. In practice it fires on inventions and not on real answers:
across the Moen, Dune, drain-pump, Dynamo and World Cup checks it dropped nothing.

`how do I…` / install / replace questions get `max_tokens_search_long` (220)
instead of 120, since instructions can't be given in 120 tokens without cutting
off mid-step.

## Live queries take a different path

`build_search_query()` returns `(query, engines)`. When the question is a fixture,
result, or local-news question it is treated as **live**, which changes four things:

| | normal | live |
|---|---|---|
| engines | `search.engines` | `search.news_engines` |
| SearXNG category | `general` | `news` |
| snippets kept | `result_count` (3) | `live_result_count` (6) |
| Wikipedia leg | yes | skipped — an encyclopedia entry never answers "when is the next game" |

`rewrite_event_query()` converts the spoken question into a keyword query, because
spoken questions rank terribly as web queries — "when is the next Dynamo game"
returned the **Next** clothing retailer, and "who won the Astros game last night"
returned dictionary definitions of "won". It strips question framing to the
subject, re-adds the intent as keywords, and prefixes `search.home_city` for
one-word team names:

| spoken | query sent |
|---|---|
| when is the next Red Bulls game | `New York Red Bulls schedule next game 2026` |
| who won the Yankees game last night | `New York Yankees score result last night 2026` |
| what time does the Knicks game start | `New York Knicks start time` |
| when do the New York Yankees play next | `New York Yankees schedule next game 2026` (already has a city) |

The current year is appended unless the question already names one. Without it,
"World Cup score result" ranked a 2034 bid story and a 2023 basketball upset;
with it, the top hit is the actual final result. This one word is the difference
between the model having the answer available and having to invent it.

## Query shaping applies to every question, not just fixtures

`shape_general_query()` strips question framing from **all** searches and flips
"X of Y" into "Y X", because a spoken sentence ranks glossaries: "who is the CEO
of Starbucks" returned *"Hierarchy of Company: CEO, CFO, COO, CMO…"* until it
became `Starbucks CEO 2026`, which returns Brian Niccol.

`needs_fresh_results()` decides whether the answer moves over time — prices,
ratings, officeholders, hours, standings, stock, release dates — and pins the
current year via `pin_year()`. Stable facts are left alone: "capital of Portugal"
goes out as `Portugal capital`, with no year. The router path gets the same
treatment applied deterministically after the fact, because the model guesses
years badly (it once wrote "Dune 2024") but the rule itself is simple.

| spoken | query sent |
|---|---|
| who is the CEO of Starbucks | `Starbucks CEO 2026` |
| what is the price of gold | `gold price 2026` |
| who is the prime minister of Canada | `Canada prime minister 2026` |
| what is the capital of Portugal | `Portugal capital` — stable, unpinned |

## One retry when the first search comes back thin

`search_best()` wraps `search()`. If the first attempt returns nothing, or nothing
but homepage boilerplate, it retries once — pinning the year if it wasn't pinned,
otherwise swapping engine pools and category. If the retry is also thin it keeps
the first attempt's crumbs rather than losing them.

This is the automated form of the fix that rescued the World Cup answer by hand:
the undated query returned cricket scores and a 2034 bid story, the year-pinned
retry returned the actual final. Costs one extra SearXNG round trip (~1s) and only
on failures.

## STT damage limitation

Garbled transcripts produce garbled queries. Two fixes live in `_STT_FIXES`:

| heard | corrected |
|---|---|
| "the 20-26 FIFA World Cup" | "the 2026 FIFA World Cup" |
| "We won the World Cup" | "who won the World Cup" |

Spoken years come back hyphenated, and the hyphen survives into the search query
where it ranks nothing. "Who" is regularly heard as "We"; the correction only
applies at the start of a sentence followed by an article, since a question is far
likelier than someone reporting their own victory to a voice assistant.

`route_search_with_llm()` also strips operators and placeholders the model invents
when the transcript is mangled — one real query came back as
`We team name + 20-26 FIFA World Cup winner`.

Results are then ranked by `_rank()`: dated headlines first, homepage boilerplate
("Official match schedule of…", "Find New York news and weather on…") last. The
prompt from `format_for_prompt(live=True)` includes today's date and each result's
publish date, and tells the model to say it couldn't find the answer rather than
invent a fixture.

## Config (`server/config/server.yaml`)

```yaml
search:
  enabled: true
  url: "http://localhost:8080"
  result_count: 3            # top N snippets injected
  live_result_count: 6       # wider window for fixtures/results/local news
  home_city: "New York"       # disambiguates one-word team names
  max_tokens_search: 120     # LLM token budget (default is 65)
  timeout_seconds: 4         # fail fast rather than stall TTS
  engines: "google,bing,duckduckgo"
  news_engines: "google news,bing news,google,bing"
```

## The system prompt has to agree

`server/config/system_prompt.txt` used to say "You can't browse the internet" and
"for live internet data … acknowledge the limit". With those lines in place the
model refused live questions **even when the snippets were in its context** — the
search worked and the answer was still "I only have my training data". The
`WEB SEARCH RESULTS` section now tells it to treat injected results as current
fact. If you edit the persona, keep that section.

## SearXNG deployment

Docker Compose at `searxng/docker-compose.yml`, bound to `127.0.0.1:8080`.
Config at `searxng/searxng/settings.yml` (git-ignored): copy `settings.yml.example` and set `server.secret_key` (`openssl rand -hex 32`).

```bash
cd searxng && docker compose up -d
curl 'http://localhost:8080/search?q=test&format=json' | python3 -m json.tool | head -20
```

## Code locations

- `SearXNGSkill` class — after `news_cache` instantiation
- `is_search_request()`, `extract_search_query()` — after news intent parsing section
- Pipeline hook — in `run_pipeline()`, after the news block, before `extra_context` merge

## Graceful degradation

Any exception from SearXNG (timeout, down, JSON error) logs a warning and the LLM answers from weights. No error message is surfaced to the user.

## Limitations

- Adds 1-3s latency per search query, plus ~0.5-1.3s when the LLM router runs
- SearXNG must be running locally (`docker compose up -d`)
- Not triggered when timer/memory/news context is already set (search is lowest priority injector)
- **The `google` engine returns 0 results** on this instance (blocked upstream);
  bing, duckduckgo and google news carry the load. Leaving it in the engine list
  is harmless but it contributes nothing.
- **Anything whose answer lives on the page rather than in the snippet.** Fixtures
  are the obvious case (the date is behind JS on ESPN/MLS), but so are install
  instructions: for "how do I install a Moen 1225 cartridge" the results are the
  right pages — Manuals+ and MOEN's own guide — and the snippets carry no steps,
  so Antigua correctly says it couldn't find them instead of inventing a
  procedure. Fetching the top result's text ("deep read") is the fix for this
  whole class; a schedule API would fix sports specifically.
- STT is the other limiter: "Moen 1225" was heard as "Mone 1,225" and "is the new
  Zelda game any good" as "Is the new Zelda game, Anika?". The router recovered
  from the first, not the second.
