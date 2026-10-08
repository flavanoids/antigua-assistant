#!/usr/bin/env python3
"""Music resolution benchmark: how often does "play X" play the right X?

Runs spoken-style requests through the real route (classify), parser and
resolver against the live Music Assistant catalogue, with playback swapped
for a recorder, and scores what would have played against what was meant.
Nothing plays anywhere; no data/*.json is touched.

Every catalogue, Deezer and SearXNG response is cached in
data/music_bench_cache.json, so reruns are fast and a resolver change is
compared on the same search results. --refresh re-fetches everything.

Cases: tests/music_bench/cases.yaml (synthetic, committed), plus
data/music_bench_local.yaml when present (real requests from the logs —
local only, never committed). Each run is saved under data/music_bench_runs/
and compared with the previous one, listing cases that flipped.

Case format:
  - say: play so sick by ne-yo          # or a list: a multi-turn exchange,
    want: {type: track, name: So Sick, artist: Ne-Yo}   # scored on the last turn
    tags: [song, by-artist]
  want keys (all optional, all must hold):
    route     the classify() route (default "music")
    type      track | album | artist | playlist, or a list of them
              ("artist" also accepts a playlist or radio named after the artist)
    name      title (normalized, fuzzy); name_has: any of these words in it
    artist    must be among the credited artists (or the artist itself)
    ask       true: nothing plays and Antigua asks a question

Run: venv/bin/python tests/music_bench.py            # all cases
     venv/bin/python tests/music_bench.py -t song    # only cases tagged song
     venv/bin/python tests/music_bench.py -v         # show replies for failures
"""

import argparse
import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "server"))

from antigua_core import music as music_mod, settings  # noqa: E402
from antigua_core.classify import classify  # noqa: E402
from antigua_core.mcp_client import read_env_file  # noqa: E402
from antigua_core.music import MusicControl, _norm, _sim  # noqa: E402
from antigua_core.music_intents import parse_correction, parse_music  # noqa: E402

DATA = settings.DATA_DIR
CACHE_PATH = DATA / "music_bench_cache.json"
RUNS = DATA / "music_bench_runs"
CASE_FILES = [Path(__file__).parent / "music_bench" / "cases.yaml", DATA / "music_bench_local.yaml"]
# Music Assistant commands that only read; anything else is a write and is dropped.
_READS = ("music/search", "music/artists/", "music/albums/", "music/tracks/", "music/playlists/",
          "players/all", "players/get", "player_queues/get")


class Cache:
    def __init__(self, refresh: bool):
        self.refresh = refresh
        self.lock = threading.Lock()
        self.hits = self.misses = 0
        try:
            self.data = {} if refresh else json.loads(CACHE_PATH.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, key_obj, fetch):
        key = hashlib.sha1(json.dumps(key_obj, sort_keys=True, default=str).encode()).hexdigest()
        with self.lock:
            if key in self.data:
                self.hits += 1
                return self.data[key]
        value = fetch()
        with self.lock:
            self.misses += 1
            if not _is_error(value):        # a failure is retried next run, not frozen in
                self.data[key] = value
        return value

    def save(self):
        CACHE_PATH.write_text(json.dumps(self.data))

    def purge_errors(self):
        self.data = {k: v for k, v in self.data.items() if not _is_error(v)}


def _is_error(value) -> bool:
    return value is None or (isinstance(value, dict) and "error" in value)


# Deezer allows ~50 requests per 5s per IP; parallel cases would blow through
# that and get {"error": "Quota limit exceeded"} back.
_DEEZER_GAP = 0.15
_deezer_lock = threading.Lock()
_deezer_next = [0.0]


# iTunes search allows about 20 calls a minute.
_ITUNES_GAP = 3.1
_itunes_lock = threading.Lock()
_itunes_next = [0.0]


def _itunes_turn():
    with _itunes_lock:
        wait = _itunes_next[0] - time.time()
        _itunes_next[0] = max(time.time(), _itunes_next[0]) + _ITUNES_GAP
    if wait > 0:
        time.sleep(wait)


def _deezer_turn():
    with _deezer_lock:
        wait = _deezer_next[0] - time.time()
        _deezer_next[0] = max(time.time(), _deezer_next[0]) + _DEEZER_GAP
    if wait > 0:
        time.sleep(wait)



class _Resp:
    """Just enough of requests.Response for music.py's .json() calls."""
    def __init__(self, body):
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("cached failure")
        return self._body


def install_http_cache(cache: Cache):
    real_get = requests.get

    def get(url, params=None, **kw):
        def fetch():
            for attempt in range(4):
                if "deezer.com" in url:
                    _deezer_turn()
                elif "itunes.apple.com" in url:
                    _itunes_turn()
                try:
                    body = real_get(url, params=params, **kw).json()
                except Exception:
                    return None
                if not (isinstance(body, dict) and (body.get("error") or {}).get("code") == 4):
                    return body
                time.sleep(2 * (attempt + 1))       # quota: back off and retry
            return body
        return _Resp(cache.get(["GET", url, params], fetch))
    music_mod.requests.get = get


def make_control(cfg, token, cache: Cache):
    mc = MusicControl(cfg, token)
    mc._played = set()                      # no personal history: runs are comparable
    mc._played_path = DATA / "music_bench_played.json"   # never written (see _play below)
    mc._fans, mc._fans_path = {}, DATA / "music_bench_fans.json"   # via the HTTP cache instead
    mc._prefs, mc._prefs_path = {}, DATA / "music_bench_prefs.json"   # corrections stay per case
    mc.played = []                          # what would have played, in order
    real_cmd = mc.ma.cmd

    def cmd(command, timeout=None, **args):
        if not command.startswith(_READS):
            return None
        return cache.get(["MA", command, args], lambda: real_cmd(command, timeout=timeout, **args))
    mc.ma.cmd = cmd

    def play(player, media, *, radio=False, shuffle=False):
        mc.played.append({"media": media, "radio": radio, "player": player})
        mc._active = player
    mc._play = play
    return mc


def describe(media) -> dict:
    items = media if isinstance(media, list) else [media]
    first = items[0]
    return {"type": first.get("media_type") or "?", "name": first.get("name", ""),
            "artists": [a.get("name", "") for a in first.get("artists") or [] if a.get("name")],
            "count": len(items)}


def _in_artists(want: str, names: list[str]) -> bool:
    w = _norm(want).removeprefix("the ")
    return any(w and (w in _norm(n) or _sim(n, want) >= 0.85) for n in names)


def score(want: dict, route: str, got: dict | None, reply: str) -> tuple[bool, str]:
    """(passed, why-not)."""
    if route != want.get("route", "music"):
        return False, f"route {route}"
    if want.get("route", "music") != "music":
        return True, ""
    if want.get("ask"):
        return (got is None and reply.rstrip().endswith("?")), "played instead of asking"
    if got is None:
        return False, "nothing played"
    types = want.get("type")
    types = [types] if isinstance(types, str) else types or []
    if types:
        ok_type = got["type"] in types
        if not ok_type and "artist" in types and got["type"] == "playlist":
            # "Beyoncé Essentials" counts as playing the artist; a single
            # song by them doesn't, nor Apple's karaoke "<Artist>: Sing"
            ok_type = (_in_artists(want.get("artist", ""), [got["name"]])
                       and not re.search(r":\s*Sing$", got["name"]))
        if not ok_type:
            return False, f"type {got['type']}"
    if want.get("name") and _sim(got["name"], want["name"]) < 0.85 \
            and _norm(want["name"]) != _norm(got["name"]).removeprefix("the "):
        return False, "name"
    if want.get("name_has") and not any(_norm(w) in _norm(got["name"]) for w in want["name_has"]):
        return False, "name"
    if want.get("artist"):
        names = got["artists"] + ([got["name"]] if got["type"] in ("artist", "playlist") else [])
        if not _in_artists(want["artist"], names):
            return False, "artist"
    return True, ""


def run_case(case, cfg, token, cache) -> dict:
    turns = case["say"] if isinstance(case["say"], list) else [case["say"]]
    mc = make_control(cfg, token, cache)
    route, reply, t0 = "", "", time.time()
    for turn in turns:
        mc.played.clear()
        cor = parse_correction(turn)        # the pipeline checks this before classify()
        if cor and mc.correctable():
            route, reply = "music", mc.correct(cor)
            continue
        route = classify(turn)
        if route != "music":
            reply = ""
            continue
        try:
            reply = mc.handle(parse_music(turn))
        except Exception as e:           # a crash is a failed case, not a failed run
            reply = f"CRASH {type(e).__name__}: {e}"
    got = describe(mc.played[-1]["media"]) if mc.played else None
    ok, why = score(case["want"], route, got, reply)
    return {"say": case["say"], "tags": case.get("tags", []), "ok": ok, "why": why,
            "got": got, "reply": reply, "route": route, "secs": round(time.time() - t0, 2)}


def load_cases(tags):
    cases = []
    for path in CASE_FILES:
        if path.exists():
            for c in yaml.safe_load(path.read_text()) or []:
                c.setdefault("tags", [])
                if path.parent == DATA:
                    c["tags"] = [*c["tags"], "local"]
                cases.append(c)
    if tags:
        cases = [c for c in cases if set(tags) & set(c["tags"])]
    return cases


def fmt_got(r):
    g = r["got"]
    if not g:
        return f'(nothing) "{r["reply"]}"' if r["route"] == "music" else f"(route {r['route']})"
    by = f" — {', '.join(g['artists'])}" if g["artists"] else ""
    more = f" +{g['count'] - 1}" if g["count"] > 1 else ""
    return f"{g['name']}{by} [{g['type']}{more}]"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-t", "--tag", action="append", help="only cases with this tag (repeatable)")
    ap.add_argument("--refresh", action="store_true", help="ignore the response cache")
    ap.add_argument("-v", "--verbose", action="store_true", help="print replies for failures")
    ap.add_argument("-j", "--jobs", type=int, default=6)
    args = ap.parse_args()

    cfg_path = settings.local_or_example(ROOT / "server" / "config" / "server.yaml")
    cfg = yaml.safe_load(cfg_path.read_text())
    settings.configure(cfg)
    token = read_env_file(ROOT / cfg.get("mcp", {}).get("env_file", "server/config/mcp.env")).get(
        "MUSIC_ASSISTANT_TOKEN")
    if not token:
        sys.exit("No MUSIC_ASSISTANT_TOKEN in the mcp env file.")

    cases = load_cases(args.tag)
    cache = Cache(args.refresh)
    cache.purge_errors()
    install_http_cache(cache)
    t0 = time.time()
    with ThreadPoolExecutor(args.jobs) as ex:
        results = list(ex.map(lambda c: run_case(c, cfg, token, cache), cases))
    cache.save()

    for r in results:
        say = " → ".join(r["say"]) if isinstance(r["say"], list) else r["say"]
        mark = "✓" if r["ok"] else "✗"
        print(f"{mark} {say:<52} {fmt_got(r)}" + ("" if r["ok"] else f"   [{r['why']}]"))
        if args.verbose and not r["ok"] and r["got"]:
            print(f"      reply: {r['reply']}")

    by_tag: dict[str, list[bool]] = {}
    for r in results:
        for tag in r["tags"] or ["untagged"]:
            by_tag.setdefault(tag, []).append(r["ok"])
    print("\nBy tag:")
    for tag, oks in sorted(by_tag.items()):
        print(f"  {tag:<14} {sum(oks):>3}/{len(oks):<3} {100 * sum(oks) / len(oks):5.1f}%")
    passed = sum(r["ok"] for r in results)
    print(f"\nTotal: {passed}/{len(results)} ({100 * passed / max(len(results), 1):.1f}%)  "
          f"in {time.time() - t0:.1f}s, cache {cache.hits} hits / {cache.misses} fetched")

    RUNS.mkdir(parents=True, exist_ok=True)
    previous = sorted(RUNS.glob("*.json"))
    if previous and not args.tag:
        before = {json.dumps(r["say"]): r["ok"] for r in json.loads(previous[-1].read_text())}
        flips = [r for r in results
                 if (k := json.dumps(r["say"])) in before and before[k] != r["ok"]]
        if flips:
            print(f"\nChanged since {previous[-1].stem}:")
            for r in flips:
                print(f"  {'fixed' if r['ok'] else 'BROKE'}: {r['say']} → {fmt_got(r)}")
    if not args.tag:
        (RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
