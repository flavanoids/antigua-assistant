"""Conversation mode: "let's chat" opens a back-and-forth without the wake word.

Plain-speech follow-ups are off everywhere else because without AEC on the
Pi's speakers they looped (see the kitchen bridge's FOLLOW_UP_SPEECH_TRIGGER).
Here the user opts in, and every plain-speech turn has to pass an addressee
check first: the model is asked whether what the mic heard is someone
talking to Antigua, or a TV, people talking to each other, or her own voice
echoing back. Anything that isn't ends the mode silently. The wake word still
works as it always has.

While a chat is on, replies get a short conversational stage direction and
the conversation keeps more history (settings.CHAT_MAX_HISTORY).
"""

import logging
import random
import re
import time

import requests

log = logging.getLogger("antigua_core")

# Whole-utterance only: "talk to me about volcanoes" is a question, not a mode.
_START_RE = re.compile(
    r"^(?:hey[, ]+)?(?:(?:let'?s|let us|can we|could we|wanna|want to|i want to|i'd like to)\s+"
    r"(?:chat|talk|have a (?:chat|conversation))|chat with me|talk with me|keep me company"
    r"|(?:start )?(?:a )?conversation mode(?: on)?|start a conversation)"
    r"(?: for a (?:bit|while|minute|little))?(?: with (?:me|you))?(?: for a (?:bit|while|minute|little))?[.!?]*$",
    re.I,
)
_END_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|alright|well)[, ]+)?"
    r"(?:that'?s (?:all|it)|i'?m done|we'?re done|i'?m good|bye(?: bye)?|goodbye|good ?night"
    r"|talk (?:to you )?later|see you(?: later)?|stop chatting|end (?:the )?(?:chat|conversation)"
    r"|conversation mode off|never ?mind|gotta go|i have to go)"
    r"(?: for now)?(?:[, ]+(?:thanks|thank you)(?: antigua)?)?[.!?]*$",
    re.I,
)
_NAMED_RE = re.compile(r"\b(?:antigua|alexa)\b", re.I)
_WORD_RE = re.compile(r"[a-z']+")

_OPENERS = ["Sure, what's on your mind?", "I'm all ears.", "Okay, let's chat. What's up?",
            "Pull up a chair. What's going on?"]
_GOODBYES = ["Okay, talk later.", "Alright, I'll be here.", "Good chat. Later."]

HINT = ("You're in a relaxed back-and-forth chat. Answer like a friend talking out loud: one or two "
        "short sentences, no lists. Now and then end with a short question back, but not every time. "
        "Don't offer help, don't make up what happened to them.")

_IDLE_TTL = 600  # a chat nobody has spoken in for this long is over
_sessions: dict[str, dict] = {}


def is_start(transcript: str) -> bool:
    return bool(_START_RE.match(transcript.strip()))


def is_end(transcript: str) -> bool:
    return bool(_END_RE.match(transcript.strip()))


def opener() -> str:
    return random.choice(_OPENERS)


def goodbye() -> str:
    return random.choice(_GOODBYES)


def _cleanup():
    now = time.time()
    for cid in [c for c, s in _sessions.items() if now - s["last"] > _IDLE_TTL]:
        del _sessions[cid]


def active(conversation_id: str) -> bool:
    _cleanup()
    return conversation_id in _sessions


def begin(conversation_id: str):
    _sessions[conversation_id] = {"last": time.time(), "reply": "", "asked": False}
    log.info("Chat mode ON for conv %s", conversation_id)


def end(conversation_id: str, why: str = ""):
    if _sessions.pop(conversation_id, None) is not None:
        log.info("Chat mode OFF for conv %s%s", conversation_id, f" ({why})" if why else "")


def note_reply(conversation_id: str, reply: str, asked: bool):
    """What she just said, for the next turn's addressee check. asked = a
    skill question is waiting on the answer ("which timer?"), which is
    plainly meant for her and skips the check."""
    if s := _sessions.get(conversation_id):
        s.update(last=time.time(), reply=reply, asked=asked)


def needs_check(conversation_id: str) -> bool:
    s = _sessions.get(conversation_id)
    return bool(s) and not s["asked"]


def last_reply(conversation_id: str) -> str:
    return (_sessions.get(conversation_id) or {}).get("reply", "")


# ── Addressee check ──────────────────────────────────────────────────────────

_ADDRESSEE_PROMPT = """A voice assistant named Antigua is in a casual spoken chat with someone in a \
home kitchen. Right after Antigua spoke, its microphone picked up the speech below. Decide whether \
that speech is the person talking to Antigua (answering it, reacting, asking something, carrying on \
the chat) or something else: people talking to each other, a TV or video, song lyrics, one side of a \
phone call, or Antigua's own words echoing back.

Antigua's last line: "{last}"
Speech heard: "{heard}"

Reply with one word: YES if it is meant for Antigua, NO otherwise."""


def _echo(heard: str, last: str) -> bool:
    """Mostly her own last line coming back through the mic."""
    h, said = _WORD_RE.findall(heard.lower()), set(_WORD_RE.findall(last.lower()))
    return len(h) >= 3 and sum(w in said for w in h) / len(h) >= 0.7


def is_addressed(heard: str, last: str, *, ollama_host, model, timeout, keep_alive="15m",
                 num_ctx=8192) -> bool:
    """Fails closed: if the model can't answer, the chat ends rather than
    risk answering the TV."""
    if _NAMED_RE.search(heard):
        return True
    if last and _echo(heard, last):
        log.info("Chat addressee: echo of her last line")
        return False
    try:
        resp = requests.post(
            f"{ollama_host}/api/generate",
            json={
                "model": model,
                "prompt": _ADDRESSEE_PROMPT.format(last=last or "(nothing yet)", heard=heard.strip()),
                "stream": False,
                "think": False,
                # Must match the chat calls, or Ollama reloads the model.
                "options": {"temperature": 0, "num_predict": 3, "num_ctx": num_ctx},
                "keep_alive": keep_alive,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        answer = (resp.json().get("response") or "").strip().upper()
    except Exception as e:
        log.warning("Chat addressee check failed (ending chat): %s", e)
        return False
    log.info("Chat addressee: %r -> %s", heard, answer or "(empty)")
    return answer.startswith("YES")
