"""Runtime settings shared by antigua_core modules.

Each server (primary and fallback) calls configure(cfg) at startup with its
parsed server.yaml. Core modules read these attributes at call time (never at
import time), so configure() may run after imports.

mqtt_publish is a hook: the server assigns its own publisher so core modules
can emit events without importing server code. Default is a no-op, which is
also what the tests want — the routing suite runs with every service down.
"""

import copy
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("antigua_core")

# Repo root (server/antigua_core/ -> repo)
BASE_DIR = Path(__file__).parent.parent.parent
# Runtime state (memories, lists, timers, caches). ANTIGUA_DATA_DIR moves it
# all at once; the test suites point it at a temp dir so they never touch the
# live files.
DATA_DIR = Path(os.environ.get("ANTIGUA_DATA_DIR") or BASE_DIR / "data")


def local_or_example(path) -> Path:
    """Return path, or its tracked `.example` sibling when the local file is
    missing (a fresh clone). server.yaml -> server.example.yaml,
    system_prompt.txt -> system_prompt.example.txt. Real household values live
    only in the git-ignored local files."""
    path = Path(path)
    if path.exists():
        return path
    example = path.with_name(f"{path.stem}.example{path.suffix}")
    if example.exists():
        log.warning("%s not found — using %s", path.name, example.name)
        return example
    return path

# ── Weather ──────────────────────────────────────────────────────────────────
# Neutral placeholders (New York City); the real home point comes from
# server.yaml's weather: block.
DEFAULT_WEATHER_LOCATION = "10001"
WEATHER_TTL = 900
NWS_LAT = 40.713
NWS_LON = -74.006
NWS_ALERTS_TTL = 600
# Open-Meteo home point + spoken-answer tuning knobs.
WEATHER_HOME_LAT = 40.713
WEATHER_HOME_LON = -74.006
WEATHER_HOME_TZ = "America/New_York"
WEATHER_TIMEOUT = 4                 # fail fast rather than stall TTS
WEATHER_RAIN_LIKELY_PCT = 50       # >= this: "yes / likely"
WEATHER_RAIN_SLIGHT_PCT = 20       # >= this: "maybe / slight chance"
WEATHER_JACKET_BELOW_F = 58        # feels-like below this: suggest a jacket
WEATHER_NOTABLE_WIND_MPH = 20      # only mention wind unprompted above this
WEATHER_PREWARM_CITIES: list = []
WEATHER_SEVERE_ALERTS = True       # speak new Severe/Extreme NWS alerts unprompted

# ── Calc (currency conversion) ──────────────────────────────────────────────
CALC_CURRENCY_ENABLED = True
CALC_CURRENCY_TTL = 21600          # ECB publishes once per business day
CALC_CURRENCY_BASE = "USD"         # prewarmed table; other bases fetched on demand
CALC_CURRENCY_TIMEOUT = 4

# ── Sports scores ────────────────────────────────────────────────────────────
SPORTS_ENABLED = True
SPORTS_LIVE_TTL = 60               # scoreboard cache — live games move fast
SPORTS_TEAM_TTL = 300              # team record/schedule cache
SPORTS_F1_TTL = 300                # F1 scoreboard cache
SPORTS_TIMEOUT = 4                 # fail fast rather than stall TTS

# ── News ─────────────────────────────────────────────────────────────────────
NEWS_TTL = 1200
NEWS_MAX_TOKENS = 150
NEWS_SOURCES = []
NEWS_MAX_ITEMS = 5

# ── Search ───────────────────────────────────────────────────────────────────
SEARCH_ENABLED = False
SEARCH_URL = "http://localhost:8080"
SEARCH_RESULT_COUNT = 3
SEARCH_LIVE_RESULT_COUNT = 6
SEARCH_HOME_CITY = ""
SEARCH_CACHE_TTL = 900
SEARCH_ROUTER_ENABLED = True
SEARCH_ROUTER_TIMEOUT = 3
SEARCH_MAX_TOKENS_LONG = 220
SEARCH_MAX_TOKENS = 120
SEARCH_TIMEOUT = 4
SEARCH_ENGINES = "google,bing,duckduckgo"
SEARCH_NEWS_ENGINES = "google news,bing news,google,bing"

# ── Memory / conversation / timers ──────────────────────────────────────────
MEMORY_TTL_DAYS = 14
MEMORY_STORE_PATH = DATA_DIR / "memories.json"
# Household members, from server.yaml's household: block. Each is
# {"name": ..., "nicknames": [...], "misheard_as": [...], "say_as": ...}:
#   nicknames   — also accepted as this person ("kat" -> "Katherine")
#   misheard_as — STT outputs rewritten to the name before routing
#                 (case-sensitive, whole word: Whisper hears "Sean" as "Shawn")
#   say_as      — respelling for TTS when Kokoro mispronounces the name
# Memories are stored per member; with nobody configured Antigua still asks
# "who is this for?" but can't resolve an answer.
HOUSEHOLD: list = []
CONVERSATION_TTL = 300
MAX_HISTORY = 6
TIMER_STORE_PATH = DATA_DIR / "timers.json"
LISTS_STORE_PATH = DATA_DIR / "lists.json"
SPEAKER_PROFILES_PATH = DATA_DIR / "speaker_profiles.json"
SPEAKER_MIN_SIMILARITY = 0.75  # cosine similarity floor — see speaker_id.py

# ── Music (Music Assistant + Apple Music) ───────────────────────────────────
# Spoken speaker name → Music Assistant player name, from server.yaml's
# music.speakers. Keys double as the words music_intents.py listens for after
# "on/in the …".
MUSIC_SPEAKERS: dict = {}
MUSIC_DEFAULT_PLAYER = ""

# ── Assistant ────────────────────────────────────────────────────────────────
DISPLAY_ENABLED = False  # optional wall display; see server_common.DISPLAY_TOPICS
WAKE_WORDS = ["alexa"]
DISPLAY_COMMANDS = []
WAKE_PREFIX_RE = None  # built by configure() from WAKE_WORDS


# Pristine defaults, so configure() is idempotent: each call starts from these,
# not from whatever an earlier call left behind (tests configure repeatedly).
_DEFAULTS = copy.deepcopy({k: v for k, v in globals().items() if k.isupper()})


def mqtt_publish(topic, payload):  # replaced by the server at startup
    pass


def configure(cfg: dict, *, mqtt_publish_fn=None):
    """Populate settings from a parsed server.yaml. Mirrors the defaults the
    primary server has always used, so an absent key changes nothing."""
    g = globals()
    g.update(copy.deepcopy(_DEFAULTS))
    if mqtt_publish_fn is not None:
        g["mqtt_publish"] = mqtt_publish_fn

    w = cfg.get("weather", {})
    g["DEFAULT_WEATHER_LOCATION"] = w.get("location", DEFAULT_WEATHER_LOCATION)
    g["WEATHER_TTL"] = w.get("ttl_seconds", WEATHER_TTL)
    g["NWS_LAT"] = w.get("nws_lat", NWS_LAT)
    g["NWS_LON"] = w.get("nws_lon", NWS_LON)
    g["NWS_ALERTS_TTL"] = w.get("alerts_ttl_seconds", NWS_ALERTS_TTL)
    g["WEATHER_HOME_LAT"] = w.get("home_lat", w.get("nws_lat", WEATHER_HOME_LAT))
    g["WEATHER_HOME_LON"] = w.get("home_lon", w.get("nws_lon", WEATHER_HOME_LON))
    g["WEATHER_HOME_TZ"] = w.get("home_tz", WEATHER_HOME_TZ)
    g["WEATHER_TIMEOUT"] = w.get("timeout_seconds", WEATHER_TIMEOUT)
    g["WEATHER_PREWARM_CITIES"] = w.get("prewarm_cities", [])
    g["WEATHER_SEVERE_ALERTS"] = w.get("severe_alerts", WEATHER_SEVERE_ALERTS)
    _wp = w.get("phrasing", {})
    g["WEATHER_RAIN_LIKELY_PCT"] = _wp.get("rain_likely_pct", WEATHER_RAIN_LIKELY_PCT)
    g["WEATHER_RAIN_SLIGHT_PCT"] = _wp.get("rain_slight_pct", WEATHER_RAIN_SLIGHT_PCT)
    g["WEATHER_JACKET_BELOW_F"] = _wp.get("jacket_below_f", WEATHER_JACKET_BELOW_F)
    g["WEATHER_NOTABLE_WIND_MPH"] = _wp.get("notable_wind_mph", WEATHER_NOTABLE_WIND_MPH)

    cc = cfg.get("calc", {}).get("currency", {})
    g["CALC_CURRENCY_ENABLED"] = cc.get("enabled", CALC_CURRENCY_ENABLED)
    g["CALC_CURRENCY_TTL"] = cc.get("ttl_seconds", CALC_CURRENCY_TTL)
    g["CALC_CURRENCY_BASE"] = cc.get("base", CALC_CURRENCY_BASE)
    g["CALC_CURRENCY_TIMEOUT"] = cc.get("timeout_seconds", CALC_CURRENCY_TIMEOUT)

    sp = cfg.get("sports", {})
    g["SPORTS_ENABLED"] = sp.get("enabled", SPORTS_ENABLED)
    g["SPORTS_LIVE_TTL"] = sp.get("live_ttl_seconds", SPORTS_LIVE_TTL)
    g["SPORTS_TEAM_TTL"] = sp.get("team_ttl_seconds", SPORTS_TEAM_TTL)
    g["SPORTS_F1_TTL"] = sp.get("f1_ttl_seconds", SPORTS_F1_TTL)
    g["SPORTS_TIMEOUT"] = sp.get("timeout_seconds", SPORTS_TIMEOUT)

    n = cfg.get("news", {})
    g["NEWS_TTL"] = n.get("ttl_seconds", NEWS_TTL)
    g["NEWS_MAX_TOKENS"] = n.get("max_tokens_news", NEWS_MAX_TOKENS)
    g["NEWS_SOURCES"] = n.get("sources", NEWS_SOURCES)
    g["NEWS_MAX_ITEMS"] = n.get("max_headlines", NEWS_MAX_ITEMS)

    s = cfg.get("search", {})
    g["SEARCH_ENABLED"] = s.get("enabled", SEARCH_ENABLED)
    g["SEARCH_URL"] = s.get("url", SEARCH_URL)
    g["SEARCH_RESULT_COUNT"] = s.get("result_count", SEARCH_RESULT_COUNT)
    g["SEARCH_LIVE_RESULT_COUNT"] = s.get("live_result_count", SEARCH_LIVE_RESULT_COUNT)
    g["SEARCH_HOME_CITY"] = s.get("home_city", SEARCH_HOME_CITY)
    g["SEARCH_CACHE_TTL"] = s.get("cache_ttl_seconds", SEARCH_CACHE_TTL)
    g["SEARCH_ROUTER_ENABLED"] = s.get("router_enabled", SEARCH_ROUTER_ENABLED)
    g["SEARCH_ROUTER_TIMEOUT"] = s.get("router_timeout_seconds", SEARCH_ROUTER_TIMEOUT)
    g["SEARCH_MAX_TOKENS_LONG"] = s.get("max_tokens_search_long", SEARCH_MAX_TOKENS_LONG)
    g["SEARCH_MAX_TOKENS"] = s.get("max_tokens_search", SEARCH_MAX_TOKENS)
    g["SEARCH_TIMEOUT"] = s.get("timeout_seconds", SEARCH_TIMEOUT)
    g["SEARCH_ENGINES"] = s.get("engines", SEARCH_ENGINES)
    g["SEARCH_NEWS_ENGINES"] = s.get("news_engines", SEARCH_NEWS_ENGINES)

    m = cfg.get("memory", {})
    g["MEMORY_TTL_DAYS"] = m.get("ttl_days", MEMORY_TTL_DAYS)
    # storage_path is relative to the repo root; ANTIGUA_DATA_DIR overrides it.
    g["MEMORY_STORE_PATH"] = (
        DATA_DIR / "memories.json" if os.environ.get("ANTIGUA_DATA_DIR")
        else BASE_DIR / m.get("storage_path", "data/memories.json")
    )
    # household: replaces the older memory.users list of bare names.
    g["HOUSEHOLD"] = [
        {"name": p} if isinstance(p, str) else dict(p)
        for p in cfg.get("household", m.get("users", HOUSEHOLD))
    ]

    mu = cfg.get("music", {})
    if mu.get("speakers"):
        g["MUSIC_SPEAKERS"] = {k.lower(): v for k, v in mu["speakers"].items()}
    g["MUSIC_DEFAULT_PLAYER"] = mu.get("default_player", MUSIC_DEFAULT_PLAYER)

    g["DISPLAY_ENABLED"] = bool(cfg.get("display", {}).get("enabled", False))

    a = cfg.get("assistant", {})
    g["WAKE_WORDS"] = [x.lower() for x in a.get("wake_words", WAKE_WORDS)]
    g["DISPLAY_COMMANDS"] = a.get("display_commands", DISPLAY_COMMANDS)
    g["WAKE_PREFIX_RE"] = (
        re.compile(
            r"^(?:(?:" + "|".join(re.escape(w) for w in g["WAKE_WORDS"]) + r")[,.\s]*)+",
            re.IGNORECASE,
        )
        if g["WAKE_WORDS"] else None
    )
