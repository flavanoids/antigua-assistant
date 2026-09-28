# Ingredient Substitution Skill

**Status:** Active  
**Pipeline stage:** Context injection (runs before general web search)  
**Machine:** primary (SearXNG query; skill code in `antigua_core/`)

## What it does

Detects ingredient substitution questions, rewrites them into a focused SearXNG query (`"ingredient substitute for <X>"`), and injects the top snippets as `extra_context` with a substitution-tuned LLM prompt. The LLM speaks the best substitute, the ratio if not 1:1, and any key texture or flavor trade-off.

## Trigger examples

- "What's a substitute for butter?"
- "Can I use honey instead of sugar?"
- "I don't have buttermilk, what can I use?"
- "I'm out of eggs, what can I substitute?"
- "Dairy-free substitute for heavy cream"
- "What can I use instead of bread crumbs in meatballs?"
- "Replace sour cream with something"
- "Vegan alternative for gelatin"

## Detection

Two functions in `antigua_server.py`:

- `is_substitution_request(transcript)` — matches `_SUBST_EXPLICIT_RE` (direct framing like "substitute for", "instead of", "I don't have") or `_SUBST_CONTEXTUAL_RE` ("X substitute in", "use Y instead")
- `extract_substitution_ingredient(transcript)` — three extraction strategies in priority order:
  1. "I don't have / I'm out of X" → captures X via `_SUBST_MISSING_RE`
  2. "instead of X" → captures X via `_SUBST_INSTEAD_RE`
  3. Strip leading framing + trailing qualifiers via `_SUBST_STRIP_RE` / `_SUBST_TRAILING_RE`

## Response

SearXNG is queried with `"ingredient substitute for <ingredient>"`. Results feed `format_substitution_prompt()` on `SearXNGSkill`, which instructs the LLM to include the best substitute, ratio, and trade-off in 1-2 spoken sentences.

## Code locations

- Detection: `is_substitution_request()`, `extract_substitution_ingredient()` (~line 1677 of `antigua_server.py`)
- Prompt: `SearXNGSkill.format_substitution_prompt()` (~line 742)
- Pipeline hook: `run_pipeline()`, substitution block before general web search block

## Config knobs

Inherits `search.*` from `server/config/server.yaml`:
- `search.enabled` — must be `true`
- `search.result_count` — number of snippets used
- `search.max_tokens_search` — LLM token budget
- `search.timeout_seconds` — SearXNG timeout

## Priority

Runs **before** general web search. If substitution context is set, the web search block is skipped for that turn. Substitution is skipped when timer, memory, or news context is already set (same priority rules as search).

## Limitations

- Ingredient name extraction may include surrounding words for complex phrasings like "what can I use instead of heavy cream in a cake if I'm lactose intolerant"
- Wikipedia parallel fetch is still fired (from `SearXNGSkill.search()`); the infobox result is discarded since substitution queries never produce infoboxes
- No ratio math — ratios come verbatim from search snippets
