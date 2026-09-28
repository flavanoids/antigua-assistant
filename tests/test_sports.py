#!/usr/bin/env python3
"""Tests for the deterministic sports-scores skill (NFL/MLB/NBA/MLS/F1).

No network — phrase_*() and resolve_team() are pure functions driven by
synthetic data, same discipline as test_weather.py/test_calc.py. Every
returned phrase is pushed through clean_for_tts() and checked for a
surviving digit/comma, since the pipeline speaks skill replies verbatim.

Run: python3 tests/test_sports.py   (also works under pytest)
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

settings.configure({})

from antigua_core import sports  # noqa: E402
from antigua_core.classify import classify  # noqa: E402
from antigua_core.tts_text import clean_for_tts  # noqa: E402

_DIGIT_RE = re.compile(r"\d")


# ── resolve_team() ──────────────────────────────────────────────────────────

RESOLVE_CASES = [
    ("what's the score of the astros game", "mlb", "hou"),
    ("did the texans win", "nfl", "hou"),
    ("how did the rockets do", "nba", "hou"),
    ("when is the next dynamo game", "mls", "6077"),
    ("did the packers win", "nfl", "gb"),
    ("who won the lakers game", "nba", "lal"),
]

RESOLVE_NONE_CASES = [
    "what's for dinner",
    "what's the weather like",
]


def _check_resolve():
    failed = 0
    for text, league, team_id in RESOLVE_CASES:
        r = sports.resolve_team(text)
        if r is None or (r.league, r.team_id) != (league, team_id):
            failed += 1
            print(f"[FAIL] resolve_team({text!r}) -> {r!r}, expected ({league!r}, {team_id!r})")
        else:
            print(f"[ok] resolve_team({text!r}) -> {r.display_name}")

    for text in RESOLVE_NONE_CASES:
        r = sports.resolve_team(text)
        if r is not None:
            failed += 1
            print(f"[FAIL] resolve_team({text!r}) -> {r!r}, expected None")
        else:
            print(f"[ok] resolve_team({text!r}) -> None")

    # Giants/Cardinals: shared nickname, no qualifier -> documented default (NFL).
    r = sports.resolve_team("who won the giants game")
    if r is None or r.league != "nfl":
        failed += 1
        print(f"[FAIL] ambiguous 'giants' -> {r!r}, expected nfl default")
    else:
        print(f"[ok] ambiguous 'giants' defaults to {r.display_name}")

    r = sports.resolve_team("who won the football giants game")
    if r is None or r.league != "nfl":
        failed += 1
        print(f"[FAIL] 'football giants' -> {r!r}, expected nfl")
    else:
        print(f"[ok] 'football giants' -> {r.display_name}")

    r = sports.resolve_team("who won the baseball giants game")
    if r is None or r.league != "mlb":
        failed += 1
        print(f"[FAIL] 'baseball giants' -> {r!r}, expected mlb")
    else:
        print(f"[ok] 'baseball giants' -> {r.display_name}")

    return failed


# ── phrase_*() with synthetic TeamSummary data ──────────────────────────────

def _ts(**kwargs) -> sports.TeamSummary:
    base = dict(league="mlb", team_id="hou", display_name="Houston Astros",
                record="75-73", next_event=None, last_result=None, stale=False)
    base.update(kwargs)
    return sports.TeamSummary(**base)


def _check_phrases():
    failed = 0

    def check(label, got, must_contain):
        nonlocal failed
        if must_contain not in got:
            failed += 1
            print(f"[FAIL] {label}: {got!r} missing {must_contain!r}")
            return
        spoken = clean_for_tts(got)
        if _DIGIT_RE.search(spoken):
            failed += 1
            print(f"[FAIL] {label}: digit survived clean_for_tts -> {spoken!r}")
        else:
            print(f"[ok] {label}: {got!r}")

    # Win
    ts = _ts(last_result=sports.LastResult("Tampa Bay Rays", 5, 2, True, "home"))
    check("win", sports.phrase_last_result(ts), "beat")

    # Loss
    ts = _ts(last_result=sports.LastResult("Tampa Bay Rays", 2, 5, False, "away"))
    check("loss", sports.phrase_last_result(ts), "lost")

    # MLS draw
    ts = _ts(league="mls", team_id="6077", display_name="Houston Dynamo FC",
             last_result=sports.LastResult("Chicago Fire FC", 1, 1, None, "home"))
    check("draw", sports.phrase_last_result(ts), "tied")

    # Record
    ts = _ts(record="75-73")
    check("record", sports.phrase_record(ts), "75-73")

    # No upcoming game (offseason / bye)
    ts = _ts(next_event=None)
    got = sports.phrase_next_game(ts)
    if "don't see an upcoming game" not in got:
        failed += 1
        print(f"[FAIL] no-next-game phrasing: {got!r}")
    else:
        print(f"[ok] no-next-game: {got!r}")

    # Next game with a real time
    ts = _ts(next_event=sports.NextEvent(
        "Tampa Bay Rays", datetime(2026, 9, 13, 22, 10, tzinfo=timezone.utc), "away"))
    got = sports.phrase_next_game(ts, now_utc=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc))
    check("next game", got, "Tampa Bay Rays")

    return failed


# ── F1: winner must come from `order`, never the (unreliable) `winner` field ─

def _f1_season(completed=True):
    return {
        "events": [
            {
                "name": "Spanish Grand Prix",
                "date": "2026-09-11T11:30Z",
                "competitions": [{
                    "status": {"type": {"completed": completed}},
                    "competitors": [
                        {"order": 2, "winner": False, "athlete": {"displayName": "Driver Two"}},
                        # `winner: False` here even though this is the actual
                        # P1 finisher — confirmed live ESPN behavior for F1.
                        {"order": 1, "winner": False, "athlete": {"displayName": "Driver One"}},
                    ],
                }],
            },
            {
                "name": "Bahrain Grand Prix",
                "date": "2026-11-01T15:00Z",
                "competitions": [{
                    "status": {"type": {"completed": False}},
                    "competitors": [],
                }],
            },
        ]
    }


def _check_f1():
    failed = 0
    season = _f1_season()
    got = sports.phrase_f1_last_result(season)
    if "Driver One" not in got or "Driver Two" in got:
        failed += 1
        print(f"[FAIL] F1 winner should come from order==1, got: {got!r}")
    else:
        print(f"[ok] F1 last result uses order, not winner field: {got!r}")

    got = sports.phrase_f1_next_race(season, now_utc=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc))
    if "Bahrain" not in got:
        failed += 1
        print(f"[FAIL] F1 next race: {got!r}")
    else:
        print(f"[ok] F1 next race: {got!r}")

    return failed


# ── Fetch-failure: never fall through to the LLM, which could hallucinate ──

class _FailingProvider:
    def team_summary(self, league, team_id):
        return None

    def scoreboard(self, league):
        return None

    def f1_season(self):
        return None


def _check_fetch_failure():
    failed = 0
    provider = _FailingProvider()
    got = sports.answer("did the astros win", provider)
    if not got or "trouble reaching" not in got:
        failed += 1
        print(f"[FAIL] team fetch failure should apologize, got: {got!r}")
    else:
        print(f"[ok] team fetch failure: {got!r}")

    got = sports.answer("who won the last f1 race", provider)
    if not got or "trouble reaching" not in got:
        failed += 1
        print(f"[FAIL] F1 fetch failure should apologize, got: {got!r}")
    else:
        print(f"[ok] F1 fetch failure: {got!r}")

    return failed


# ── classify() routing ──────────────────────────────────────────────────────

ROUTES_SPORTS = [
    "who won the astros game last night",
    "what's the score of the rockets game",
    "when is the next dynamo game",
    "did the texans win",
    "what's the astros record",
    "who won the packers game",
    "who won the last f1 race",
    "when's the next grand prix",
    "who won the football giants game",
    "who won the baseball giants game",
]

ROUTES_NOT_SPORTS = [
    ("I met a Texans fan yesterday", "llm"),   # team name, no sports verb
    ("what's 7 minus 12", "calc"),
]


def _check_routing():
    failed = 0
    for text in ROUTES_SPORTS:
        r = classify(text)
        if r != "sports":
            failed += 1
            print(f"[FAIL] {text!r} routed {r!r}, expected 'sports'")
        else:
            print(f"[ok] routes sports: {text!r}")

    for text, expected in ROUTES_NOT_SPORTS:
        r = classify(text)
        if r == "sports":
            failed += 1
            print(f"[FAIL] {text!r} routed 'sports', expected {expected!r}")
        else:
            print(f"[ok] not sports ({r}): {text!r}")

    return failed


def run():
    failed = 0
    failed += _check_resolve()
    failed += _check_phrases()
    failed += _check_f1()
    failed += _check_fetch_failure()
    failed += _check_routing()

    if failed:
        print(f"\n{failed} check(s) failed")
        sys.exit(1)
    print("\nAll sports checks passed")


if __name__ == "__main__":
    run()
