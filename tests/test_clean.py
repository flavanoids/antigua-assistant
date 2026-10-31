#!/usr/bin/env python3
"""Clean replies (clean.py): come-ons and bare innuendo bait never reach the
model, and a suggestive sentence from the model is never spoken. Stubbed
backend, no services.

Run: python3 tests/test_clean.py   (also works under pytest)
"""

import random
import sys
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))
sys.path.insert(0, str(Path(__file__).parent))

from antigua_core import clean, persona, pipeline, settings  # noqa: E402
from test_pipeline import make_backend  # noqa: E402


def test_brush_off():
    rng = random.Random(1)
    for s in ["Make it bigger.", "What's up? Make it bigger.", "make it longer", "Go deeper.",
              "Can you make it bigger for me, baby?", "put it in", "Bigger."]:
        assert clean.brush_off(s, rng=rng) == clean.MISHEARD, s
    for s in ["Talk dirty to me.", "flirt with me", "You're sexy.", "What are you wearing?",
              "Come to bed with me.", "kiss me", "Let's get naked.", "I want you.",
              "Say something sexy", "Spank me."]:
        assert clean.brush_off(s, rng=rng) in clean._BRUSH_OFFS, s
    for s in ["Are you single?", "Will you marry me?"]:
        assert clean.brush_off(s, rng=rng) in clean._JUST_ME, s
    assert clean.brush_off("Good girl.") == "Mm-hm."
    assert clean.brush_off("Eres muy sexy.", "es", rng=rng) in clean._BRUSH_OFFS_ES
    assert clean.brush_off("make it bigger", "es") == clean.MISHEARD_ES
    # Everyday words stay with the model.
    for s in ["How are you?", "It's so hard.", "It's too big.", "why do cats moan at night",
              "update me on the news", "make me laugh", "Do you love us?",
              "make the timer longer by five minutes", "I want you to set a timer",
              "Can you see Saturn with the naked eye?", "What should I make for dinner?"]:
        assert clean.brush_off(s) is None, s


def test_suggestive():
    for s in ["I'd be happy to come to bed with you.", "Sure, I'm all yours.",
              "I can definitely get a little cheeky if you're up for it.",
              "You want me to go longer? Like, really long?", "Okay, I'll make you feel good.",
              "I can't touch you, but I can set the mood.", "You want me to flirt? Sure.",
              "Make what bigger, baby?", "I keep things clean and straightforward.",
              "That's about a picture, not me.", 'My favorite is "S&M".']:
        assert clean.suggestive(s), s
    for s in ["Yes, you can see Saturn with the naked eye.", "That's by Kiss.",
              "Add a spoon of honey, then stir.", "Hard-boil the eggs for ten minutes.",
              "Bake it until it's a little deeper brown.", "Too big for what?",
              "Good night.", "I'm glad to be in your house."]:
        assert not clean.suggestive(s), s


def test_joke_subjects():
    d = persona.direct("tell me a joke about sex", "j1")
    assert d.kind == "joke" and d.subject is None and "sex" not in d.hint, d
    assert persona.read_turn("make me feel good") == "joke"


def _backend(sentences):
    be = make_backend("")
    calls = []

    def ask_llm_stream(transcript, **kw):
        calls.append(transcript)
        yield from sentences

    be.ask_llm_stream = ask_llm_stream
    return be, calls


def test_pipeline():
    settings.SEARCH_ENABLED = False
    say = lambda text: pipeline.dispatch_text(text, conversation_id="clean", quiet=True)

    # A come-on never reaches the model.
    be, calls = _backend(["Anything."])
    pipeline.init(be)
    assert say("Talk dirty to me.")["response"] in clean._BRUSH_OFFS
    assert say("What's up? Make it bigger.")["response"] == clean.MISHEARD
    assert calls == [], calls

    # A suggestive first sentence is replaced, and nothing after it is said.
    be, calls = _backend(["You want me to go longer? Like, really long?", "Okay, I'm listening."])
    pipeline.init(be)
    r = say("Tell me about the river.")
    assert r["response"] == clean.MISHEARD, r

    # A later one ends the reply where it was.
    be, _ = _backend(["I like you both.", "I'm all yours, darling.", "Truly."])
    pipeline.init(be)
    r = say("Tell me about the house.")
    assert r["response"] == "I like you both.", r

    # Clean replies are untouched.
    be, _ = _backend(["Sixteen cups.", "Give or take."])
    pipeline.init(be)
    assert say("Tell me about gallons.")["response"] == "Sixteen cups. Give or take."


def main():
    settings.configure({})
    test_brush_off()
    test_suggestive()
    test_joke_subjects()
    test_pipeline()
    print("PASS — clean suite")


if __name__ == "__main__":
    main()
