"""Route classification: ROUTE_ORDER and classify().

classify() walks ROUTE_ORDER and returns the first route whose parser
matches; order matters (earlier routes win). The parsers themselves live in
antigua_core/intents/, one module per skill, and are re-exported here so the
pipeline, the servers and the tests import them from one place.
Network-touching cousins live elsewhere: HomeControl.parse_tv() (configured
input labels), route_search_with_llm() (Ollama), and the format_* builders.
"""

import re

from . import settings
from . import sports as _sports
from .music_intents import parse_music
from .podcast import parse_podcast

# Intent parsers live in intents/, one module per skill; re-exported here so
# callers keep importing them from classify.
from .intents.commute import parse_commute_request  # noqa: F401 — re-exported
from .intents.calc import (  # noqa: F401 — re-exported
    _CALC_CCY,
    _CALC_ROUTE_RE,
    _CALC_UNITS,
    is_calc_request,
)
from .intents.funny_sound import is_funny_sound_request  # noqa: F401 — re-exported
from .intents.govee import (  # noqa: F401 — re-exported
    _GOVEE_ALL_DEVICES,
    _GOVEE_BARE_POWER_MAX_WORDS,
    _GOVEE_BRIGHTNESS_RE,
    _GOVEE_CHANDELIER,
    _GOVEE_COLORS,
    _GOVEE_DEVICE_PATTERNS,
    _GOVEE_KELVIN_RE,
    _GOVEE_POWER_VERB_RE,
    _GOVEE_QUESTION_RE,
    _GOVEE_WHITE_TEMPS,
    parse_govee_request,
)
from .intents.knowledge import (  # noqa: F401 — re-exported
    is_household_question,
    is_knowledge_followup,
    knowledge_language,
    parse_knowledge_request,
    split_possessive,
    who_kind,
)
from .intents.lists import (  # noqa: F401 — re-exported
    _LIST_ADD_RE,
    _LIST_ALIASES,
    _LIST_CLEAR_RE,
    _LIST_NAME_GROUP,
    _LIST_QUERY_RE,
    _LIST_REMOVE_RE,
    _normalize_list_name,
    _split_list_items,
    extract_list_query_name,
    is_list_query,
    parse_list_add_request,
    parse_list_remove_request,
)
from .intents.memory import (  # noqa: F401 — re-exported
    _BROAD_MEDICINE_TERMS,
    _DID_PERSON,
    _FOLLOWUP_RE,
    _FORGET_CONTENT_RE,
    _FORGET_LAST_RE,
    _FORGET_MY_MEMORY_RE,
    _MEMORY_QUERY_RE,
    _REMEMBER_INTENT_RE,
    _SPECIFIC_MEDICINE_TERMS,
    _did_member_re,
    _extract_memory_keywords,
    _is_medicine_query,
    _needs_medicine_detail,
    extract_query_person,
    extract_remember_person,
    is_forget_request,
    is_memory_query,
    parse_forget_content,
    parse_remember_request,
)
from .intents.news import (  # noqa: F401 — re-exported
    _NEWS_CATEGORY_RE,
    _NEWS_FOLLOWUP_RE,
    _NEWS_INTENT_RE,
    _NEWS_LOCATION_RE,
    _NEWS_PLACE_RE,
    _NEWS_SOURCE_RE,
    _NEWS_TOPIC_RE,
    extract_news_category,
    extract_news_source,
    extract_news_topic,
    is_news_followup,
    is_news_request,
)
from .intents.recipe import (  # noqa: F401 — re-exported
    looks_like_food,
    parse_cook_command,
    parse_recipe_request,
    requested_servings,
)
from .intents.search import (  # noqa: F401 — re-exported
    _EVENT_FILLER,
    _EVENT_RESULT_RE,
    _EVENT_TIME_RE,
    _EVENT_WHEN_RE,
    _FRESHNESS_RE,
    _HOWTO_RE,
    _QUERY_FRAMING_RE,
    _QUERY_OF_RE,
    _SEARCH_EVENT_RE,
    _SEARCH_EXPLICIT_RE,
    _SEARCH_FACTUAL_RE,
    _SEARCH_STRIP_PREFIX_RE,
    _SEARCH_TEMPORAL_RE,
    build_search_query,
    extract_search_query,
    is_live_event_query,
    is_local_news_query,
    is_search_request,
    needs_fresh_results,
    rewrite_event_query,
    shape_general_query,
)
from .intents.recipe import parse_swap_question
from .recipe_subs import table_swap
from .intents.substitution import (  # noqa: F401 — re-exported
    _SUBST_CONTEXTUAL_RE,
    _SUBST_EXPLICIT_RE,
    _SUBST_INSTEAD_RE,
    _SUBST_MISSING_RE,
    _SUBST_STRIP_RE,
    _SUBST_TRAILING_RE,
    extract_substitution_ingredient,
    is_substitution_request,
)
from .intents.time_date import (  # noqa: F401 — re-exported
    _DATE_QUERY_RE,
    _TIME_QUERY_RE,
)
from .intents.timers import (  # noqa: F401 — re-exported
    TimerRef,
    alarm_missing_time,
    is_dismiss_request,
    normalize_clock,
    parse_alarm_change,
    parse_pause_request,
    parse_skip_request,
    resolve_new_time,
    timer_missing_length,
    _ADD_TIME_RE,
    _ALARM_RE,
    _ALARM_TRIGGER_RE,
    _AlarmLabelView,
    _CANCEL_VERB,
    _DAILY_WORDS,
    _DAYPART,
    _DAYPART_DEFAULTS,
    _DAYPART_RE,
    _DURATION_TERM_RE,
    _FUZZY_DURATION,
    _NAMED_SUFFIX_RE,
    _NAME_STOP,
    _ORDINAL_WORDS,
    _PICK_ONE,
    _REF_CLOCK_RE,
    _REF_STOP,
    _RELATIVE_TIME_RE,
    _REMINDER_DAY_RE,
    _REMINDER_FILLER_RE,
    _REMINDER_LEAD_RE,
    _REMINDER_LEAD_TIME_RE,
    _REMINDER_TAIL_TIME_RE,
    _REMINDER_TRIGGER_RE,
    _REPEAT_RE,
    _SKIP_NAMES,
    _SNOOZE_RE,
    _STATUS_TARGET_RES,
    _TIMER_NAME_RES,
    _TIMER_NOUN,
    _TIMER_RESET_RE,
    _TIMER_STATUS_RE,
    _UNIT_SECONDS,
    _WEEKDAYS,
    _WORD_TO_NUM,
    _extract_name,
    _extract_reminder_message,
    _extract_timer_name,
    _humanize_seconds,
    _parse_repeat,
    _replace_word_numbers,
    _resolve_clock_time,
    parse_add_time_request,
    parse_alarm_request,
    parse_reminder_request,
    parse_snooze_request,
    parse_timer_cancel_request,
    parse_timer_ref,
    parse_timer_request,
    parse_timer_reset_request,
    parse_timer_status_ref,
    parse_timer_status_request,
    reminder_missing_time,
    resolve_day_offset,
    resolve_duration,
)
from .intents.pineda import parse_pineda_request  # noqa: F401 — re-exported
from .intents.transcript import (  # noqa: F401 — re-exported
    _DIDNT_CATCH,
    _SHOW_ME_RE,
    _STT_FIXES,
    _WHISPER_GARBAGE,
    correct_transcript,
    detect_display_command,
    is_garbage_transcript,
    strip_wake_prefix,
)
from .intents.security import parse_security_request  # noqa: F401 — re-exported
from .intents.tv import (  # noqa: F401 — re-exported
    _HDMI_NUM_RE,
    _TV,
    _TV_APP_RE,
    _TV_INPUT_RE,
    _TV_RE,
)
from .intents.volume import (  # noqa: F401 — re-exported
    _VOLUME_RE,
    parse_volume_request,
)
from .intents.weather import (  # noqa: F401 — re-exported
    _WEATHER_CITY_RE,
    _WEATHER_ROUTE_RE,
    _WEATHER_SIMPLE_RE,
    _WEATHER_TOPIC_RE,
    extract_weather_location,
    wants_weather_context,
)


# Every route run_pipeline can take, in the order it tests them. This is the
# only place the routing order is written down: tests/test_routing.py asserts
# fixtures against it, so a new skill that quietly steals utterances from an
# older one fails the suite instead of failing in the kitchen.
#
# Phase 2 of PLAN_polish_July27.md makes run_pipeline dispatch on classify().
# Until then classify() *mirrors* run_pipeline's branch order and the two must
# be edited together — that duplication is the whole reason this is one short
# function and not scattered through the pipeline.
ROUTE_ORDER = [
    "garbage",
    # Cameras and the front door lock: narrow regexes (a camera or door
    # mention plus a show/lock verb), ahead of Govee so "show me the porch"
    # isn't a light; "the porch light" itself stays Govee's.
    "security",
    # Govee and the TV are tested before volume/media so "tv volume down"
    # reaches the TV and "tv bar lights" reach Govee; plain "turn it up" still
    # lands on volume because the TV regexes all require a TV mention.
    "govee",
    # The airplaypi display before the TV, whose input regex would take
    # "switch to the ocean theme"; its own regexes need a display, theme,
    # Pi, quote, phrase or photo mention, and leave "play the ... theme" alone.
    "pineda",
    "tv",
    # Before podcast/music, which would search Apple Music for "a funny sound".
    "funny_sound",
    # Music before volume so "turn the music up" drives the music player;
    # bare "turn it up" has no "music" and still lands on volume.
    # Podcasts first: "play Apple News today" isn't an Apple Music search.
    "podcast",
    "music",
    "volume",
    "time_date",
    "weather",
    "display",
    "memory_save",
    "memory_forget_content",
    "memory_forget_last",
    "medicine_query",
    "memory_query",
    # Lists come after memory so "remember that..." / "what did I say" are
    # never stolen — the regexes don't overlap, but this keeps the intent
    # ordering readable for whoever edits it next.
    "list_add",
    "list_query",
    "list_remove",
    "timer_add",
    "alarm_skip",
    "timer_cancel",
    "alarm_change",
    "timer_pause",
    "timer_reset",
    "timer_status",
    "snooze",
    "reminder_set",
    "alarm_set",
    "timer_set",
    # "How long to get to Lowe's", "how far is Mom's house" — after the timer
    # block, which owns "how long is left on the pasta timer".
    "commute",
    "sports",
    "calc",
    "news",
    # "How do I make cheesecake" — before search, which has "recipe for" and
    # "how to make" but would answer with one squashed paragraph.
    "recipe",
    "substitution",
    # People, history and events — before search, which would otherwise take
    # "who was X" with a one-sentence snippet answer. Role and time-sensitive
    # questions ("who is the CEO of…", "latest…") are left to search.
    "knowledge",
    "search",
    "llm",
]


def classify(transcript: str, *, search_enabled: bool | None = None) -> str:
    """Return the route run_pipeline would take for `transcript`.

    Pure and network-free — no LLM, no SearXNG, no MCP call, no weather
    fetch — so the fixture suite can run anywhere, including on a box with the
    services down. Where a pipeline branch calls something that touches the
    network, this classifies off that branch's guard regex instead:

      * TV input switching: `_TV_INPUT_RE` rather than
        HomeControl.parse_tv(), which also needs the configured input labels.
      * Weather: `_WEATHER_SIMPLE_RE` + location guard rather than
        format_weather_response(), which reads the wttr.in cache.
      * Search: pattern layers only. The LLM router that run_pipeline falls back
        to is deliberately not consulted, so "search" here means "a regex layer
        claimed it". Anything the router *might* claim classifies as "llm".

    Two known divergences from run_pipeline, both pre-existing:

      * The memory follow-up branch (`_FOLLOWUP_RE` + `_last_memory_topic`) and
        the knowledge follow-up ("was she married?" after "who was Frida
        Kahlo") are state, not properties of the transcript, so neither is
        reachable here.
      * run_pipeline's context-injecting branches (memory_query, news, search)
        fall through rather than returning, so a transcript matching both
        memory_query and a timer would trigger both. classify() reports the
        first match. Phase 2 should resolve that ambiguity in the pipeline
        rather than teach this function to reproduce it.
    """
    if search_enabled is None:
        search_enabled = settings.SEARCH_ENABLED

    if not transcript or is_garbage_transcript(transcript):
        return "garbage"

    if parse_security_request(transcript):
        return "security"
    # Govee before the TV so "tv bar" / "tv lights" aren't swallowed by the TV
    # regex; both before volume so "volume up on the tv" reaches the TV.
    if parse_govee_request(transcript):
        return "govee"
    if parse_pineda_request(transcript):
        return "pineda"
    if (_TV_APP_RE.search(transcript) or _TV_INPUT_RE.search(transcript)
            or _TV_RE.search(transcript)):
        return "tv"

    if is_funny_sound_request(transcript):
        return "funny_sound"
    if parse_podcast(transcript):
        return "podcast"
    if parse_music(transcript):
        return "music"
    if parse_volume_request(transcript):
        return "volume"

    if _TIME_QUERY_RE.search(transcript) or _DATE_QUERY_RE.search(transcript):
        return "time_date"

    if _WEATHER_SIMPLE_RE.search(transcript):
        # Any location — the weather skill fetches the forecast for the city
        # named ("weather in Denver") as readily as for home.
        return "weather"

    if detect_display_command(transcript):
        return "display"

    # "What time did I set my alarm for" is ours, not a memory question.
    if parse_timer_status_request(transcript) and re.search(
            r"\b(?:alarm|timer|reminder)s?\b", transcript, re.IGNORECASE):
        return "timer_status"

    if parse_remember_request(transcript):
        return "memory_save"
    if parse_forget_content(transcript):
        return "memory_forget_content"
    if is_forget_request(transcript):
        return "memory_forget_last"
    if _is_medicine_query(transcript):
        return "medicine_query"
    if is_memory_query(transcript):
        return "memory_query"

    if parse_list_add_request(transcript):
        return "list_add"
    if is_list_query(transcript):
        return "list_query"
    if parse_list_remove_request(transcript):
        return "list_remove"

    # Timer block. run_pipeline computes timer/alarm up front but tests the
    # chain in this order, and only looks for an alarm when no timer matched.
    # Add (and take-off) first: "remove 2 minutes from the timer" isn't a
    # cancel. Skip before cancel: "turn off my alarm for tomorrow" sits out
    # one day of a repeating alarm. Change before reset: "reset my alarm to 7".
    if parse_add_time_request(transcript):
        return "timer_add"
    if parse_skip_request(transcript):
        return "alarm_skip"
    if parse_timer_cancel_request(transcript):
        return "timer_cancel"
    if parse_alarm_change(transcript):
        return "alarm_change"
    if parse_pause_request(transcript):
        return "timer_pause"
    if parse_timer_reset_request(transcript) is not None:
        return "timer_reset"
    if parse_timer_status_request(transcript):
        return "timer_status"
    if parse_snooze_request(transcript):
        return "snooze"
    # Reminders before alarms/timers: "remind me ... at 9" carries an action
    # phrase and should not be reduced to a bare alarm. An under-specified one
    # ("remind me ... tomorrow") also routes here — the handler asks the time.
    if parse_reminder_request(transcript) or reminder_missing_time(transcript):
        return "reminder_set"
    # No time / no length: the handler asks "for what time?" / "for how long?"
    if parse_alarm_request(transcript) or alarm_missing_time(transcript):
        return "alarm_set"
    if parse_timer_request(transcript) or timer_missing_length(transcript) is not None:
        return "timer_set"

    if settings.COMMUTE_ENABLED and parse_commute_request(transcript):
        return "commute"

    # Broader weather phrasings ("do I need an umbrella", "colder than
    # yesterday", "when's sunset") — after the imperative-shaped routes above so
    # "remind me to bring an umbrella" stays a memory.
    if _WEATHER_ROUTE_RE.search(transcript):
        return "weather"

    # NFL / MLB / NBA / MLS / F1 scores, results, next game, record —
    # resolve_team()/is_f1_query() are pure/offline (static alias table), so
    # this can run unconditionally before the live-event search block below,
    # claiming anything these 5 leagues can answer deterministically. A
    # sports-verb word is required alongside a team/F1 mention so a casual
    # mention ("I met a Texans fan yesterday") doesn't get claimed.
    if _sports.is_sports_query(transcript):
        return "sports"

    # Arithmetic / unit / temperature conversion — deterministic, offline, and
    # answered by calc.answer(). After weather/timers/memory so "add 5 minutes"
    # and "colder than yesterday" are already claimed; a miss here falls to the
    # LLM exactly as before.
    if is_calc_request(transcript):
        return "calc"

    # Topic-form requests ("news about ukraine") are news even when the intent
    # regex misses — extract_news_topic() is the filter the RSS path applies.
    if (is_news_request(transcript) or extract_news_topic(transcript)) \
            and not is_local_news_query(transcript):
        return "news"

    if search_enabled and settings.RECIPE_ENABLED and parse_recipe_request(transcript):
        return "recipe"
    if search_enabled:
        if is_substitution_request(transcript):
            return "substitution"
    # "Can I use olive oil instead of butter?" the curated table answers,
    # search or not (recipe_subs.table_swap).
    swap = parse_swap_question(transcript)
    if swap and table_swap(*swap):
        return "substitution"
    if settings.KNOWLEDGE_ENABLED and parse_knowledge_request(transcript):
        return "knowledge"
    if search_enabled:
        if is_search_request(transcript):
            return "search"

    return "llm"
