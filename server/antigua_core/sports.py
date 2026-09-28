"""Sports scores skill: ESPN scoreboard/team data + deterministic spoken
answers for NFL, MLB, NBA, MLS, and F1.

Same shape as weather.py: a pure/offline resolver (`resolve_team`) plus an
ordered intent-pattern list feed pure `phrase_*()` functions, so an answer
can never hallucinate a score. The network lives entirely in the cache
classes below (serve-stale-refresh-in-background, same pattern as
weather.WeatherProvider and caches.NewsCache); everything else is
unit-testable with synthetic data.

Team data (sports_teams.TEAMS) is generated from ESPN's live /teams endpoint,
not hand-typed — see server/scripts/build_sports_teams.py.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from . import settings
from .sports_teams import COLLISION_DEFAULTS, QUALIFIER_WORDS, TEAMS

log = logging.getLogger("antigua_core")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
LEAGUE_PATHS = {
    "mlb": "baseball/mlb",
    "nfl": "football/nfl",
    "nba": "basketball/nba",
    "mls": "soccer/usa.1",
}
LEAGUE_SPOKEN = {
    "mlb": "MLB",
    "nfl": "NFL",
    "nba": "NBA",
    "mls": "MLS",
}


# ── Team resolution (pure/offline — safe to call from classify.py) ─────────


@dataclass
class ResolveResult:
    league: str
    team_id: str
    display_name: str


def _build_alias_index() -> dict[str, list[tuple[str, str, str]]]:
    index: dict[str, list[tuple[str, str, str]]] = {}
    for (league, team_id), info in TEAMS.items():
        for alias in info["aliases"]:
            index.setdefault(alias, []).append((league, team_id, info["display"]))
    return index


_ALIAS_INDEX = _build_alias_index()
# Longest alias first, so "san francisco giants" is tried before bare "giants".
_ALIASES_BY_LENGTH = sorted(_ALIAS_INDEX, key=len, reverse=True)


def resolve_team(text: str) -> ResolveResult | None:
    """Word-boundary alias lookup, longest alias wins on overlap. Pure/offline
    — no network — so classify.py's gate can call this directly."""
    low = text.lower()
    for alias in _ALIASES_BY_LENGTH:
        if re.search(rf"\b{re.escape(alias)}\b", low):
            candidates = _ALIAS_INDEX[alias]
            if len(candidates) == 1:
                league, team_id, display = candidates[0]
                return ResolveResult(league, team_id, display)
            # Ambiguous alias (e.g. "giants") — a qualifier word picks the league;
            # otherwise fall back to the documented default (sports_teams.py).
            for league, words in QUALIFIER_WORDS.items():
                if any(re.search(rf"\b{w}\b", low) for w in words):
                    for c_league, team_id, display in candidates:
                        if c_league == league:
                            return ResolveResult(c_league, team_id, display)
            default_league = COLLISION_DEFAULTS.get(alias)
            for c_league, team_id, display in candidates:
                if c_league == default_league:
                    return ResolveResult(c_league, team_id, display)
            return ResolveResult(*candidates[0])
    return None


_F1_RE = re.compile(
    r"\b(f1|formula\s*1|formula\s*one|grand\s+prix)\b", re.IGNORECASE
)


def is_f1_query(text: str) -> bool:
    return bool(_F1_RE.search(text))


_SPORTS_VERB_RE = re.compile(
    r"\b(score|scores|scoring|won|win|wins|winning|lost|lose|losing|beat|"
    r"playing|play|plays|game|match|race|record|schedule|standings?|next|"
    r"how\s+(?:are|is|did))\b",
    re.IGNORECASE,
)


def is_sports_query(text: str) -> bool:
    if not settings.SPORTS_ENABLED:
        return False
    if is_f1_query(text) and _SPORTS_VERB_RE.search(text):
        return True
    if resolve_team(text) is not None and _SPORTS_VERB_RE.search(text):
        return True
    return False


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass
class NextEvent:
    opponent: str
    when_utc: datetime
    home_away: str


@dataclass
class LastResult:
    opponent: str
    team_score: int
    opp_score: int
    won: bool | None  # None only for a soccer draw
    home_away: str


@dataclass
class TeamSummary:
    league: str
    team_id: str
    display_name: str
    record: str | None
    next_event: NextEvent | None
    last_result: LastResult | None
    stale: bool = False


# ── Fetch helpers ────────────────────────────────────────────────────────────


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=settings.SPORTS_TIMEOUT) as r:
        return json.loads(r.read())


def _score_int(raw) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        v = raw.get("value")
        return int(v) if v is not None else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _competitor_matches(competitor: dict, league: str, team_id: str) -> bool:
    t = competitor.get("team", {})
    if league == "mls":
        return str(t.get("id")) == team_id
    return (t.get("abbreviation") or "").lower() == team_id


def _parse_next_event(team_json: dict, league: str, team_id: str) -> NextEvent | None:
    events = team_json.get("team", {}).get("nextEvent") or []
    if not events:
        return None
    ev = events[0]
    comp = ev["competitions"][0]
    opponent = None
    home_away = "home"
    for c in comp["competitors"]:
        if _competitor_matches(c, league, team_id):
            home_away = c.get("homeAway", "home")
        else:
            opponent = c["team"]["displayName"]
    if opponent is None:
        return None
    when_utc = datetime.strptime(ev["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    return NextEvent(opponent=opponent, when_utc=when_utc, home_away=home_away)


def _parse_record(team_json: dict) -> str | None:
    items = team_json.get("team", {}).get("record", {}).get("items", [])
    for item in items:
        if item.get("type") == "total":
            return item.get("summary")
    return items[0].get("summary") if items else None


def _parse_last_result(schedule_json: dict, league: str, team_id: str) -> LastResult | None:
    events = schedule_json.get("events", [])
    for ev in reversed(events):
        comp = ev["competitions"][0]
        if not comp["status"]["type"].get("completed"):
            continue
        us = opp = None
        for c in comp["competitors"]:
            if _competitor_matches(c, league, team_id):
                us = c
            else:
                opp = c
        if us is None or opp is None:
            continue
        team_score = _score_int(us.get("score"))
        opp_score = _score_int(opp.get("score"))
        if team_score is None or opp_score is None:
            continue
        if team_score == opp_score:
            won = None  # draw (MLS)
        else:
            won = team_score > opp_score
        return LastResult(
            opponent=opp["team"]["displayName"],
            team_score=team_score,
            opp_score=opp_score,
            won=won,
            home_away=us.get("homeAway", "home"),
        )
    return None


# ── Caches (serve-stale-refresh-in-background, same pattern as weather.py) ──


class _TeamCache:
    _FAIL_TTL = 30

    def __init__(self):
        self._cache: dict[tuple[str, str], tuple[TeamSummary, float]] = {}
        self._lock = threading.Lock()

    def get(self, league: str, team_id: str) -> TeamSummary | None:
        key = (league, team_id)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
        ttl = settings.SPORTS_TEAM_TTL
        if hit:
            summary, at = hit
            if now - at >= ttl:
                threading.Thread(target=self._refresh, args=(league, team_id), daemon=True).start()
                if summary is not None:
                    summary.stale = now - at >= ttl * 4
            return summary
        return self._refresh(league, team_id)

    def _refresh(self, league: str, team_id: str) -> TeamSummary | None:
        key = (league, team_id)
        path = LEAGUE_PATHS[league]
        try:
            team_json = _get_json(f"{ESPN_BASE}/{path}/teams/{team_id}")
            schedule_json = _get_json(f"{ESPN_BASE}/{path}/teams/{team_id}/schedule")
            display = team_json.get("team", {}).get("displayName", team_id)
            summary = TeamSummary(
                league=league,
                team_id=team_id,
                display_name=display,
                record=_parse_record(team_json),
                next_event=_parse_next_event(team_json, league, team_id),
                last_result=_parse_last_result(schedule_json, league, team_id),
            )
            with self._lock:
                self._cache[key] = (summary, time.time())
            return summary
        except Exception as e:
            log.warning("sports team fetch failed for %s/%s: %s", league, team_id, e)
            with self._lock:
                if key in self._cache:
                    summary = self._cache[key][0]
                    if summary is not None:
                        summary.stale = True
                    return summary
                self._cache[key] = (None, time.time() - settings.SPORTS_TEAM_TTL + self._FAIL_TTL)
            return None


class _ScoreboardCache:
    _FAIL_TTL = 20

    def __init__(self):
        self._cache: dict[str, tuple[dict, float]] = {}
        self._lock = threading.Lock()

    def get(self, league: str) -> dict | None:
        now = time.time()
        with self._lock:
            hit = self._cache.get(league)
        ttl = settings.SPORTS_LIVE_TTL
        if hit:
            data, at = hit
            if now - at >= ttl:
                threading.Thread(target=self._refresh, args=(league,), daemon=True).start()
            return data
        return self._refresh(league)

    def _refresh(self, league: str) -> dict | None:
        try:
            data = _get_json(f"{ESPN_BASE}/{LEAGUE_PATHS[league]}/scoreboard")
            with self._lock:
                self._cache[league] = (data, time.time())
            return data
        except Exception as e:
            log.warning("sports scoreboard fetch failed for %s: %s", league, e)
            with self._lock:
                if league in self._cache:
                    return self._cache[league][0]
                self._cache[league] = (None, time.time() - settings.SPORTS_LIVE_TTL + self._FAIL_TTL)
            return None


class _F1Cache:
    """Full-season schedule (one fetch covers last race + next race)."""

    _FAIL_TTL = 60

    def __init__(self):
        self._data: dict | None = None
        self._at: float = 0.0
        self._lock = threading.Lock()

    def get(self) -> dict | None:
        now = time.time()
        with self._lock:
            data, at = self._data, self._at
        ttl = settings.SPORTS_F1_TTL
        if data is not None and now - at < ttl:
            return data
        if data is not None:
            threading.Thread(target=self._refresh, daemon=True).start()
            return data
        return self._refresh()

    def _refresh(self) -> dict | None:
        try:
            year = date.today().year
            data = _get_json(f"{ESPN_BASE}/racing/f1/scoreboard?dates={year}")
            with self._lock:
                self._data = data
                self._at = time.time()
            return data
        except Exception as e:
            log.warning("F1 scoreboard fetch failed: %s", e)
            with self._lock:
                if self._data is not None:
                    return self._data
                self._at = time.time() - settings.SPORTS_F1_TTL + self._FAIL_TTL
            return None


class SportsProvider:
    """Facade wrapping the three caches — one instance per server process."""

    def __init__(self):
        self.teams = _TeamCache()
        self.scoreboards = _ScoreboardCache()
        self.f1 = _F1Cache()

    def team_summary(self, league: str, team_id: str) -> TeamSummary | None:
        return self.teams.get(league, team_id)

    def scoreboard(self, league: str) -> dict | None:
        return self.scoreboards.get(league)

    def f1_season(self) -> dict | None:
        return self.f1.get()


# ── Intent resolution ────────────────────────────────────────────────────────

_INTENT_PATTERNS = [
    ("next_game", re.compile(
        r"\bwhen\s+(?:is|do|does)\b.{0,30}?\b(?:play|next)\b|\bnext\s+(?:game|match|race)\b",
        re.IGNORECASE,
    )),
    ("last_result", re.compile(
        r"\b(?:who\s+)?(?:won|lost|beat)\b|\bhow\s+did\b.{0,30}?\bdo\b|"
        r"\blast\s+(?:game|match|race|result)\b",
        re.IGNORECASE,
    )),
    ("record", re.compile(
        r"\brecord\b|\bwin[\s-]?loss\b|\bhow\s+many\s+wins\b", re.IGNORECASE,
    )),
]


def _classify_intent(text: str) -> str:
    for name, pattern in _INTENT_PATTERNS:
        if pattern.search(text):
            return name
    return "live_score"


# ── Phrase functions (pure, unit-testable with synthetic data) ─────────────


def _local_day_label(when_local: datetime, now_local: datetime) -> str:
    delta = (when_local.date() - now_local.date()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return when_local.strftime("%A")


def _to_home_local(when_utc: datetime) -> datetime:
    return when_utc.astimezone(ZoneInfo(settings.WEATHER_HOME_TZ))


def phrase_next_game(ts: TeamSummary, now_utc: datetime | None = None) -> str:
    if ts.next_event is None:
        return f"I don't see an upcoming game scheduled for the {ts.display_name} right now."
    now_utc = now_utc or datetime.now(timezone.utc)
    local = _to_home_local(ts.next_event.when_utc)
    now_local = _to_home_local(now_utc)
    day = _local_day_label(local, now_local)
    time_str = local.strftime("%-I:%M %p")
    vs = "at" if ts.next_event.home_away == "away" else "against"
    return f"The {ts.display_name} play {vs} the {ts.next_event.opponent} {day} at {time_str}."


def phrase_last_result(ts: TeamSummary) -> str:
    lr = ts.last_result
    if lr is None:
        return f"I don't have a recent result for the {ts.display_name}."
    if lr.won is None:
        return f"The {ts.display_name} tied the {lr.opponent} {lr.team_score}-{lr.opp_score}."
    if lr.won:
        return f"The {ts.display_name} beat the {lr.opponent} {lr.team_score}-{lr.opp_score}."
    return f"The {ts.display_name} lost to the {lr.opponent} {lr.opp_score}-{lr.team_score}."


def phrase_record(ts: TeamSummary) -> str:
    if not ts.record:
        return f"I don't have a record for the {ts.display_name} right now."
    return f"The {ts.display_name} are {ts.record} this season."


def _find_live_event(scoreboard: dict | None, league: str, team_id: str) -> dict | None:
    if not scoreboard:
        return None
    for ev in scoreboard.get("events", []):
        comp = ev["competitions"][0]
        if any(_competitor_matches(c, league, team_id) for c in comp["competitors"]):
            return ev
    return None


def phrase_live_score(ts: TeamSummary, event: dict | None) -> str:
    if event is None:
        # No game today — fall back to the most recent/next thing to report.
        if ts.last_result is not None:
            return phrase_last_result(ts)
        return phrase_next_game(ts)

    comp = event["competitions"][0]
    state = comp["status"]["type"]["state"]
    us = opp = None
    for c in comp["competitors"]:
        if _competitor_matches(c, ts.league, ts.team_id):
            us = c
        else:
            opp = c
    opp_name = opp["team"]["displayName"] if opp else "their opponent"

    if state == "pre":
        return f"The {ts.display_name} haven't started yet — they play the {opp_name} today."

    us_score = _score_int(us.get("score")) if us else None
    opp_score = _score_int(opp.get("score")) if opp else None
    if us_score is None or opp_score is None:
        return f"The {ts.display_name} are playing the {opp_name} right now, but I don't have a score yet."

    if state == "in":
        if us_score == opp_score:
            return f"The {ts.display_name} are tied with the {opp_name}, {us_score}-{opp_score}."
        lead = "ahead of" if us_score > opp_score else "behind"
        return f"The {ts.display_name} are {lead} the {opp_name}, {us_score}-{opp_score}."

    # state == "post" — game just finished today
    if us_score == opp_score:
        return f"The {ts.display_name} tied the {opp_name} {us_score}-{opp_score}."
    if us_score > opp_score:
        return f"The {ts.display_name} beat the {opp_name} {us_score}-{opp_score}."
    return f"The {ts.display_name} lost to the {opp_name} {opp_score}-{us_score}."


def _f1_events(season: dict) -> list[dict]:
    return season.get("events", [])


def phrase_f1_last_result(season: dict) -> str:
    completed = [e for e in _f1_events(season) if e["competitions"][0]["status"]["type"].get("completed")]
    if not completed:
        return "There hasn't been an F1 race yet this season."
    race = completed[-1]
    comp = race["competitions"][0]
    competitors = comp.get("competitors", [])
    if not competitors:
        return f"I don't have a result for the {race['name']} yet."
    # F1's `winner` field is unreliable (observed false even for P1) — always
    # derive the winner from finishing `order`, never `winner`.
    winner = min(competitors, key=lambda c: c.get("order", 999))
    driver = winner.get("athlete", {}).get("displayName", "the winner")
    return f"{driver} won the {race['name']}."


def phrase_f1_next_race(season: dict, now_utc: datetime | None = None) -> str:
    now_utc = now_utc or datetime.now(timezone.utc)
    upcoming = [
        e for e in _f1_events(season)
        if not e["competitions"][0]["status"]["type"].get("completed")
    ]
    if not upcoming:
        return "I don't see another F1 race scheduled this season."
    race = sorted(upcoming, key=lambda e: e["date"])[0]
    when_utc = datetime.strptime(race["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    local = _to_home_local(when_utc)
    now_local = _to_home_local(now_utc)
    day = _local_day_label(local, now_local)
    return f"The next race is the {race['name']}, {day} at {local.strftime('%-I:%M %p')}."


# ── Entry point ──────────────────────────────────────────────────────────────


def answer(text: str, provider: SportsProvider, *, now: datetime | None = None) -> str | None:
    """Deterministic sports answer, or None if this isn't a sports turn."""
    now = now or datetime.now(timezone.utc)

    if is_f1_query(text):
        season = provider.f1_season()
        if season is None:
            return "I'm having trouble reaching F1 results right now. Try again in a minute."
        intent = _classify_intent(text)
        if intent == "next_game":
            return phrase_f1_next_race(season, now)
        return phrase_f1_last_result(season)

    resolved = resolve_team(text)
    if resolved is None:
        return None

    ts = provider.team_summary(resolved.league, resolved.team_id)
    if ts is None:
        return (f"I'm having trouble reaching {LEAGUE_SPOKEN[resolved.league]} scores "
                f"right now. Try again in a minute.")

    intent = _classify_intent(text)
    if intent == "next_game":
        return phrase_next_game(ts, now)
    if intent == "last_result":
        return phrase_last_result(ts)
    if intent == "record":
        return phrase_record(ts)

    scoreboard = provider.scoreboard(resolved.league)
    event = _find_live_event(scoreboard, resolved.league, resolved.team_id)
    return phrase_live_score(ts, event)
