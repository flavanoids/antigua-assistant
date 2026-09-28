"""Household members: the names Antigua remembers things for.

Everything here is built from settings.HOUSEHOLD (server.yaml's household:
block) at call time, because configure() runs after import. The compiled
regexes are cached per household, so they're built once per config.
"""

import functools
import re

from . import settings


@functools.lru_cache(maxsize=4)
def _build(members: tuple) -> dict:
    # members: ((name, nicknames, misheard_as, say_as), ...)
    canon = {}
    for name, nicks, _, _ in members:
        for word in (name, *nicks):
            canon[word.lower()] = name
    # Longest first so "katherine" wins over a nickname prefix; (?!) never
    # matches, so an empty household just recognises nobody.
    alt = "|".join(re.escape(w) for w in sorted(canon, key=len, reverse=True)) or "(?!)"
    return {
        "names": [m[0] for m in members],
        "canon": canon,
        "alt": alt,
        "remember": re.compile(rf"\bfor\s+({alt})\b|\b({alt})\b", re.IGNORECASE),
        "reply": re.compile(rf"^\s*(?:for\s+)?({alt})\s*[.,!?]?\s*$", re.IGNORECASE),
        "query": re.compile(rf"\b({alt})\b", re.IGNORECASE),
        "stt": [
            (re.compile(rf"\b{re.escape(heard)}\b"), name)
            for name, _, misheard, _ in members for heard in misheard
        ],
        "say": [
            (re.compile(rf"\b{re.escape(name)}\b"), say)
            for name, _, _, say in members if say
        ],
    }


def people() -> dict:
    return _build(tuple(
        (
            p["name"],
            tuple(p.get("nicknames") or ()),
            tuple(p.get("misheard_as") or ()),
            p.get("say_as") or "",
        )
        for p in settings.HOUSEHOLD
    ))


def canonical(raw: str) -> str | None:
    """'kat' -> 'Katherine'; None for anyone not in the household."""
    return people()["canon"].get(raw.lower())


def names() -> list[str]:
    return people()["names"]


def choice() -> str:
    """' — Alex or Sam' for a "who is this for?" prompt; '' if nobody
    (or only one person) is configured."""
    n = names()
    if len(n) < 2:
        return ""
    return " — " + ", ".join(n[:-1]) + " or " + n[-1]


def fix_mishearings(text: str) -> str:
    for pattern, name in people()["stt"]:
        text = pattern.sub(name, text)
    return text


def respell_for_tts(text: str) -> str:
    for pattern, say in people()["say"]:
        text = pattern.sub(say, text)
    return text
