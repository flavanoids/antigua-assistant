#!/usr/bin/env python3
"""Routing snapshot suite.

Asserts that every fixture utterance in fixtures/routing.yaml reaches the skill
it is supposed to reach. Not a correctness suite for the skills themselves —
purely a guard on which skill *claims* an utterance, because that is what
silently breaks when a new regex lands.

Runs without any Antigua service up: classify() is network-free.

    python3 tests/test_routing.py          # plain runner, no pytest needed
    pytest tests/test_routing.py           # also works
"""

import os
import sys
from collections import Counter
from pathlib import Path

import yaml

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "server"))

# Importing antigua_server reads its config, system prompt and
# data/memories.json. All three are read-only at import; nothing connects to
# MQTT, Ollama or the network until main() runs. The tracked example config
# keeps routing independent of this machine's server.yaml.
os.environ["ANTIGUA_CONFIG"] = str(REPO_ROOT / "server" / "config" / "server.example.yaml")
import antigua_server as srv  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "routing.yaml"


def _load():
    return yaml.safe_load(FIXTURES.read_text())


def load_cases():
    """Yield (utterance, expected_route) for the must-pass fixtures."""
    for route, utterances in _load()["routes"].items():
        for utterance in utterances:
            yield utterance, route


def load_known_gaps():
    """Yield (utterance, expected, actual, note) for recorded divergences."""
    for gap in _load().get("known_gaps") or []:
        yield gap["utterance"], gap["expected"], gap["actual"], gap.get("note", "")


def _check_routes():
    return [
        (utterance, expected, srv.classify(utterance))
        for utterance, expected in load_cases()
        if srv.classify(utterance) != expected
    ]


def _check_known_gaps():
    """Return gaps that no longer reproduce — i.e. someone fixed one."""
    fixed = []
    for utterance, expected, actual, _note in load_known_gaps():
        now = srv.classify(utterance)
        if now != actual:
            fixed.append((utterance, expected, actual, now))
    return fixed


def test_routes_are_known():
    """Every route named in the fixtures is one classify() can actually return."""
    data = _load()
    named = set(data["routes"])
    for gap in data.get("known_gaps") or []:
        named.add(gap["expected"])
        named.add(gap["actual"])
    unknown = named - set(srv.ROUTE_ORDER)
    assert not unknown, f"fixture routes not in ROUTE_ORDER: {sorted(unknown)}"


def test_routing():
    """Every fixture utterance reaches its expected skill."""
    failures = _check_routes()
    if failures:
        lines = [f"{len(failures)} utterance(s) routed to the wrong skill:", ""]
        for utterance, expected, actual in failures:
            lines.append(f"  {utterance!r}")
            lines.append(f"      expected {expected}  ->  got {actual}")
        raise AssertionError("\n".join(lines))


def test_known_gaps_still_reproduce():
    """A fixed gap must be promoted into `routes`, not left rotting here."""
    fixed = _check_known_gaps()
    if fixed:
        lines = [
            f"{len(fixed)} known gap(s) no longer reproduce — move them into "
            "`routes` in fixtures/routing.yaml:",
            "",
        ]
        for utterance, expected, was, now in fixed:
            lines.append(f"  {utterance!r}")
            lines.append(f"      recorded as {was}, now {now} (wanted {expected})")
        raise AssertionError("\n".join(lines))


def main():
    cases = list(load_cases())
    gaps = list(load_known_gaps())
    failures = _check_routes()
    fixed = _check_known_gaps()

    by_route = Counter(route for _, route in cases)
    uncovered = [r for r in srv.ROUTE_ORDER if r not in by_route]

    print(f"{len(cases)} fixtures across {len(by_route)} routes, {len(gaps)} known gaps")
    if uncovered:
        print(f"no fixtures for: {', '.join(uncovered)}")
    print()

    for utterance, expected, actual in failures:
        print(f"  FAIL {utterance!r}")
        print(f"       expected {expected}  ->  got {actual}")
    for utterance, expected, was, now in fixed:
        print(f"  FIXED {utterance!r} — was {was}, now {now} (wanted {expected})")
        print("       promote it into `routes`")

    if failures or fixed:
        print(f"\nFAIL — {len(failures)} misrouted, {len(fixed)} stale gap(s)")
        return 1

    print(f"PASS — {len(cases)} utterances routed correctly, {len(gaps)} gaps recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
