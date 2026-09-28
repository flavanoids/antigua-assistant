# Skill: Lists

**Status:** Active
**Pipeline stage:** LLM bypass — `ListStore` is a plain JSON-backed store and every reply is built deterministically in Python. No LLM round-trip, no risk of the model claiming an item was added when it wasn't.

---

## What It Does

Named, household-shared, ordered lists — shopping and to-do by default. Unlike `remember/` (per-person, TTL'd, tagged), lists are permanent and shared: nobody is asked "who is this for?" Add, read, remove, and clear are all supported, with multi-item adds ("milk, eggs and bread") split the way Alexa does.

This was the first feature written entirely in `antigua_core` (Phase 3, 2026-07-27) — it landed in both the primary and fallback servers for free, which is the point of the Phase 2 extraction: one `ListStore`, one set of parsers, one set of pipeline handlers, shared by both backends.

---

## How Users Trigger It

**Add:**
- "Add milk to the shopping list"
- "Add milk, eggs and bread to the list" (multi-item — split on comma and "and")
- "Put bread on my todo list"
- "Add call the vet to my to-do list"

**Read:**
- "What's on my shopping list?"
- "What's on the list?" (defaults to shopping)
- "Read my list"
- "Read the todo list"

**Remove one item:**
- "Remove milk from the shopping list"
- "Take eggs off the list"
- "Delete bread from my todo list"

**Clear a whole list:**
- "Clear the shopping list"
- "Empty my list"

**Named lists:** "shopping" (aliases: grocery, groceries) and "todo" (aliases: to-do, to do, task, tasks) are recognized; an unnamed "the list" / "my list" defaults to shopping, since it's the more common household use. Any other word before "list" is accepted too (e.g. "the reading list") and stored under that name — the alias table isn't a hard whitelist.

---

## Detection

```python
parse_list_add_request(transcript)     # -> (list_name, [items]) or None
is_list_query(transcript)              # -> bool
extract_list_query_name(transcript)    # -> list_name (defaults to "shopping")
parse_list_remove_request(transcript)  # -> (list_name, item) to remove one,
                                        #    (list_name, None) to clear, or None
```

All in `server/antigua_core/classify.py`. Patterns: `_LIST_ADD_RE`, `_LIST_QUERY_RE`, `_LIST_CLEAR_RE`, `_LIST_REMOVE_RE`, `_LIST_NAME_GROUP`, `_LIST_ALIASES`.

**Collision guards:** `_LIST_ADD_RE` requires "add/put ... to/on ... list", which never matches `_REMEMBER_INTENT_RE`'s "remember/don't forget/make a note/..." openers, so "remember that I took my medicine" can never be mistaken for a list add. `_LIST_QUERY_RE` requires "list" as the final word, which `_MEMORY_QUERY_RE` never produces, so "what did I say I took" and "what's on my shopping list" can't collide either. Both directions are pinned in `tests/fixtures/routing.yaml` — the `memory_save`/`memory_query` fixtures and the `list_add`/`list_query` fixtures share the suite, so a regex change that makes one steal the other's utterance fails immediately.

Multi-item split (`_split_list_items`): `"milk, eggs and bread"` → normalizes the last `"and"` to a comma, then splits — `["milk", "eggs", "bread"]`. Works with just commas or just "and" too.

Route order (`ROUTE_ORDER` in `classify.py`): `list_add` → `list_query` → `list_remove`, placed right after the memory routes and before the timer block.

---

## Response

All three routes are deterministic — `_speak_items()` joins multiple items Alexa-style ("milk, eggs, and bread"), `_list_label()` renders the spoken list name ("shopping list" / "to-do list" / "`<name>` list").

- **Add:** "Added milk, eggs, and bread to your shopping list." Items already on the list (case-insensitive) are silently skipped; if every requested item was already present: "That's already on your shopping list."
- **Read:** "On your shopping list: milk, eggs, and bread." Empty: "Your shopping list is empty."
- **Remove:** "Removed milk from your shopping list." No match: "I don't see milk on your shopping list." (matches on exact or substring, case-insensitive.)
- **Clear:** "Cleared 3 items from your shopping list." Already empty: "Your shopping list was already empty."

---

## Code Location

| What | Where |
|---|---|
| Store | `ListStore` class in `server/antigua_core/stores.py` |
| Persistence | `ListStore._save()` / `._load()` → `data/lists.json` (the backup keeps its own copy under its deploy directory — see Limitations) |
| Detection | `parse_list_add_request()`, `is_list_query()` + `extract_list_query_name()`, `parse_list_remove_request()` in `antigua_core/classify.py` |
| Pipeline hooks | `_handle_list_add()`, `_handle_list_query()`, `_handle_list_remove()` in `antigua_core/pipeline.py` (`_HANDLERS` map) |
| HTTP endpoint | `GET /lists` in both `AntiguaHandler` (`antigua_server.py`) and `FallbackHandler` (`antigua_fallback_server.py`) — returns `{"lists": {"shopping": [...], "todo": [...]}}` |
| Backend wiring | `list_store` field on `pipeline.Backend`, instantiated once per server (`list_store = ListStore()`) and passed into `pipeline.init(...)` |

---

## Config Knobs

No list-specific config in `server.yaml`. `LISTS_STORE_PATH` in `antigua_core/settings.py` defaults to `data/lists.json`; the fallback overrides it to its own data directory the same way it overrides `MEMORY_STORE_PATH` and `TIMER_STORE_PATH`.

---

## MQTT Events

None yet — lists don't publish anything over MQTT. (Memories publish `antigua/memory` for the display; a `antigua/list` equivalent would be the natural addition if a display card is ever built — see How to Extend.)

---

## Limitations

- **`/lists` JSON is the minimum viable surface.** The value of a list is checking it in the grocery store — a phone-viewable page is out of scope for this phase. Recorded as a choice, not an oversight.
- **The primary and fallback keep separate list files**, same as memory and timers. An item added while the fallback is active (primary down) won't appear once the primary is back, and vice versa. This is the existing, accepted tradeoff for all per-server state in this codebase, not something new to lists.
- **No undo.** Clearing a list is immediate and irreversible; there's no "did you mean" confirmation.
- **Fuzzy matching on remove is substring-based**, not semantic — "remove milk" will also match an item literally called "chocolate milk" (first match wins). Fine for a voice-only shopping list at this scale; would need tightening if lists grow long.
- **List names are open-ended** — anything followed by the word "list" is accepted and persisted under that literal name, so a mishearing ("the shipping list") creates its own separate list rather than erroring. Low-risk (worst case is an extra near-empty list), not guarded against.

---

## How to Extend

**A phone-viewable page:** Build a small static page (or a PinedaDisplay card) that polls `GET /lists` — the JSON is already there, this is purely a presentation layer.

**MQTT events:** Publish `antigua/list` on add/remove/clear, mirroring `antigua/memory`, if a live display card is built.

**Fuzzy/semantic remove matching:** Replace the substring check in `ListStore.remove()` with `difflib.SequenceMatcher`, the same approach `MemoryStore.add()` already uses for duplicate detection.

**Shared list state across primary/fallback:** Would need `ListStore` to read/write a location both boxes can reach (e.g. an MQTT-synced store or a shared network path) instead of a local JSON file — a bigger change than this phase scoped for.
