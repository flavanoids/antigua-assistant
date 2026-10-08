# Skill: Conversation Memory

**Status:** Active  
**Pipeline stage:** Cross-turn context — history passed into every LLM call within a session

---

## What It Does

Maintains conversation history across multiple turns so the user can ask follow-up questions without repeating context. History is stored server-side, keyed by a conversation ID that the satellite sends with every request via HTTP header. With **follow-up mode** (default on), plain speech within the follow-up window continues the conversation — no wake word needed.

---

## Conversation mode ("let's chat", 2026-10-04)

Plain-speech follow-ups are off for ordinary replies (they looped on
Antigua's own voice and the TV; see `FOLLOW_UP_SPEECH_TRIGGER` in
`kitchen-mic/bridge/kitchen_bridge.py`). Conversation mode is the opt-in
way to talk back and forth without repeating the wake word:

```text
"Alexa, let's chat"            → "Sure, what's on your mind?"   (LED: soft green breathe)
"I'm thinking of repainting the kitchen"
→ "Ooh, bold. What color are you leaning toward?"
"Maybe a dark green"           ← no wake word, no beep
"Alexa—" (mid-reply)           ← cuts her off; say the new thing
"Okay, that's all"             → "Okay, talk later."
```

- **Start:** a whole-utterance phrase: "let's chat/talk", "can we chat for a
  bit", "keep me company", "conversation mode" (`chat._START_RE`; "talk to
  me about volcanoes" is a question, not a mode).
- **While on:** every reply carries `chat_mode: true`; the bridge reopens a
  20s plain-speech window after each reply (`CHAT_TIMEOUT_S`, no turn cap).
  Free-form turns get a chatty stage direction (1–2 sentences, sometimes a
  question back; `chat.HINT`) unless a persona direction, search or an
  article already shapes the answer. Skills (timers, weather…) work as usual
  and don't end the chat. History holds `CHAT_MAX_HISTORY` (20) messages
  instead of 6.
- **Guard:** each plain-speech turn passes an addressee check first
  (`chat.is_addressed`): her own last line coming back → no; her name → yes;
  otherwise one YES/NO call to the 4B model (~0.2s, fails closed after
  `CHAT_ADDRESSEE_TIMEOUT`). Not for her → the chat ends silently with the
  soft `chat_end.wav` cue. Wake-word turns and answers to a pending skill
  question skip the check.
- **Interrupt:** while a chat reply plays on the Pi, the bridge keeps
  scoring the wake word at `BARGE_IN_THRESHOLD` (0.9). A hit publishes
  `antigua/stop` (the Pi drops its queue and kills paplay; the server stops
  publishing the rest of the reply) and starts a new recording.
- **End:** "that's all", "bye", "talk later"… (spoken goodbye), 20s of
  silence, or speech that wasn't for her (both with the end cue). In a chat
  the bridge's sleep words don't apply ("thanks, that's sweet" is chat).
- Primary only: the fallback has no addressee check and says chatting needs
  the main server.

---

## How Users Trigger It

No explicit trigger — it's always active when inside a conversation window.

**Starting a conversation:** Say the wake word → ask anything.  
**Continuing (follow-up mode, default):** Within 8 seconds of the response ending, just speak — no wake word. Saying the wake word also works and gets the usual beep.

```text
"Alexa" → "Who was Frida Kahlo?"
→ [response]
"Where was she born?"                 ← no wake word needed
"Tell me more about that"             ← full context retained
```

**Continuing (follow-up mode off):** Say the wake word again within `continuation_timeout_s` (default 15s).

**Ending:** Say a sleep word, or let the window expire in silence.

### Follow-up mode internals (satellite)

- After playback the satellite arms the wake word detector **and** the VAD
  (`verify_speech`, polled in 500ms slices) concurrently on the shared
  `MicStream` for `follow_up_timeout_s`. Either continues the conversation.
- `FOLLOW_UP_ARM_DELAY_MS` (350ms) passes before the VAD arms, so room reverb
  from Antigua's own playback can't self-trigger the window.
- Audio consumed by `verify_speech` is returned and passed to
  `record(preroll=...)` so the start of the utterance is not clipped.
- A VAD-opened turn is sent with `X-Follow-Up: 1`. If the server's
  `is_garbage_transcript()` rejects the transcript (background TV opened the
  window), it returns `end_conversation: true` with no audio and the satellite
  ends the conversation **silently** — no "didn't catch that".
- One-word skill triggers (`louder`, `snooze`) are exempted from the garbage
  filter server-side, so a bare "snooze" at a ringing alarm works.

---

## Detection

No transcript detection needed. Conversation state is managed by:
- `conversation_id` — a hex string generated on the first turn, carried in `X-Conversation-ID` HTTP header
- `ConversationStore` — thread-safe LRU dict on the server

---

## Response

LLM-generated. The full message history (up to `MAX_HISTORY` messages) is passed as the `messages` array to Ollama's `/api/chat`. The server inserts an updated system prompt as `messages[0]` on every turn.

```python
messages = conversations.get_messages(conversation_id)  # previous turns
messages.insert(0, {"role": "system", "content": system_with_context})
messages.append({"role": "user", "content": f"{transcript} /no_think"})
# → sent to Ollama, response appended to history
```

---

## Code Location

| What | Where |
|---|---|
| History store | `ConversationStore` class (~line 561, `server/antigua_server.py`) |
| Get/create session | `ConversationStore.get_or_create(conv_id)` |
| Add turn | `ConversationStore.add_message(conv_id, role, content)` |
| History injection | `ask_llm()` (~line 653) — builds message array |
| ID generation | `run_pipeline()` — `os.urandom(8).hex()` on first turn |
| ID header | `send_to_server()` in satellite — `X-Conversation-ID` header |
| Follow-up window | Satellite `main()` — dual-armed detector + `verify_speech` loop |
| Follow-up flag | `send_to_server()` — `X-Follow-Up` header; server silent-ends garbage follow-ups |
| Continuation timeout | Satellite `main()` — `CONTINUATION_TIMEOUT_S` (used when `follow_up_mode: false`) |
| TTL cleanup | `cleanup_loop()` — runs every 60s, expires sessions older than `CONVERSATION_TTL` |

---

## Config Knobs

In `server/antigua_server.py` (not in YAML currently):

```python
CONVERSATION_TTL = 300   # seconds before an idle conversation is expired (5 min)
MAX_HISTORY = 6          # max messages retained per conversation (3 turns)
```

In `antigua_satellite.py` (or `satellite.yaml`):

```yaml
satellite:
  follow_up_mode: true         # plain speech continues the conversation
  follow_up_timeout_s: 8       # seconds the follow-up window stays open
  continuation_timeout_s: 15   # wake-word window, used when follow_up_mode: false
```

---

## MQTT Events

| Topic | Direction | Payload | When |
|---|---|---|---|
| `antigua/done` | server → Pi | `{"state": "complete"}` | Turn finished |
| `antigua/done` | server → Pi | `{"state": "sleep"}` | Sleep word or timeout |
| `antigua/status` | server → Pi | `{"state": "awaiting_continuation"}` | Waiting for follow-up wake word |

---

## Limitations

- History is in-memory only. Server restart clears all conversations.
- `MAX_HISTORY = 6` keeps the last 3 full turns (user + assistant pairs), plus the first message is always preserved. Older turns are dropped.
- Each turn re-sends the full history to Ollama — longer conversations increase LLM latency slightly.
- The system prompt is rebuilt on every turn (time, and weather when relevant), so it's always fresh but adds overhead. Measured on the primary: **the persona alone costs ~1.2s of prompt eval per turn** at 733 tokens. Ollama re-evaluates it every time — a repeated *identical* prompt hits its cache (0.07s), but any change to the system message (the clock ticks every minute) invalidates it. Keeping the persona short is therefore a direct latency win, not just tidiness.
- Tone rules live in `server/config/system_prompt.txt`. Two behaviours are load-bearing and easy to undo by accident:
  - **FACTUAL ANSWERS / BEFORE YOU SPEAK** — without these the model appends jokes and asides to plain facts ("sixteen cups in a gallon, so if you're measuring coffee for the humidity to match your mood…").
  - **WEB SEARCH RESULTS** — without it the model refuses live questions even with search snippets in context.
- The 4b model follows negative instructions unreliably. Prompt wording got unsolicited weather talk from 3/8 chit-chat turns down to 0/8 only in combination with the deterministic guard in `weather_claim_allowed()`; wording alone plateaued around 3/8.

---

## How to Extend

**Longer history:** Increase `MAX_HISTORY` in `antigua_server.py`. Watch for LLM latency increase with deep history.

**Persist across restarts:** Serialize `ConversationStore._convs` to JSON on a background timer. On startup, reload and check `last_used` against `CONVERSATION_TTL`.

**User-identified sessions:** Replace the random `conv_id` with a speaker-identification token if speaker diarization is added. Each user would have their own history.

**Explicit reset command:** Detect "start over" or "forget that" before the LLM, clear the conversation from `ConversationStore`, and reply "Starting fresh."
