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

# ── Commute (drive times from home; antigua_core/commute.py) ────────────────
COMMUTE_ENABLED = True
# Home + favorites (people's houses, work). Git-ignored and chmod 600 — the
# tracked places.example.yaml is only a template and is never read.
COMMUTE_PLACES_PATH = BASE_DIR / "server" / "config" / "places.yaml"
COMMUTE_PHOTON_URL = "https://photon.komoot.io"
COMMUTE_OSRM_URL = "https://router.project-osrm.org"   # used when there's no TomTom key
COMMUTE_TRANSTAR = True            # Houston TranStar speeds + incidents on the route
COMMUTE_SEARCH_RADIUS_KM = 35      # businesses farther than this aren't "near you"
COMMUTE_UNUSUAL_MIN_MINUTES = 5    # slower than usual by at least this much...
COMMUTE_UNUSUAL_PCT = 20           # ...and by at least this share of the usual time
COMMUTE_TIMEOUT = 5
COMMUTE_AREA_RADIUS_KM = 12        # "how's traffic" covers this far around home
COMMUTE_ROAD_ALIASES: dict = {}    # spoken -> OSM road name, e.g. {"the beltway": "sam houston"}

# ── Knowledge (people, history, events — Wikipedia + Wikidata) ─────────────
KNOWLEDGE_ENABLED = True
KNOWLEDGE_TIMEOUT = 4              # fail fast rather than stall TTS
KNOWLEDGE_TOPIC_TTL = 900          # follow-ups ("was she married?") within 15 min
KNOWLEDGE_MAX_TOKENS = 220         # overview budget
KNOWLEDGE_FOLLOWUP_MAX_TOKENS = 130
KNOWLEDGE_OVERVIEW_SENTENCES = "3 to 4"
KNOWLEDGE_LEAD_CHARS = 1800        # article lead given to the overview
KNOWLEDGE_PASSAGE_CHARS = 700      # per passage given to a follow-up
KNOWLEDGE_CONTEXT_CHARS = 2400     # all passages for one follow-up
KNOWLEDGE_FACTS_WAIT = 2.0         # how long a follow-up waits on Wikidata

# ── Recipes (real recipes from the web, read step by step) ────────────────────
RECIPE_ENABLED = True              # also needs search (SearXNG) enabled
RECIPE_CANDIDATES = 10             # result pages fetched per dish
RECIPE_FETCH_TIMEOUT = 5           # per page; all pages are fetched in parallel
RECIPE_CACHE_DAYS = 14             # parsed recipes kept per dish
RECIPE_SESSION_TTL = 4 * 3600      # a cooking session survives a long bake
RECIPE_ANSWER_TTL = 600            # bare "yes"/"no" answers a question this recent
RECIPE_QA_MAX_TOKENS = 110         # grounded answer to a free-form cooking question

# ── News ─────────────────────────────────────────────────────────────────────
NEWS_TTL = 1200
NEWS_MAX_TOKENS = 150
NEWS_SOURCES = []
NEWS_MAX_ITEMS = 5

# ── Persona ──────────────────────────────────────────────────────────────────
PERSONA_SASS = 2   # 0 warm .. 3 sharp; how much edge casual chat gets (persona.py)

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
# No Bing for web results: through SearXNG it answers with unrelated pages
# ("fix a running toilet" -> Microsoft account pages; 2026-10-07, on both
# SearXNG 2026.8.13 and 2026.10.7). Bing News is fine. SearXNG skips an
# engine while it's suspended (DuckDuckGo's CAPTCHA, Brave's rate limit).
SEARCH_ENGINES = "google,duckduckgo,brave"
SEARCH_NEWS_ENGINES = "google news,bing news,duckduckgo news,google"
# Fallback for when SearXNG comes back empty: its one working web engine
# scrapes Google from the home IP and gets rate-limited. Parallel's Search
# API (https://docs.parallel.ai), "fast" mode ~0.7 s, with a key
# (server.yaml search.parallel_api_key, or PARALLEL_API_KEY); without one,
# Parallel's free public MCP endpoint (no key, no SLA, unknown rate limit),
# unless search.parallel_keyless is false.
PARALLEL_URL = "https://api.parallel.ai/v1/search"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
PARALLEL_API_KEY = ""
PARALLEL_KEYLESS = True
PARALLEL_MODE = "fast"

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
# Conversation mode ("let's chat", chat.py): more turns of history, and how
# long the addressee check may take before the chat ends instead.
CHAT_MAX_HISTORY = 20
CHAT_ADDRESSEE_TIMEOUT = 2.5
TIMER_STORE_PATH = DATA_DIR / "timers.json"
LISTS_STORE_PATH = DATA_DIR / "lists.json"
SPEAKER_PROFILES_PATH = DATA_DIR / "speaker_profiles.json"
# Kitchen far-field clips score ~0.55 against their own speaker's centroid
# (2026-10-06, 85 captures), so 0.75 rejected nearly everything. A low floor
# plus a lead over the runner-up: 0.35/0.10 made zero leave-one-out errors.
SPEAKER_MIN_SIMILARITY = 0.35  # cosine similarity floor — see speaker_id.py
SPEAKER_MIN_MARGIN = 0.10      # best must beat the runner-up by this much

# ── Music (Music Assistant + Apple Music) ───────────────────────────────────
# Spoken speaker name → Music Assistant player name, from server.yaml's
# music.speakers. Keys double as the words music_intents.py listens for after
# "on/in the …".
MUSIC_SPEAKERS: dict = {}
MUSIC_DEFAULT_PLAYER = ""
PODCASTS: dict | None = None  # podcast.DEFAULT_SHOWS when unset

# ── Assistant ────────────────────────────────────────────────────────────────
DISPLAY_ENABLED = False  # optional wall display; see server_common.DISPLAY_TOPICS
WAKE_WORDS = ["alexa"]
DISPLAY_COMMANDS = []

# PinedaDisplay Web, from server.yaml's pineda: block (antigua_core/pineda.py).
PINEDA_URL = None             # e.g. http://127.0.0.1:8090; None = skill off
PINEDA_PROFILE = "default"    # the kiosk's profile — its theme wins over the global one
PINEDA_DEVICE = None          # the kiosk's device id, for "this photo"; None = latest screen
PINEDA_REBOOT_DELAY_S = 10    # airplaypi plays the "rebooting" reply first
PINEDA_RECIPE_SECONDS = 120   # the full-screen recipe card comes down by itself after this
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

    co = cfg.get("commute", {})
    g["COMMUTE_ENABLED"] = co.get("enabled", COMMUTE_ENABLED)
    if co.get("places_file"):
        g["COMMUTE_PLACES_PATH"] = BASE_DIR / co["places_file"]
    g["COMMUTE_PHOTON_URL"] = co.get("photon_url", COMMUTE_PHOTON_URL)
    g["COMMUTE_OSRM_URL"] = co.get("osrm_url", COMMUTE_OSRM_URL)
    g["COMMUTE_TRANSTAR"] = co.get("transtar", COMMUTE_TRANSTAR)
    g["COMMUTE_SEARCH_RADIUS_KM"] = co.get("search_radius_km", COMMUTE_SEARCH_RADIUS_KM)
    g["COMMUTE_UNUSUAL_MIN_MINUTES"] = co.get("unusual_min_minutes", COMMUTE_UNUSUAL_MIN_MINUTES)
    g["COMMUTE_UNUSUAL_PCT"] = co.get("unusual_pct", COMMUTE_UNUSUAL_PCT)
    g["COMMUTE_TIMEOUT"] = co.get("timeout_seconds", COMMUTE_TIMEOUT)
    g["COMMUTE_AREA_RADIUS_KM"] = co.get("area_radius_km", COMMUTE_AREA_RADIUS_KM)
    g["COMMUTE_ROAD_ALIASES"] = co.get("road_aliases", {})

    k = cfg.get("knowledge", {})
    g["KNOWLEDGE_ENABLED"] = k.get("enabled", KNOWLEDGE_ENABLED)
    g["KNOWLEDGE_TIMEOUT"] = k.get("timeout_seconds", KNOWLEDGE_TIMEOUT)
    g["KNOWLEDGE_TOPIC_TTL"] = k.get("topic_ttl_seconds", KNOWLEDGE_TOPIC_TTL)
    g["KNOWLEDGE_MAX_TOKENS"] = k.get("max_tokens", KNOWLEDGE_MAX_TOKENS)
    g["KNOWLEDGE_FOLLOWUP_MAX_TOKENS"] = k.get("max_tokens_followup", KNOWLEDGE_FOLLOWUP_MAX_TOKENS)
    g["KNOWLEDGE_OVERVIEW_SENTENCES"] = k.get("overview_sentences", KNOWLEDGE_OVERVIEW_SENTENCES)
    g["KNOWLEDGE_LEAD_CHARS"] = k.get("lead_chars", KNOWLEDGE_LEAD_CHARS)
    g["KNOWLEDGE_PASSAGE_CHARS"] = k.get("passage_chars", KNOWLEDGE_PASSAGE_CHARS)
    g["KNOWLEDGE_CONTEXT_CHARS"] = k.get("context_chars", KNOWLEDGE_CONTEXT_CHARS)
    g["KNOWLEDGE_FACTS_WAIT"] = k.get("facts_wait_seconds", KNOWLEDGE_FACTS_WAIT)

    rc = cfg.get("recipes", {})
    g["RECIPE_ENABLED"] = rc.get("enabled", RECIPE_ENABLED)
    g["RECIPE_CANDIDATES"] = rc.get("candidates", RECIPE_CANDIDATES)
    g["RECIPE_FETCH_TIMEOUT"] = rc.get("fetch_timeout_seconds", RECIPE_FETCH_TIMEOUT)
    g["RECIPE_CACHE_DAYS"] = rc.get("cache_days", RECIPE_CACHE_DAYS)
    g["RECIPE_SESSION_TTL"] = rc.get("session_ttl_seconds", RECIPE_SESSION_TTL)
    g["RECIPE_ANSWER_TTL"] = rc.get("answer_ttl_seconds", RECIPE_ANSWER_TTL)
    g["RECIPE_QA_MAX_TOKENS"] = rc.get("qa_max_tokens", RECIPE_QA_MAX_TOKENS)

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
    g["PARALLEL_API_KEY"] = s.get("parallel_api_key") or os.environ.get("PARALLEL_API_KEY", "")
    g["PARALLEL_MODE"] = s.get("parallel_mode", PARALLEL_MODE)
    g["PARALLEL_KEYLESS"] = bool(s.get("parallel_keyless", PARALLEL_KEYLESS))

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

    g["PODCASTS"] = cfg.get("podcasts")

    g["PERSONA_SASS"] = int((cfg.get("persona") or {}).get("sass", PERSONA_SASS))

    g["DISPLAY_ENABLED"] = bool(cfg.get("display", {}).get("enabled", False))

    # PinedaDisplay Web (the airplaypi kiosk screen). No url = no display skill.
    pw = cfg.get("pineda") or {}
    g["PINEDA_URL"] = pw.get("url")
    g["PINEDA_PROFILE"] = pw.get("profile", PINEDA_PROFILE)
    g["PINEDA_DEVICE"] = pw.get("device")
    g["PINEDA_REBOOT_DELAY_S"] = pw.get("reboot_delay_seconds", PINEDA_REBOOT_DELAY_S)
    g["PINEDA_RECIPE_SECONDS"] = pw.get("recipe_seconds", PINEDA_RECIPE_SECONDS)

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
