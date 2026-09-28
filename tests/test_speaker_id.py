#!/usr/bin/env python3
"""Pure unit tests for antigua_core.speaker_id — no torch/SpeechBrain needed.

SpeakerProfiles only ever sees plain embedding vectors; these tests use fake
ones to exercise the storage, centroid, and threshold logic in isolation from
the (heavy, primary-only) embedding extraction itself.

Run: python3 tests/test_speaker_id.py   (also works under pytest)
"""

import sys
import tempfile
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core.speaker_id import SpeakerProfiles, cosine_similarity  # noqa: E402


def _store():
    tmp = Path(tempfile.mkdtemp(prefix="antigua_speaker_test_"))
    return SpeakerProfiles(path=tmp / "profiles.json", min_similarity=0.75)


def main():
    # Identical vectors -> similarity 1.0
    assert cosine_similarity([1, 0, 0], [1, 0, 0]) == 1.0
    # Orthogonal -> 0.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0
    # Zero vector is handled without a ZeroDivisionError
    assert cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0

    sp = _store()
    assert sp.enrolled_people() == []
    assert sp.identify([1.0, 0.0, 0.0]) is None  # nothing enrolled yet

    sp.enroll("Alex", [[1.0, 0.0, 0.0], [0.98, 0.02, 0.0], [0.97, 0.03, 0.01]])
    sp.enroll("Katherine", [[0.0, 1.0, 0.0], [0.02, 0.97, 0.01]])
    assert set(sp.enrolled_people()) == {"Alex", "Katherine"}

    # Confident match
    person, score = sp.identify([1.0, 0.0, 0.0])
    assert person == "Alex" and score > 0.99, (person, score)

    # A blend that resembles neither closely enough falls through to None —
    # this is the safety property: ambiguous audio must never guess.
    assert sp.identify([0.5, 0.5, 0.5]) is None

    # Persistence: a fresh SpeakerProfiles instance over the same path sees
    # the same profiles (matches MemoryStore/ListStore's load-on-init pattern).
    sp2 = SpeakerProfiles(path=sp._path, min_similarity=0.75)
    assert set(sp2.enrolled_people()) == {"Alex", "Katherine"}
    assert sp2.identify([0.0, 1.0, 0.0])[0] == "Katherine"

    # Re-enrolling replaces, not blends
    sp.enroll("Alex", [[0.0, 0.0, 1.0], [0.0, 0.01, 0.99]])
    match = sp.identify([1.0, 0.0, 0.0])
    assert match is None or match[0] != "Alex", match  # old Alex voiceprint is gone

    print("PASS — speaker_id unit suite")


if __name__ == "__main__":
    main()
