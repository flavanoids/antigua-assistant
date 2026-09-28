# Skill: Sleep Words (End Conversation)

**Status:** Active
**Pipeline stage:** Kitchen bridge, after `/pipeline` returns the transcript

---

## What It Does

Ends the current conversation when the user says a closing phrase. Antigua
still replies normally (usually something short like "No problem"). After
that, the bridge drops the conversation: no follow-up window opens, and the
next wake word starts a fresh context.

---

## How Users Trigger It

These phrases work alone or at the end of a sentence (case-insensitive, with
trailing punctuation stripped):

- "Thank you" / "Thanks"
- "Stop" / "Stop listening"
- "Goodbye"
- "That's all"

---

## Code Location

| What | Where |
|---|---|
| Word list | `SLEEP_WORDS` in `kitchen-mic/bridge/kitchen_bridge.py` |
| Handling | The `/pipeline` response handler calls `_end_conversation()`: it clears `conversation_id` and the follow-up counter and returns to wake-word listening |

`_end_conversation()` deliberately leaves the playback mute alone, so the mic
doesn't hear the closing reply and fire a new wake. Only `antigua/done` (or the
20-second safety timeout) unmutes it.

---

## Limitations

- The server does a full turn (STT, LLM, TTS) for the closing phrase. That's
  intended, since a short acknowledgement is spoken.
- A sentence ending in a sleep word ends the conversation even if it wasn't
  meant to ("don't stop").
- The list is a constant in the bridge, not config.
- To stop a ringing alarm, say the wake word. Sleep words don't dismiss it.
