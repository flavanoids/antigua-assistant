# Contributing

Antigua is a personal project, but it's built to be read and changed.
[docs/architecture.md](docs/architecture.md) is the map, and
[docs/decisions.md](docs/decisions.md) explains the parts that look odd.

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r server/requirements.txt -r server/requirements-tts.txt
git config core.hooksPath .githooks
```

The tests need no network, GPU, models or local config. They run against
`server/config/server.example.yaml`.

## Before you commit

```bash
ruff check .
for t in tests/test_*.py; do venv/bin/python "$t" >/dev/null || echo "FAIL $t"; done
```

`pytest` works too: `tests/conftest.py` runs each suite as its own script
in a subprocess, which is also what CI (`.github/workflows/ci.yml`) does on
Python 3.11 and 3.13. `tests/dry_run_live.py` is different: it's a manual
check against running services, not part of the suite.

The pre-commit hook refuses:

- audio recordings, transcripts and logs (only the generated cues in `audio/`
  and `server/static_audio/` are allowed);
- added lines matching `.githooks/private-patterns`, a git-ignored list of
  your own names, addresses and paths, plus `ssh -i` lines and private keys.

## Ground rules

- **Actions are deterministic.** Anything that changes the world (timers, TV,
  lights, music, lists, memories) is parsed in `antigua_core/intents/` and executed in
  code. Don't hand these to the LLM.
- **Keep the tests off live data.** A new suite imports `_isolated` first (see
  any existing suite), so its stores write to a temp dir, not `data/`.
- **Every route has fixtures.** When you add or change a parser, add phrases
  to `tests/fixtures/routing.yaml`, including the neighboring skills' phrases
  it might steal. Order in `ROUTE_ORDER` matters.
- **Spoken text has no digits.** Skills phrase numbers with `num2words`;
  `clean_for_tts()` handles the rest. `tests/test_calc.py` shows the pattern.
- **Config has two homes.** A new key goes in `antigua_core/settings.py`
  (default plus `configure()`), in your local `server.yaml`, and in
  `server.example.yaml` with a generic value. Personal values never go in a
  tracked file.
- **Both servers share `antigua_core`.** If a change needs something only the
  primary has, pass it in through the `Backend` so the fallback degrades
  cleanly.
- **Keep the mic from hearing Antigua.** Don't reopen the mic on plain speech
  or add a Whisper prompt. See the echo-loop entries in `docs/decisions.md`.
- **Anything that stores audio or transcripts** needs the same 7-day
  retention: put it under `logs/` or the captures directory.
- **Match the surrounding code**: comment density, naming, and the "why"
  comments that record a measurement or an incident.

Adding a skill: see the template in
[ANTIGUA_SKILLS/README.md](ANTIGUA_SKILLS/README.md#template-for-a-new-skill).
Adding an MCP server: [server/mcp/README.md](server/mcp/README.md).

## Commits

Imperative, specific subject lines that say what changed for the person
talking to Antigua ("Tell timers apart: names, lengths, ordinals"). Record a
decision in `docs/decisions.md` when you make one, and add a line to
`CHANGELOG.md` under Unreleased.
