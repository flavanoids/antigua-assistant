#!/usr/bin/env python3
"""One-off generator for server/antigua_core/sports_teams.py's TEAMS table.

Hits ESPN's live /teams endpoint for each league and prints a Python literal
to stdout. Run manually, then hand-merge the output into sports_teams.py —
don't overwrite it wholesale, since that file also carries hand-added
colloquial aliases (EXTRA below only seeds the ones known at generation
time). Re-run this if a franchise relocates, renames, or MLS expands.

Usage: python3 server/scripts/build_sports_teams.py > /tmp/teams_draft.py
"""

import json
import urllib.request

LEAGUE_PATHS = {
    "mlb": "baseball/mlb",
    "nfl": "football/nfl",
    "nba": "basketball/nba",
    "mls": "soccer/usa.1",
}

# Colloquial aliases ESPN's team fields don't supply. Extend this as new
# gaps are found; re-running the script preserves nothing else by itself.
EXTRA_ALIASES = {
    ("mlb", "ari"): ["d-backs", "dbacks"],
    ("mlb", "ath"): ["a's", "as", "oakland athletics"],
    ("mlb", "sf"): ["giants"],
    ("mlb", "stl"): ["cardinals"],
    ("nfl", "sf"): ["niners", "49ers"],
    ("nfl", "nyg"): ["giants"],
    ("nfl", "ari"): ["cardinals"],
    ("mls", "9720"): ["impact", "montreal"],
    ("mls", "193"): ["dc united"],
    ("mls", "17362"): ["minnesota united"],
    ("mls", "17606"): ["nycfc", "new york city"],
    ("mls", "186"): ["sporting kc", "sporting kansas city"],
    ("mls", "21812"): ["st louis city", "st. louis city"],
    ("mls", "190"): ["ny red bulls", "red bull ny"],
    ("mls", "189"): ["revs"],
    ("mls", "191"): ["quakes"],
}


def fetch_teams(league_path: str) -> list[dict]:
    url = f"https://site.api.espn.com/apis/site/v2/sports/{league_path}/teams?limit=50"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    return data["sports"][0]["leagues"][0]["teams"]


def norm(s: str) -> str:
    return s.lower().strip()


def build() -> str:
    lines = ["TEAMS: dict[tuple[str, str], dict] = {"]
    for league, path in LEAGUE_PATHS.items():
        teams = fetch_teams(path)
        lines.append(f"    # -- {league.upper()} ({len(teams)} teams) " + "-" * 30)
        for t in teams:
            team = t["team"]
            display = team["displayName"]
            location = team.get("location", "")
            aliases = {norm(display)}
            if league == "mls":
                team_id = team["id"]
                nickname = team.get("nickname") or team.get("shortDisplayName") or ""
                nn = norm(nickname)
                if nn and nn != norm(display) and nn != norm(location):
                    aliases.add(nn)
            else:
                team_id = team["abbreviation"].lower()
                nickname = team.get("name") or ""
                nn = norm(nickname)
                if nn:
                    aliases.add(nn)
                    combo = norm(f"{location} {nickname}")
                    if combo != nn:
                        aliases.add(combo)
            for a in EXTRA_ALIASES.get((league, team_id), []):
                aliases.add(norm(a))
            aliases.discard("")
            lines.append(
                f'    ("{league}", "{team_id}"): '
                f'{{"display": {display!r}, "aliases": {sorted(aliases)!r}}},'
            )
    lines.append("}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(build())
