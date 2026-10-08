#!/usr/bin/env python3
"""Tests for antigua_core.persona — the per-turn stage direction.

Run: python3 tests/test_persona.py
"""

import random
import sys
from datetime import datetime
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import persona  # noqa: E402
from antigua_core import settings  # noqa: E402


def run():
    settings.configure({})
    read = persona.read_turn
    # Reading the moment.
    for s, kind in [
        ("tell me a joke", "joke"), ("know any good jokes?", "joke"),
        ("tell me a joke about cats", "joke"), ("make me laugh", "joke"),
        ("roast me", "roast"), ("you're so useless", "jab"), ("shut up", "jab"),
        ("thank you", "praise"), ("you're hilarious", "praise"),
        ("how are you", "about_her"), ("what's your favorite song", "about_her"),
        ("are you real", "about_her"), ("I'm so tired", "chat"), ("guess what", "chat"),
        ("good morning", "chat"),
        # Facts and tasks stay plain.
        ("what's the capital of France", None), ("how many cups in a gallon", None),
        ("who won the world series", None), ("turn off the lights", None),
    ]:
        assert read(s) == kind, (s, read(s), kind)
    # Follow-ups only count right after a joke.
    assert read("another one", "joke") == "encore"
    assert read("another one", None) is None
    assert read("that wasn't funny", "joke") == "heckle"
    assert read("I don't get it", "encore") == "heckle"

    # Joke directions: a subject and a device, both rotate rather than repeat.
    rng = random.Random(7)
    now = datetime(2026, 10, 5, 19, 0)  # a Monday evening in October
    seen_devices, seen_subjects = [], []
    for i in range(4):
        d = persona.direct("tell me a joke", f"c{i}", now=now, rng=rng)
        assert d.kind == "joke" and d.temperature > 0.7 and ", about " in d.hint
        seen_subjects.append(d.hint.split(", about ")[1].split(";")[0])
        seen_devices.append(d.hint.split("to build it, ")[1].split(". ")[0])
        persona.record(d, f"Joke number {i} goes like this and lands.")
    assert len(set(seen_devices)) == 4, seen_devices
    assert len(set(seen_subjects)) == 4, seen_subjects
    # A first turn never gets a callback (nothing to call back to).
    assert "earlier in this conversation" not in " ".join(seen_devices)
    # Told jokes are named so the next one isn't the same.
    d = persona.direct("tell me a joke", "c9", now=now, rng=rng)
    assert '"Joke number 3 goes like this and…"' in d.hint, d.hint
    # An asked-for subject wins.
    d = persona.direct("tell me a joke about tacos", "c10", now=now, rng=rng)
    assert ", about tacos;" in d.hint

    # Conversation memory: "another one" after a joke in the same conversation.
    persona.direct("tell me a joke", "conv", now=now, rng=rng)
    d = persona.direct("one more", "conv", now=now, rng=rng)
    assert d.kind == "encore" and "different joke from the last" in d.hint

    # Edge: late night is gentler; a jab gets more.
    settings.PERSONA_SASS = 2
    late = persona.direct("how are you", "x1", now=datetime(2026, 10, 5, 2, 0))
    day = persona.direct("how are you", "x2", now=datetime(2026, 10, 5, 14, 0))
    jab = persona.direct("you're dumb", "x3", now=datetime(2026, 10, 5, 14, 0))
    assert late.hint.endswith(persona._EDGE[1]) and day.hint.endswith(persona._EDGE[2])
    assert jab.hint.endswith(persona._EDGE[3])
    settings.PERSONA_SASS = 0
    assert persona.direct("you're dumb", "x4", now=datetime(2026, 10, 5, 14, 0)).hint.endswith(persona._EDGE[1])

    # Plain questions get nothing.
    assert persona.direct("what time does the store close", "y") is None
    print("persona: all passed")


if __name__ == "__main__":
    run()
