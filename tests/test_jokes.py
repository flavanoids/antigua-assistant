#!/usr/bin/env python3
"""Tests for antigua_core.jokes — the joke bank.

Run: python3 tests/test_jokes.py
"""

import random
import re
import sys
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import jokes, persona  # noqa: E402
from antigua_core import settings  # noqa: E402


def run():
    settings.configure({})
    texts = [j for j, _ in jokes.JOKES]
    assert len(texts) == len(set(texts)), "duplicate joke"
    for j, tags in jokes.JOKES:
        # Spoken aloud: no symbols TTS would read out or mangle.
        assert not re.search(r"[*_#@&/%$]|\d", j), j
        assert tags and all(t == t.lower() for t in tags), j
        # House rules: no jokes about AI or about her.
        assert not re.search(r"\b(?:AI|Antigua|assistant|Alexa|Siri|chatbot)\b", j), j
        assert not {"ai", "antigua", "assistant", "yourself"} & set(tags), j

    # The whole deck is dealt before anything repeats, and it survives a restart
    # because the told list lives on disk.
    rng = random.Random(3)
    jokes._state_path().unlink(missing_ok=True)
    dealt = [jokes.pick(rng=rng) for _ in texts]
    assert len(set(dealt)) == len(texts)
    after = jokes.pick(rng=rng)   # reshuffled, but not one of the latest
    assert after not in dealt[-jokes._KEEP_AFTER_RESHUFFLE:]

    # Subjects pick from tagged jokes; plurals match; unknown subjects decline.
    cat = {j for j, t in jokes.JOKES if "cat" in t}
    jokes._state_path().unlink()
    assert jokes.pick("cats", rng=rng) in cat
    assert jokes.pick("tacos", rng=rng) in {j for j, t in jokes.JOKES if "taco" in t}
    assert jokes.pick("quantum accounting regulations", rng=rng) is None
    dad = {j for j, t in jokes.JOKES if "dad" in t}
    assert jokes.pick("dad", rng=rng) in dad
    # Once every joke on a subject is told, the LLM gets the subject.
    jokes._state_path().unlink()
    for _ in cat:
        jokes.pick("cat", rng=rng)
    assert jokes.pick("cat", rng=rng) is None

    # persona hands over the subject: none for a plain ask, "dad" for a dad joke.
    assert persona.direct("tell me a joke", "j1").subject is None
    assert persona.direct("tell me a dad joke", "j2").subject == "dad"
    assert persona.direct("tell me a joke about dogs", "j3").subject == "dogs"
    # "another dad joke" right after one is an encore, not a heckle.
    assert persona.direct("another dad joke", "j2").kind == "encore"
    assert persona.direct("tell me another one", "j2").kind == "encore"
    print("jokes: all passed")


if __name__ == "__main__":
    run()
