#!/usr/bin/env python3
"""Summarize the TURN lines (antigua_core/turn_timing.py) in Antigua's logs:
turns, median and 90th-percentile first audio and total, the median of every
stage and number on the lines, and how often the waiting cue fired.

  turn_stats.py logs/server.log logs/server.log.1          # the primary
  ssh <backup> 'cat <FALLBACK_DIR>/logs/fallback.log' | turn_stats.py -
  turn_stats.py --by-route logs/server.log                 # one row per route

Chat = route llm; skills = every other route. .gz files are read as is.
"""

import argparse
import gzip
import re
import statistics
import sys
from collections import defaultdict

LINE = re.compile(r"TURN route=(\S+) total=([\d.]+)s first_audio=([\d.]+)s(?: \| (.*))?")


def _lines(paths):
    for p in paths:
        if p == "-":
            yield from sys.stdin
        else:
            with (gzip.open(p, "rt") if p.endswith(".gz") else open(p)) as f:
                yield from f


def _p90(xs):
    return sorted(xs)[min(len(xs) - 1, int(0.9 * len(xs)))]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("logs", nargs="+", help="log files, or - for stdin")
    ap.add_argument("--by-route", action="store_true", help="one row per route, not chat/skills")
    args = ap.parse_args()

    groups = defaultdict(list)
    for line in _lines(args.logs):
        m = LINE.search(line)
        if not m:
            continue
        route, total, first, rest = m.group(1), float(m.group(2)), float(m.group(3)), m.group(4) or ""
        fields = {}
        for kv in rest.split():
            k, _, v = kv.partition("=")
            try:
                fields[k] = float(v)
            except ValueError:
                pass   # host=noedeb02 and the like
        key = route if args.by_route else ("chat" if route == "llm" else "skills")
        groups[key].append((total, first, fields))

    if not groups:
        sys.exit("no TURN lines found")
    for key in sorted(groups, key=lambda k: -len(groups[k])):
        turns = groups[key]
        firsts = [t[1] for t in turns]
        totals = [t[0] for t in turns]
        cues = sum("cue" in t[2] for t in turns)
        print(f"{key}: {len(turns)} turns | first_audio median {statistics.median(firsts):.2f}s "
              f"p90 {_p90(firsts):.2f}s | total median {statistics.median(totals):.2f}s "
              f"p90 {_p90(totals):.2f}s | cue {cues}/{len(turns)}")
        stages = defaultdict(list)
        for _, _, fields in turns:
            for k, v in fields.items():
                stages[k].append(v)
        if stages:
            print("  medians: " + " ".join(
                f"{k}={statistics.median(v):.2f}(n{len(v)})" for k, v in sorted(stages.items())))


if __name__ == "__main__":
    main()
