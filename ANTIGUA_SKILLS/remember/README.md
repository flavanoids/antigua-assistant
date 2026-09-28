# Skill: Remember (Voice Notepad)

**Status:** Active  
**Pipeline stage:** Server-side (`run_pipeline`) — pre-LLM, before garbage filter

---

## What It Does

A persistent, multi-user voice notepad. Users can save arbitrary facts with timestamps, query what was saved today, and delete the last entry. Supports the household members configured in `server.yaml` (`household:`), with disambiguation when a save request doesn't name the intended person. Medicine memories get a special follow-up to capture the specific drug name.

---

## How Users Trigger It

**Saving a memory:**
- "Remember I just took my medicine"
- "Don't forget I took my vitamin"
- "Make a note that I had breakfast"
- "Remember for Alex that he took his pill" ← person named inline, no disambiguation needed

**Querying (general):**
- "Did I take my medicine today?"
- "What did Katherine do this morning?"
- "When did Alex take his medicine?"

**Querying (medicine — direct answer with exact time):**
- "Did I take my medicine today?" → "Yes — you took your Tylenol at 8:30 AM today."
- "Did Alex take his meds?" → "Yes — Alex took vitamin D at 7:15 AM today."
- "Did I take my medicine?" (no person named) → "Who — Alex or Katherine?"

**Forgetting (last entry):**
- "Forget that"
- "Undo that"
- "That was wrong"
- "Forget Alex's last note"

**Forgetting (specific content):**
- "Forget that I took my medicine"
- "Delete the memory about breakfast"
- "Forget everything about vitamins"
- "Remove that note about walking the dog"
- "Forget my medicine memory"

---

## Speaker ID (Phase 4, 2026-07-27)

When the transcript names nobody ("remember I took my medicine", not "remember Alex took his medicine"), the server checks who's actually speaking before falling back to the disambiguation question. If SpeechBrain's ECAPA-TDNN model, run on the WAV in a background thread parallel to STT, is confident (cosine similarity ≥ `settings.SPEAKER_MIN_SIMILARITY`, default 0.75) that the voice matches an enrolled profile, the turn is skipped entirely:

```text
User: "Remember I took my medicine"
Antigua: "Got it. I'll remember Alex took his medicine at 10:32 AM."   ← no disambiguation turn
```

**Safety property (non-negotiable):** below the similarity threshold, or under 1.5s of audio, or for an unenrolled voice, speaker ID returns `None` and the pipeline falls straight through to the existing "Who am I remembering this for?" flow below — identical to Phase-3-and-earlier behavior. It never guesses. An explicit name in the transcript ("remember **for Katherine** that...") always wins over speaker ID, even if they'd disagree.

This applies to every memory branch that takes a `person` argument: save, both forget paths, the medicine query, and — because a bare "what did I say" names nobody either — the general memory query.

**Not yet enrolled.** `data/speaker_profiles.json` starts empty; until Alex and Katherine are enrolled (`python3 server/scripts/enroll_speaker.py enroll <name> <wav clips>`, 5–10 clips each, see the script's docstring), speaker ID always returns `None` and every memory branch behaves exactly as it did before this phase — a safe no-op, not a partial feature.

**Primary only.** SpeechBrain/torch would be installed on the primary only (they aren't installed yet); the backup server's `Backend.identify_speaker` is `None`, so it always asks, the same as an unenrolled speaker on the primary. See `ANTIGUA_SKILLS/README.md`'s pipeline-stage notes for why (heavy dependency, GPU-adjacent, and the safety property means "absent" is indistinguishable from "not confident").

Code: `antigua_core/speaker_id.py` (pure — profile storage, cosine similarity, dependency-free so the fallback can still import `antigua_core`), `antigua_server.py`'s `extract_speaker_embedding()` / `identify_speaker()` (the actual SpeechBrain call, primary-only), `antigua_core/pipeline.py`'s `run_pipeline()` (the parallel thread) and `_handle_memory_save` / `_handle_memory_forget_content` / `_handle_memory_forget_last` / `_handle_medicine_query` / the `memory_query` branch (`person = extract_..._person(t.transcript) or t.speaker`).

---

## Multi-Turn Disambiguation Flow

### Standard save

When a save request contains no person name **and speaker ID isn't confident**, the server asks and waits one turn:

```text
User: "Remember I took my medicine"
Antigua: "Who am I remembering this for — Alex or Katherine?"
User: "Alex"                          ← single-word reply (handled before garbage filter)
Antigua: "Got it. I'll remember Alex took his medicine at 10:32 AM."
```

### Medicine detail capture

If the saved fact uses a broad medicine term ("medicine", "meds", "medication", "pill") without naming the specific drug, the server asks for clarification:

```text
User: "Remember I took my medicine"
Antigua: "Who am I remembering this for — Alex or Katherine?"
User: "Alex"
Antigua: "What medicine did you take specifically?"
User: "Tylenol"
Antigua: "Got it. I'll remember Alex took Tylenol at 10:32 AM."
```

If the user already names the drug (e.g. "Remember I took Tylenol"), no follow-up is needed.

The pending state is keyed on `conversation_id` and expires after 60 seconds. The same disambiguation flow applies to all "forget" requests and medicine queries with no person named.

**Critical implementation note:** Disambiguation resolution runs BEFORE `is_garbage_transcript()` in `run_pipeline()`. Without this, single-word replies like "Alex" are rejected as garbage. See the bug note at the bottom.

---

## Storage

Memories are persisted to a JSON file (`data/memories.json`, relative to the repo root). The file is auto-created on first save. Each entry:

```json
{
  "id": "a3f1b2c4",
  "person": "Alex",
  "raw": "took his medicine",
  "timestamp": "2026-04-22T10:32:15"
}
```

Entries older than `memory.ttl_days` (default: 14) are auto-pruned on every write.

---

## Code Location

| What | Where |
|---|---|
| `MemoryStore` class | `server/antigua_core/stores.py` |
| `memory_store` instance | `Backend.memory_store`, built by each server |
| `_pending_memories` dict | Module-level — `dict[conv_id, {fact, expires}]` |
| Detection regexes | `_REMEMBER_INTENT_RE`, `_MEMORY_QUERY_RE`, `_FORGET_LAST_RE` in `classify.py`; person-name regexes built from the household in `antigua_core/household.py` |
| Intent functions | `parse_remember_request()`, `extract_remember_person()`, `is_memory_query()`, `extract_query_person()`, `is_forget_request()`, `parse_forget_content()`, `_is_medicine_query()`, `_answer_medicine_query()`, `_needs_medicine_detail()` |
| Pipeline hooks | `antigua_core/pipeline.py` `run_pipeline()` — before `is_garbage_transcript()` (pending resolution) and in memory skill block (new saves, queries, forget) |

---

## Config Knobs (`server/config/server.yaml`)

```yaml
household:                           # who memories are kept for
  - name: Alex
  - name: Katherine
    nicknames: [kat]                 # also accepted as this person
    # misheard_as: [Cat]             # STT outputs rewritten to the name
    # say_as: Kath-rin               # TTS respelling
memory:
  ttl_days: 14                       # entries older than this are auto-deleted
  storage_path: data/memories.json   # relative to the repo root
```

To add someone, add them to `household`. The name regexes are built from it at runtime (`antigua_core/household.py`), and the "who is this for?" prompt lists everyone.

---

## MQTT Events

Saves publish `antigua/memory` for the optional wall display (only when `display.enabled`). Otherwise, only the standard reply audio.

---

## Known Bugs / Limitations

- **Single-word disambiguation replies** were previously rejected by `is_garbage_transcript()`. Fixed: pending resolution now runs before the garbage filter in `run_pipeline()`.
- The server still does a full STT round-trip for the disambiguation reply (no shortcut — whisper must transcribe "Alex" before we can check it).
- No cross-session recall: if the user asks "what did I take yesterday?" without an active conversation, the memory query must be matched by `_MEMORY_QUERY_RE`. Phrases that don't match the regex fall through to the LLM, which has no memory context.
- Content-specific forget does substring matching against the raw memory text and tags. A request like "forget my medicine memory" searches for "medicine" in all entries for the named person and deletes the most recent match. "Forget everything about vitamins" deletes **all** matches.
- Medicine queries bypass the LLM and answer directly from today's entries. If no person is named, disambiguation is asked first. The answer format is: "Yes — {person} took {drug} at {time} today."
- Medicine detail capture only triggers for broad terms ("medicine", "meds", "medication", "pill"). Specific drug names like "Tylenol" or "vitamin D" skip the follow-up.
- `data/memories.json` is excluded from git (in `.gitignore` under `data/`). Back it up manually if needed.
- Speaker ID (see above) is unenrolled by default and primary-only. `data/speaker_profiles.json` is also gitignored.
- Speaker ID similarity threshold (0.75) was calibrated against synthetic TTS "voices" as a stand-in, not real recordings of household members — it may need adjusting after real enrollment. Watch the `Speaker ID: <person> (similarity=...)` log line and tighten `SPEAKER_MIN_SIMILARITY` in `antigua_core/settings.py` if a wrong match ever gets through, or loosen it if legitimate matches are consistently just below threshold.

---

## How to Extend

**Add "note for later" / future events:** Add an optional `due` field and surface items with `due <= now` as reminders via `antigua/play` on a background thread.

**Richer query patterns:** `_MEMORY_QUERY_RE` currently matches "did X take", "when did I", etc. Extend it for "what time did", "how long ago", etc.

**Per-user history length:** Pass `days=7` or `days=14` to `format_for_prompt()` for longer recall windows (at the cost of more tokens in the LLM context).

**Move sleep words to avoid wasted STT:** Pending resolution currently still runs full STT. If a name is transcribed as a common word, add it to that member's `misheard_as` that triggers a re-ask rather than a rejection.
