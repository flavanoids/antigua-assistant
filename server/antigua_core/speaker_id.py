"""Speaker profile storage and matching — shared by both servers.

Deliberately dependency-free (no torch, no numpy): this module only stores
192-dim centroid vectors as plain lists and compares them with cosine
similarity computed by hand. The fallback server imports antigua_core.pipeline
even where it has no SpeechBrain installed, so nothing in antigua_core can
require it. The actual embedding extraction (SpeechBrain ECAPA-TDNN) lives in
antigua_server.py — a backend concern, injected via Backend.identify_speaker,
like any optional Backend capability. Today only the primary implements it; the
fallback leaves the capability absent, which is a safe no-op (see below).

Safety property (non-negotiable): identify() returns None below the
similarity threshold. Speaker ID exists to *remove* a "who is this for?"
turn when confident — it must never introduce a new failure mode. A guest,
a kid, or an unenrolled voice all resolve to None and get asked, which is
correct. Misattributing a medication memory to the wrong person is far worse
than asking.
"""

import json
import logging
import math
from pathlib import Path
from threading import Lock

from . import settings

log = logging.getLogger("antigua_core")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _centroid(embeddings: list[list[float]]) -> list[float]:
    """Mean vector across enrollment utterances."""
    n = len(embeddings)
    dims = len(embeddings[0])
    return [sum(e[d] for e in embeddings) / n for d in range(dims)]


class SpeakerProfiles:
    """Enrolled per-person voice centroids, JSON-persisted."""

    def __init__(self, path: Path | None = None, min_similarity: float | None = None,
                 min_margin: float | None = None):
        # Resolved at call time — settings.configure() runs after import.
        self._path = path or settings.SPEAKER_PROFILES_PATH
        self._min_similarity = (
            min_similarity if min_similarity is not None else settings.SPEAKER_MIN_SIMILARITY
        )
        self._min_margin = (
            min_margin if min_margin is not None else settings.SPEAKER_MIN_MARGIN
        )
        self._lock = Lock()
        self._profiles: dict[str, list[float]] = {}  # person -> centroid
        self._load()

    def _load(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._profiles = data.get("profiles", {})
                log.info(
                    "SpeakerProfiles: loaded %d profile(s) from %s",
                    len(self._profiles), self._path,
                )
            except Exception as e:
                log.warning("SpeakerProfiles: load failed (%s), starting fresh", e)
                self._profiles = {}
        else:
            self._profiles = {}

    def _save(self):
        try:
            self._path.write_text(json.dumps({"profiles": self._profiles}, indent=2))
        except Exception as e:
            log.error("SpeakerProfiles: save failed: %s", e)

    def enroll(self, person: str, embeddings: list[list[float]]) -> None:
        """Store the mean of the given enrollment embeddings as person's centroid."""
        if not embeddings:
            raise ValueError("enroll() needs at least one embedding")
        centroid = _centroid(embeddings)
        with self._lock:
            self._profiles[person] = centroid
            self._save()
        log.info(
            "SpeakerProfiles: enrolled %s from %d utterance(s)", person, len(embeddings)
        )

    def identify(self, embedding: list[float]) -> tuple[str, float] | None:
        """Return (person, similarity) for the best match above the
        similarity floor that also beats the runner-up by min_margin, or None. Callers must also gate on minimum audio
        duration before extracting embedding() — a short clip and a
        low-confidence match are the same failure mode."""
        with self._lock:
            profiles = dict(self._profiles)
        scores = sorted(
            ((cosine_similarity(embedding, c), p) for p, c in profiles.items()), reverse=True
        )
        if not scores:
            return None
        best_score, best_person = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else -1.0
        if best_score >= self._min_similarity and best_score - runner_up >= self._min_margin:
            return best_person, best_score
        return None

    def enrolled_people(self) -> list[str]:
        with self._lock:
            return list(self._profiles.keys())
