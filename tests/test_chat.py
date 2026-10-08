#!/usr/bin/env python3
"""Conversation mode ("let's chat"): phrases, the addressee gate, the chat
stage direction, interruption and the longer history. Stubbed backend, no
services.

Run: python3 tests/test_chat.py   (also works under pytest)
"""

import sys
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))
sys.path.insert(0, str(Path(__file__).parent))

from antigua_core import chat, pipeline, settings  # noqa: E402
from antigua_core.stores import ConversationStore  # noqa: E402
from test_pipeline import make_backend  # noqa: E402


def test_phrases():
    for s in ["let's chat", "Let's talk.", "can we chat for a bit", "talk with me",
              "keep me company", "conversation mode", "hey, let's have a conversation",
              "I want to chat with you"]:
        assert chat.is_start(s), s
    for s in ["talk to me about volcanoes", "let's talk about dinner", "what's the chat app",
              "how are you"]:
        assert not chat.is_start(s), s
    for s in ["that's all", "Okay, I'm done.", "bye", "good night", "talk to you later",
              "that's it for now, thanks", "never mind"]:
        assert chat.is_end(s), s
    for s in ["thanks, that's sweet", "I'm done with work today", "bye the way, what's up"]:
        assert not chat.is_end(s), s


def test_echo_and_names():
    last = "Honestly, Mondays are just Sundays with worse coffee."
    # Echo and a direct name never reach the model.
    def boom(*a, **kw):
        raise AssertionError("model called")
    real = chat.requests.post
    chat.requests.post = boom
    try:
        assert not chat.is_addressed("Mondays are just Sundays with worse coffee", last,
                                     ollama_host="x", model="m", timeout=1)
        assert chat.is_addressed("Antigua, that's not true", last,
                                 ollama_host="x", model="m", timeout=1)
    finally:
        chat.requests.post = real
    # A failed check ends the chat rather than answer the TV.
    assert not chat.is_addressed("what about Tuesdays", last,
                                 ollama_host="http://127.0.0.1:9", model="m", timeout=0.2)


def _chat_backend(addressed):
    be = make_backend("")
    seen = {"checks": [], "hints": [], "limits": []}

    def check(heard, last):
        seen["checks"].append((heard, last))
        return addressed(heard)

    def ask_llm_stream(transcript, **kw):
        seen["hints"].append(kw.get("turn_hint"))
        yield "Ha, fair enough."
        yield "What made you think of that?"

    be.check_addressed = check
    be.set_history_limit = lambda cid, n: seen["limits"].append((cid, n))
    be.ask_llm_stream = ask_llm_stream
    return be, seen


def test_gate():
    settings.SEARCH_ENABLED = False
    be, seen = _chat_backend(lambda heard: "news anchor" not in heard)
    pipeline.init(be)
    say = lambda text, follow_up=True: pipeline.dispatch_text(
        text, conversation_id="chat1", follow_up=follow_up, quiet=True)

    r = say("let's chat", follow_up=False)
    assert r["chat_mode"] and r["response"] in chat._OPENERS, r
    assert seen["limits"] == [("chat1", settings.CHAT_MAX_HISTORY)]

    # Plain speech meant for her: checked against her last line, answered with
    # the chat stage direction, chat stays on.
    r = say("I think I'm going to repaint the kitchen")
    heard, last = seen["checks"][-1]
    assert heard == "I think I'm going to repaint the kitchen" and last in chat._OPENERS, seen
    assert r["chat_mode"] and r["response"].startswith("Ha"), r
    assert seen["hints"][-1] == chat.HINT, seen["hints"]

    # A skill command mid-chat still works and keeps the chat on.
    r = say("set a tea timer for 3 minutes")
    assert r["response"] == "Tea timer set for 3 minutes." and r["chat_mode"], r
    pipeline.B.timers.cancel_all()

    # The wake word (follow_up=False) skips the check.
    n = len(seen["checks"])
    r = say("what do you think of teal", follow_up=False)
    assert len(seen["checks"]) == n and r["chat_mode"], r

    # Not for her: silent end, chat off.
    r = say("and now the news anchor with tonight's top stories")
    assert r.get("end_conversation") and not r["response"] and not r.get("chat_mode"), r
    assert not chat.active("chat1")


def test_goodbye_and_fallback():
    be, _ = _chat_backend(lambda heard: True)
    pipeline.init(be)
    pipeline.dispatch_text("let's chat", conversation_id="chat2", quiet=True)
    r = pipeline.dispatch_text("okay, that's all", conversation_id="chat2", follow_up=True, quiet=True)
    assert r["response"] in chat._GOODBYES and not r.get("chat_mode"), r
    assert not chat.active("chat2")

    # No addressee check (the fallback): no conversation mode.
    pipeline.init(make_backend(""))
    r = pipeline.dispatch_text("let's chat", conversation_id="chat3", quiet=True)
    assert "main server" in r["response"] and not r.get("chat_mode"), r


def test_pending_question_skips_check():
    be, seen = _chat_backend(lambda heard: False)
    pipeline.init(be)
    pipeline.dispatch_text("let's chat", conversation_id="chat4", quiet=True)
    chat.note_reply("chat4", "Which timer?", asked=True)
    assert not chat.needs_check("chat4")
    chat.end("chat4")


def test_interrupt_cuts_stream():
    be = make_backend("")

    def ask_llm_stream(*a, **kw):
        yield "First sentence."
        pipeline.request_stop()
        yield "Second sentence."
        yield "Third sentence."

    be.ask_llm_stream = ask_llm_stream
    pipeline.init(be)
    sent = []
    real = settings.mqtt_publish
    settings.mqtt_publish = lambda topic, payload: sent.append(topic)
    try:
        r = pipeline.dispatch_text("why is the sky blue", conversation_id="int1")
    finally:
        settings.mqtt_publish = real
    assert r["response"] == "First sentence.", r
    assert sent.count("antigua/play") == 1, sent


def test_history_limit():
    store = ConversationStore(ttl=300, max_hist=6)
    store.set_max_history("long", 20)
    for i in range(15):
        store.add_message("long", "user", f"u{i}")
        store.add_message("short", "user", f"u{i}")
    assert len(store.get_messages("long")) == 15
    assert len(store.get_messages("short")) == 6


def main():
    settings.configure({})
    test_phrases()
    test_echo_and_names()
    test_gate()
    test_goodbye_and_fallback()
    test_pending_question_skips_check()
    test_interrupt_cuts_stream()
    test_history_limit()
    print("PASS — chat suite")


if __name__ == "__main__":
    main()
