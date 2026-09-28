"""The Antigua pipeline, written once and shared by both servers.

run_pipeline() dispatches on classify() — the branch order lives in ROUTE_ORDER
and is covered by tests/fixtures/routing.yaml. Adding or reordering a skill
means updating classify() and the _HANDLERS map here, and adding fixtures.

Backend differences (whisper.cpp vs faster-whisper, 4b vs 0.8b, remote vs
local TTS) are injected via the Backend dataclass;
init(backend) must be called once at startup, after settings.configure().

Phase 2 note: the old run_pipeline's context-injecting branches (memory_query,
news, search) fell through rather than returning, so a transcript matching both
memory_query and a timer fired both. Dispatching on classify() resolves that:
the first matching route wins, as the fixtures assert.
"""

import logging
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Callable, Iterator, Optional

from . import household, settings
from .caches import pin_year
from .classify import (
    _DATE_QUERY_RE,
    _DIDNT_CATCH,
    _EVENT_WHEN_RE,
    _FOLLOWUP_RE,
    _HOWTO_RE,
    _SEARCH_TEMPORAL_RE,
    _TIME_QUERY_RE,
    _WEATHER_SIMPLE_RE,
    _WEATHER_TOPIC_RE,
    _extract_memory_keywords,
    _needs_medicine_detail,
    build_search_query,
    classify,
    correct_transcript,
    detect_display_command,
    extract_news_category,
    extract_news_source,
    extract_news_topic,
    extract_list_query_name,
    extract_query_person,
    extract_remember_person,
    extract_substitution_ingredient,
    extract_weather_location,
    is_garbage_transcript,
    is_news_followup,
    needs_fresh_results,
    parse_add_time_request,
    parse_alarm_request,
    parse_forget_content,
    parse_govee_request,
    parse_list_add_request,
    parse_list_remove_request,
    parse_reminder_request,
    parse_remember_request,
    reminder_missing_time,
    _replace_word_numbers,
    parse_snooze_request,
    parse_timer_cancel_request,
    parse_timer_ref,
    parse_timer_status_ref,
    TimerRef,
    parse_timer_request,
    parse_timer_reset_request,
    parse_volume_request,
    strip_wake_prefix,
)
from .music_intents import MusicIntent, parse_music
from .spanish import reply_in_spanish, to_english_command
from .stores import _MEMORY_TAGS, TimerSpec as _TimerSpec
from . import calc as calc_skill
from . import sports as sports_skill
from . import weather as weather_skill

log = logging.getLogger("antigua_core")


# ── Backend injection ────────────────────────────────────────────────────────


@dataclass
class Backend:
    """What the primary and fallback genuinely differ on.

    Required: STT, LLM streaming, TTS, and where audio URLs point.
    Optional capabilities default to None; a route whose capability is absent
    answers with a spoken "can't do that from here" instead of failing.
    """

    transcribe: Callable[..., dict]      # (audio_path, hotwords=None) -> {"text", ...}
    synthesize: Callable[..., str]           # (text, lang="en") -> wav path
    ask_llm_stream: Callable[..., Iterator[str]]
    audio_url_base: Callable[[], str]          # () -> "http://ip:port"
    memory_store: object
    list_store: object
    timers: object
    weather_cache: object
    news_cache: object
    weather_provider: object = None   # weather.WeatherProvider; None on the
                                      # legacy/offline fallback backend
    rates_provider: object = None     # calc_currency.RatesProvider; None where
                                      # currency conversion isn't available
    sports_provider: object = None    # sports.SportsProvider; None on the
                                      # legacy/offline fallback backend
    searxng: object = None
    route_search_with_llm: Optional[Callable[[str], Optional[str]]] = None
    identify_speaker: Optional[Callable[[str], Optional[str]]] = None
    # Slower, more accurate STT (medium.en), for a second opinion on short
    # transcripts heard over music. None = no second opinion.
    transcribe_careful: Optional[Callable[[str], dict]] = None
    # English skill reply -> Spanish, for replies spanish.py has no template
    # for (weather, calc, sports...). None = those stay English.
    translate_to_spanish: Optional[Callable[[str], Optional[str]]] = None
    static_audio_dir: Optional[Path] = None
    # Roku TV + Govee lights through MCP (home_control.HomeControl). Both
    # servers run their own MCP servers, so both normally have this.
    home: object = None
    # Music (music.MusicControl → Music Assistant); None where no MA token is set.
    music: object = None


B: Backend = None  # set by init()


def init(backend: Backend):
    global B
    B = backend


# ── Conversation-scoped pipeline state ───────────────────────────────────────

# Pending memory state: conv_id -> {fact, expires}
# Holds a remembered fact while waiting for the user to say who it's for.
_pending_memories: dict[str, dict] = {}

# Tracks the last memory topic per conversation for follow-up handling
_last_memory_topic: dict[str, dict] = {}

# Tracks the headlines just read per conversation — conv_id -> {items, expires}
# — so a follow-up ("tell me more about the X story") can be re-grounded in
# the real item(s) rather than answered from nothing but conversation history.
_last_news_items: dict[str, dict] = {}

# conv_id -> {"lang", "expires"}: a conversation that went Spanish stays
# Spanish on its next Spanish-leaning turn, even a short one.
_conv_language: dict[str, dict] = {}
_CONV_LANGUAGE_TTL = 300   # matches the LLM conversation memory

# Pending reminder state: conv_id -> {message, day_phrase, expires}. Holds a
# reminder whose day is known but whose time isn't, while we wait for the user
# to answer "what time?".
_pending_reminders: dict[str, dict] = {}

# "Which one?" for a cancel/add that matched several timers: conv_id ->
# {action, ids, seconds, expires}. And the timer a conversation last talked
# about, so "the other one" / "add a minute to it" can resolve.
_pending_timer_choice: dict[str, dict] = {}
_last_timer_ref: dict[str, dict] = {}

_last_fired_alarm: tuple[str, float] = ("", 0.0)
_SNOOZE_WINDOW_S = 900  # a bare "snooze" only refers to something that rang recently


def note_alarm_fired(label: str):
    """Called by the backend's timer-fire hook so 'snooze' knows what rang."""
    global _last_fired_alarm
    _last_fired_alarm = (label, time.time())


def _cleanup_last_topics():
    now = time.time()
    for cid in [c for c, v in _last_memory_topic.items() if now > v["expires"]]:
        del _last_memory_topic[cid]
    for cid in [c for c, v in _last_news_items.items() if now > v["expires"]]:
        del _last_news_items[cid]
    for cid in [c for c, v in _conv_language.items() if now > v["expires"]]:
        del _conv_language[cid]
    for cid in [c for c, v in _last_timer_ref.items() if now > v["expires"]]:
        del _last_timer_ref[cid]


def cleanup_pending():
    """Expire stale pending-memory entries; called from the cleanup loop."""
    now = time.time()
    for cid in [c for c, v in _pending_memories.items() if now > v["expires"]]:
        del _pending_memories[cid]
    for cid in [c for c, v in _pending_reminders.items() if now > v["expires"]]:
        del _pending_reminders[cid]
    for cid in [c for c, v in _pending_timer_choice.items() if now > v["expires"]]:
        del _pending_timer_choice[cid]
    _cleanup_last_topics()


# ── Response helpers ─────────────────────────────────────────────────────────


def _resp(t, reply, wav_path, action=None, llm_time=0, **extra):
    d = {
        "transcript": t.transcript,
        "response": reply,
        "audio_file": wav_path,
        "stt_time": t.stt_time,
        "llm_time": llm_time,
        "tts_time": 0,
        "conversation_id": t.conversation_id,
    }
    if action:
        d["action"] = action
    d.update(extra)
    return d


def _speak(t, reply, action=None, static_name=None, **extra):
    """Synthesize (or reuse a pre-generated static WAV) and build the response.
    On a Spanish turn the skill's English reply is spoken in Spanish."""
    lang = "en"
    if t.language == "es" and reply:
        es = reply_in_spanish(reply)
        if es is None and B.translate_to_spanish is not None:
            es = B.translate_to_spanish(reply)
        if es:
            reply, lang, static_name = es, "es", None   # the static WAVs are English
    wav_path = None
    if static_name and B.static_audio_dir:
        p = B.static_audio_dir / static_name
        if p.exists():
            wav_path = str(p)
    if wav_path is None:
        wav_path = B.synthesize(reply, lang=lang)
    return _resp(t, reply, wav_path, action=action, **extra)


@dataclass
class _Turn:
    transcript: str
    conversation_id: str
    stt_time: float
    extra_context: str = None
    max_tokens: int = None
    temperature: float = None
    grounding: str = None
    speaker: str = None  # from Backend.identify_speaker; None = not confident enough
    quiet: bool = False  # /pipeline_text QA path: suppress antigua/play so a
                         # dry run doesn't speak out of the real satellite
    play_topic: str = "antigua/play"  # a mic can route its own reply chunks elsewhere
                                      # (the kitchen bridge plays them on the reSpeaker)
    audio_path: str = None  # the recording, so a skill can re-run STT with hints
    language: str = "en"    # "en" or "es", from STT; picks the LLM hint and TTS voice


# ── Formatters (deterministic replies) ───────────────────────────────────────


def format_remaining(seconds):
    """Format seconds remaining as a natural string."""
    s = int(seconds)
    if s >= 3600:
        h, rem = divmod(s, 3600)
        m = rem // 60
        return f"{h} hour{'s' if h != 1 else ''}" + (
            f" {m} minute{'s' if m != 1 else ''}" if m else ""
        )
    elif s >= 60:
        m, sec = divmod(s, 60)
        return f"{m} minute{'s' if m != 1 else ''}" + (
            f" {sec} second{'s' if sec != 1 else ''}" if sec else ""
        )
    return f"{s} second{'s' if s != 1 else ''}"


def _round_remaining(seconds):
    """Spoken time-left, rounded so a running timer doesn't say '14 minutes 59
    seconds'. Under 90s stays exact."""
    s = int(round(seconds))
    if s < 90:
        return format_remaining(s)
    mins = (s + 59) // 60  # round up to the next whole minute
    if mins < 60:
        return f"{mins} minute{'s' if mins != 1 else ''}"
    h, m = divmod(mins, 60)
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minute{'s' if m != 1 else ''}" if m else "")


def _pretty_clock(hour, minute):
    if minute == 0 and hour == 12:
        return "noon"
    if minute == 0 and hour == 0:
        return "midnight"
    d = datetime(2000, 1, 1, hour, minute)
    return d.strftime("%-I %p") if minute == 0 else d.strftime("%-I:%M %p")


def _spoken_alarm(spec_or_dict):
    """'7 AM tomorrow' / '7:30 AM every weekday' from a spec or a list_active row."""
    e = spec_or_dict
    if isinstance(e, dict):
        fires_at = e["fires_at"]
        repeat = e.get("repeat")
    else:
        fires_at = time.time() + e.seconds
        repeat = e.repeat
    dt = datetime.fromtimestamp(fires_at)
    base = _pretty_clock(dt.hour, dt.minute)
    if repeat:
        from .stores import _repeat_phrase
        return f"{base} {_repeat_phrase(repeat)}"
    today = datetime.now().date()
    if dt.date() == today + timedelta(days=1):
        return f"{base} tomorrow"
    if dt.date() != today:
        return f"{base} on {dt:%A}"
    return base


def format_set_reply(spec):
    """Deterministic confirmation for a newly-set timer / alarm / reminder."""
    if spec.kind == "alarm":
        lead = f"{spec.name[:1].upper()}{spec.name[1:]} alarm" if spec.name else "Alarm"
        return f"{lead} set for {_spoken_alarm(spec)}."
    if spec.kind == "reminder":
        to = f" to {spec.message}" if spec.message else ""
        when = (f"at {_spoken_alarm(spec)}" if spec.hour is not None
                else f"in {format_remaining(spec.seconds)}")
        return f"Okay, I'll remind you{to} {when}."
    if spec.name:
        return f"{spec.name[:1].upper()}{spec.name[1:]} timer set for {format_remaining(spec.seconds)}."
    return f"Timer set for {format_remaining(spec.seconds)}."


def format_active_timers():
    """Human-readable active timers/alarms for the LLM context."""
    active = B.timers.list_active()
    if not active:
        return "No active timers or alarms."
    parts = []
    for t in active:
        if t["kind"] == "alarm":
            parts.append(f"{t['label']} ({_spoken_alarm(t)})")
        elif t["kind"] == "reminder":
            when = (_spoken_alarm(t) if t.get("hour") is not None
                    else f"in {format_remaining(t['remaining_s'])}")
            parts.append(f"reminder to {t['message'] or 'something'} ({when})")
        else:
            parts.append(f"{t['label']}: {format_remaining(t['remaining_s'])} left")
    return "Active: " + "; ".join(parts) + "."


# ── Timer references ─────────────────────────────────────────────────────────
# Which entry "the 5 minute timer" / "the first one" / "my gym alarm" means.
# classify.parse_timer_ref turns the words into a TimerRef; these match it
# against the live entries, and name entries back so two identical timers
# stay distinguishable ("first 5-minute timer" / "second 5-minute timer").

_ORDINAL_SPOKEN = ("first", "second", "third", "fourth", "fifth", "sixth")
_COUNT_SPOKEN = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def _length_phrase(seconds) -> str:
    """A set length as an adjective: '5-minute', '1 hour 30 minute'."""
    w = format_remaining(seconds).split()
    pairs = [(w[i], w[i + 1].rstrip("s")) for i in range(0, len(w) - 1, 2)]
    if len(pairs) == 1:
        return f"{pairs[0][0]}-{pairs[0][1]}"
    return " ".join(f"{n} {u}" for n, u in pairs)


def _base_desc(r) -> str:
    if r["kind"] == "alarm":
        if r.get("name"):
            return f"{r['name']} alarm"
        return f"{_pretty_clock(r['hour'], r['minute'])} alarm" if r.get("hour") is not None else "alarm"
    if r["kind"] == "reminder":
        return f"reminder to {r['message']}" if r.get("message") else "reminder"
    if r.get("name"):
        return f"{r['name']} timer"
    return f"{_length_phrase(r['set_s'])} timer" if r.get("set_s") else "timer"


def _describe(rows) -> dict:
    """id -> a name for each entry that no other entry shares, with an
    ordinal (in the order they were set) where two would otherwise match."""
    groups: dict[str, list] = {}
    for r in sorted(rows, key=lambda r: r["created_at"]):
        groups.setdefault(_base_desc(r), []).append(r)
    out = {}
    for base, rs in groups.items():
        for i, r in enumerate(rs):
            out[r["id"]] = base if len(rs) == 1 else f"{_ORDINAL_SPOKEN[min(i, 5)]} {base}"
    return out


def _timer_name(r, active, desc) -> str:
    """'your timer' when it's the only unnamed timer running, else 'your <desc>'."""
    if (r["kind"] == "timer" and not r.get("name")
            and sum(1 for x in active if x["kind"] == "timer") == 1):
        return "your timer"
    return f"your {desc[r['id']]}"


def _select_timers(rows, ref: TimerRef, conv_id: str = "") -> list:
    """Entries matching every filter in `ref`, in the order they were set."""
    c = [r for r in rows if not ref.kind or r["kind"] == ref.kind]
    if ref.name:
        def hay(r):
            return " ".join(filter(None, (r.get("name"), r["label"], r.get("message")))
                            ).lower().replace("-", " ")
        c = [r for r in c if ref.name in hay(r)]
    if ref.seconds is not None:
        c = [r for r in c if r["kind"] != "alarm" and abs(r.get("set_s", 0) - ref.seconds) < 1]
    if ref.hour is not None:
        def at(r):
            if r.get("hour") is None or r["minute"] != ref.minute:
                return False
            return r["hour"] == ref.hour if ref.meridiem else r["hour"] % 12 == ref.hour % 12
        c = [r for r in c if at(r)]
    if ref.other:
        last = _last_timer_ref.get(conv_id)
        if last and time.time() < last["expires"]:
            c = [r for r in c if r["id"] != last["tid"]]
    c.sort(key=lambda r: r["created_at"])
    if ref.ordinal is not None:
        c = [c[ref.ordinal]] if -len(c) <= ref.ordinal < len(c) else []
    return c


def _ref_phrase(ref: TimerRef) -> str:
    """What the user asked for, to say back: 'a 5-minute timer', 'an 8 AM alarm'."""
    bits = ["other"] if ref.other else []
    if ref.ordinal is not None:
        bits.append("last" if ref.ordinal < 0 else _ORDINAL_SPOKEN[min(ref.ordinal, 5)])
    if ref.hour is not None:
        bits.append(_pretty_clock(ref.hour, ref.minute) if ref.meridiem
                    else f"{ref.hour}" + (f":{ref.minute:02d}" if ref.minute else ""))
    if ref.name:
        bits.append(ref.name)
    if ref.seconds:
        bits.append(_length_phrase(ref.seconds))
    bits.append(ref.kind or "timer, alarm, or reminder")
    phrase = " ".join(bits)
    return ("an " if re.match(r"(?:[aeiou]|8|11 |11:|18)", phrase) else "a ") + phrase


def _note_timer(conv_id: str, row):
    _last_timer_ref[conv_id] = {"tid": row["id"], "expires": time.time() + 300}


def _remind_when(r) -> str:
    return (f"at {_spoken_alarm(r)}" if r.get("hour") is not None
            else f"in {_round_remaining(r['remaining_s'])}")


def _until(r) -> str:
    """', 9 hours from now' for an alarm within a day, else ''."""
    return f", {_round_remaining(r['remaining_s'])} from now" if r["remaining_s"] < 86400 else ""


def format_timer_status_reply(ref: TimerRef | None = None, conv_id: str = ""):
    """Type-aware answer to 'how much time is left' / 'when's my gym alarm' /
    'how long on the second 5 minute timer'."""
    ref = ref or TimerRef()
    active = B.timers.list_active()
    rows = _select_timers(active, ref, conv_id)
    if not rows:
        if ref.kind and (ref.empty() or not any(r["kind"] == ref.kind for r in active)):
            return f"You don't have any {ref.kind}s set."
        if not active:
            return "You don't have any timers or alarms set."
        return f"You don't have {_ref_phrase(ref)}."
    desc = _describe(active)

    def _one(r):
        if r["kind"] == "alarm":
            lead = f"Your {r['name']} alarm" if r.get("name") else "Your alarm"
            return f"{lead} is set for {_spoken_alarm(r)}{_until(r)}."
        if r["kind"] == "reminder":
            what = f" to {r['message']}" if r.get("message") else ""
            return f"I'll remind you{what} {_remind_when(r)}."
        name = _timer_name(r, active, desc)
        return f"{name[:1].upper()}{name[1:]} has {_round_remaining(r['remaining_s'])} left."

    if len(rows) == 1:
        _note_timer(conv_id, rows[0])
        return _one(rows[0])
    rows = sorted(rows, key=lambda r: r["fires_at"])
    bits = [f"{_timer_name(r, active, desc)} at {_round_remaining(r['remaining_s'])}"
            for r in rows if r["kind"] == "timer"]
    bits += [f"a reminder to {r['message']} {_remind_when(r)}" if r.get("message")
             else f"a reminder {_remind_when(r)}"
             for r in rows if r["kind"] == "reminder"]
    bits += [f"your {r['name']} alarm for {_spoken_alarm(r)}" if r.get("name")
             else f"an alarm for {_spoken_alarm(r)}"
             for r in rows if r["kind"] == "alarm"]
    joined = ", ".join(bits[:-1]) + f", and {bits[-1]}" if len(bits) > 1 else bits[0]
    return f"You've got {joined}."


def format_time_date_response(transcript: str):
    """Return a direct time/date answer string, or None if not a time/date query."""
    is_time = bool(_TIME_QUERY_RE.search(transcript))
    is_date = bool(_DATE_QUERY_RE.search(transcript))
    if not (is_time or is_date):
        return None
    now = datetime.now()
    if now.hour == 12 and now.minute == 0:
        time_str = "noon"
    elif now.hour == 0 and now.minute == 0:
        time_str = "midnight"
    else:
        time_str = now.strftime("%-I:%M %p")
    date_str = now.strftime("%A, %B %-d")
    if is_time and is_date:
        return f"It's {time_str} on {date_str}."
    if is_time:
        return f"It's {time_str}."
    return f"Today is {date_str}."


_WEATHER_FUTURE_RE = re.compile(
    r"\b(tomorrow|tonight|this\s+(?:evening|afternoon|morning|weekend)|"
    r"later|next\s+week|weekend|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b",
    re.IGNORECASE,
)


def format_weather_response(transcript: str) -> str | None:
    """Return a direct spoken weather summary, or None if not a simple weather query."""
    if not _WEATHER_SIMPLE_RE.search(transcript):
        return None
    # A future timeframe ("weather tomorrow", "will it rain this weekend") needs
    # the forecast, not current conditions — hand it to the LLM, which has the
    # 3-day forecast in its prompt.
    if _WEATHER_FUTURE_RE.search(transcript):
        return None
    # If a specific city is requested, let the LLM handle it
    loc = extract_weather_location(transcript)
    if loc and loc != settings.DEFAULT_WEATHER_LOCATION:
        return None
    data = B.weather_cache.get()
    if not data:
        return "Weather data is unavailable right now."
    c = data
    parts = [f"Currently {c['temp_f']} degrees and {c['desc']}."]
    today = c["forecast"][0] if c.get("forecast") else None
    if today:
        parts.append(
            f"Today's high is {today['high_f']} with a low of {today['low_f']}."
        )
        if int(today.get("rain_chance", 0)) > 0:
            parts.append(f"There's a {today['rain_chance']}% chance of rain.")
    return " ".join(parts)


def _answer_medicine_query(person: str) -> str:
    """Answer a medicine query directly from today's entries."""
    entries = B.memory_store.query_today(person)
    med_entries = [
        e
        for e in entries
        if any(kw in e["raw"].lower() for kw in _MEMORY_TAGS["medicine"])
    ]
    if not med_entries:
        return f"No, I don't have any medicine recorded for {person} today."
    latest = sorted(med_entries, key=lambda x: x["timestamp"], reverse=True)[0]
    ts = datetime.fromisoformat(latest["timestamp"]).strftime("%-I:%M %p")
    # Strip leading "took ..." so the sentence reads naturally
    display = re.sub(
        r"^took\s+(?:his|her|my|the)?\s*", "", latest["raw"], flags=re.IGNORECASE
    )
    return f"Yes — {person} took {display} at {ts} today."


# ── Route handlers ───────────────────────────────────────────────────────────
# Each takes a _Turn and returns a response dict, or None to continue to the
# LLM tail (optionally having set extra_context / max_tokens on the turn).


def _handle_volume(t):
    # A bare "turn it up" is for whatever is playing: the music if any is,
    # else the TV (soundbar via the Apple TV's HDMI-CEC). Never Antigua's own
    # voice or the Pi's sink.
    vol_action = parse_volume_request(t.transcript)
    if B.music is not None and B.music.playing():
        return _handle_music(t, MusicIntent("control", vol_action))
    if B.home is None or not B.home.tv_available():
        return _speak(t, "I can't control the TV right now.")
    action = f"tv_{vol_action}"
    result = B.home.tv(action)
    log.info("Volume -> TV: %s -> %s", action, result)
    reply = _TV_REPLIES[action] if result == "ok" else _TV_NO_RESPONSE
    return _speak(t, reply, action=action, static_name=_TV_STATIC_AUDIO.get(reply))


def _handle_music(t, intent=None):
    intent = intent or parse_music(t.transcript)
    if B.music is None:
        return _speak(t, "I can't play music right now.")
    reply = B.music.handle(intent, rehear=_rehear_fn(t))
    log.info("Music: %s/%s %r -> %r", intent.kind, intent.action,
             intent.query or intent.artist, reply)
    action = f"music_{intent.action}"
    if not reply:
        return _resp(t, "", None, action=action)   # transport controls are silent
    return _speak(t, reply, action=action)


def _rehear_fn(t):
    """STT again on this turn's audio, biased toward the given names."""
    if not t.audio_path:
        return None

    def rehear(hints):
        text = strip_wake_prefix(correct_transcript(
            B.transcribe(t.audio_path, hotwords=", ".join(hints))["text"]))
        return to_english_command(text) or text   # "pon música de X" -> "play X"
    return rehear


def _handle_govee(t):
    g_action, g_aliases, g_spoken, g_param = parse_govee_request(t.transcript)
    if B.home is None or not B.home.govee_available():
        return _speak(t, "I can't control the lights right now.")
    Thread(
        target=B.home.govee, args=(g_action, g_aliases, g_param), daemon=True
    ).start()
    if g_action == "power":
        reply = f"Turning {'on' if g_param else 'off'} {g_spoken}"
    elif g_action in ("color", "temperature"):
        reply = f"Setting {g_spoken} to {g_param[0]}"
    else:
        reply = f"Setting {g_spoken} to {g_param} percent"
    log.info("Govee: %s %s param=%s", g_action, g_aliases, g_param)
    return _speak(t, reply, action=f"govee_{g_action}")


# Pre-rendered confirmations (files keep their original roku_* names).
_TV_STATIC_AUDIO = {
    "Turning on the Living Room TV": "roku_power_on.wav",
    "Turning off the Living Room TV": "roku_power_off.wav",
    "Muting the Living Room TV": "roku_mute.wav",
    "Unmuting the Living Room TV": "roku_unmute.wav",
    "Turning up the Living Room TV volume": "roku_volume_up.wav",
    "Turning down the Living Room TV volume": "roku_volume_down.wav",
    "Going home on the Living Room TV": "roku_home.wav",
    "Going back on the Living Room TV": "roku_back.wav",
    "The Living Room TV did not respond. Check it is on and connected.": "roku_no_response.wav",
}

_TV_NO_RESPONSE = "The Living Room TV did not respond. Check it is on and connected."

_TV_REPLIES = {
    "tv_power_on": "Turning on the Living Room TV",
    "tv_power_off": "Turning off the Living Room TV",
    "tv_mute": "Muting the Living Room TV",
    "tv_unmute": "Unmuting the Living Room TV",
    "tv_volume_up": "Turning up the Living Room TV volume",
    "tv_volume_down": "Turning down the Living Room TV volume",
    "tv_home": "Going home on the Living Room TV",
    "tv_back": "Going back on the Living Room TV",
    "tv_play": "Resuming the Living Room TV",
    "tv_pause": "Pausing the Living Room TV",
}


def _handle_tv(t):
    if B.home is None or not B.home.tv_available():
        return _speak(t, "I can't control the TV right now.")
    parsed = B.home.parse_tv(t.transcript)
    if not parsed:
        return None  # TV regex matched but no recognizable command — LLM tail
    action, arg = parsed
    result = B.home.tv(action, arg)
    if result == "ok":
        if action == "tv_app":
            reply = f"Opening {B.home.spoken_target(action, arg)}"
        elif action == "tv_input":
            reply = f"Switching to {B.home.spoken_target(action, arg)}"
        else:
            reply = _TV_REPLIES[action]
    elif result == "no_app":
        reply = f"I don't see {B.home.spoken_target(action, arg)} on the Living Room TV."
    else:
        reply = _TV_NO_RESPONSE
    log.info("TV: %s %s -> %s", action, arg, result)
    return _speak(t, reply, action=action, static_name=_TV_STATIC_AUDIO.get(reply))


def _handle_time_date(t):
    reply = format_time_date_response(t.transcript)
    log.info("Time/date shortcut: %s", reply)
    return _speak(t, reply)


def _handle_weather(t):
    provider = getattr(B, "weather_provider", None)
    if provider is not None:
        reply = weather_skill.answer(t.transcript, provider)
        if reply:
            log.info("Weather: %s", reply)
            return _speak(t, reply)
    # Fallback: legacy wttr.in path (backends without a provider, e.g. the
    # offline fallback server).
    reply = format_weather_response(t.transcript)
    if not reply:
        return None  # non-local city with no provider — LLM tail handles it
    log.info("Weather shortcut: %s", reply)
    return _speak(t, reply)


def _handle_calc(t):
    reply = calc_skill.answer(t.transcript, rates=getattr(B, "rates_provider", None))
    if reply:
        log.info("Calc: %s", reply)
        return _speak(t, reply)
    return None  # calc.answer() declined — fall through to the LLM tail


def _handle_sports(t):
    provider = getattr(B, "sports_provider", None)
    if provider is None:
        return None  # no provider configured — fall through to the LLM tail
    reply = sports_skill.answer(t.transcript, provider)
    if reply:
        log.info("Sports: %s", reply)
        return _speak(t, reply)
    return None  # classify() already gated on resolve_team()/is_f1_query() — rare


def _handle_display(t):
    display_cmd = detect_display_command(t.transcript)
    label = display_cmd.get("label", "that")
    duration = display_cmd.get("duration", 30)
    settings.mqtt_publish("antigua/command", {
        "action": "youtube",
        "url": display_cmd["url"],
        "duration": duration,
        "label": label,
    })
    reply = f"Showing {label} for {duration} seconds."
    log.info("Display command: %s → %s", label, display_cmd["url"])
    return _speak(t, reply)


def _save_memory(t, person, fact):
    """Shared save path: add, handle duplicates and the medicine-detail loop."""
    entry = B.memory_store.add(person, fact)
    if entry is None:
        return f"I already noted that {person} {fact}."
    if _needs_medicine_detail(fact):
        _pending_memories[t.conversation_id] = {
            "fact": "__medicine_details__",
            "entry_id": entry["id"],
            "person": person,
            "expires": time.time() + 60,
        }
        log.info("Medicine detail pending for conv %s: %s", t.conversation_id, fact)
        return "What medicine did you take specifically?"
    _last_memory_topic[t.conversation_id] = {
        "topic": fact,
        "expires": time.time() + 300,
    }
    ts = datetime.fromisoformat(entry["timestamp"]).strftime("%-I:%M %p")
    log.info("Memory saved: %s — %s", person, fact)
    settings.mqtt_publish("antigua/memory", {"action": "save", "entry": {
        "id": entry["id"], "person": person, "raw": fact,
        "tags": entry.get("tags", []), "permanent": entry.get("permanent", False),
    }})
    return f"Got it. I'll remember {person} {fact} at {ts}."


def _handle_memory_save(t):
    fact = parse_remember_request(t.transcript)
    # An explicit "for <name>" always wins; speaker ID only fills in when the
    # transcript itself doesn't say who this is for.
    person = extract_remember_person(t.transcript) or t.speaker
    if person:
        reply = _save_memory(t, person, fact)
    else:
        _pending_memories[t.conversation_id] = {
            "fact": fact,
            "expires": time.time() + 60,
        }
        reply = f"Who am I remembering this for{household.choice()}?"
        log.info("Memory pending disambiguation for conv %s: %s", t.conversation_id, fact)
    return _speak(t, reply)


def _handle_memory_forget_content(t):
    keyword, delete_all = parse_forget_content(t.transcript)
    person = extract_query_person(t.transcript) or t.speaker
    if not person:
        _pending_memories[t.conversation_id] = {
            "fact": "__forget_content__",
            "keyword": keyword,
            "delete_all": delete_all,
            "expires": time.time() + 60,
        }
        reply = f"Forget whose memory{household.choice()}?"
        log.info(
            "Forget-content pending for conv %s: keyword=%s all=%s",
            t.conversation_id, keyword, delete_all,
        )
    else:
        deleted = B.memory_store.delete_by_content(person, keyword, delete_all)
        if deleted:
            if delete_all:
                reply = f"Done, I've forgotten {len(deleted)} memories about {keyword} for {person}."
            else:
                reply = f"Done, I've forgotten that {person} {deleted[0]['raw']}."
            log.info(
                "Forget-content: deleted %d entries for %s matching '%s'",
                len(deleted), person, keyword,
            )
        else:
            reply = f"I don't have any memories about {keyword} for {person}."
    return _speak(t, reply)


def _handle_memory_forget_last(t):
    person = extract_query_person(t.transcript) or t.speaker
    if not person:
        _pending_memories[t.conversation_id] = {
            "fact": "__forget_last__",
            "expires": time.time() + 60,
        }
        reply = f"Forget whose last memory{household.choice()}?"
    else:
        deleted = B.memory_store.delete_last(person)
        reply = (
            f"Done, I've forgotten that {person} {deleted['raw']}."
            if deleted
            else f"I don't have anything recent to forget for {person}."
        )
    return _speak(t, reply)


def _handle_medicine_query(t):
    person = extract_query_person(t.transcript) or t.speaker
    if not person:
        _pending_memories[t.conversation_id] = {
            "fact": "__medicine_query__",
            "expires": time.time() + 60,
        }
        reply = f"Who{household.choice()}?"
        log.info("Medicine query pending for conv %s", t.conversation_id)
    else:
        reply = _answer_medicine_query(person)
        log.info("Medicine query answered directly for %s", person)
    return _speak(t, reply)


_LIST_LABELS = {"shopping": "shopping list", "todo": "to-do list"}


def _list_label(name: str) -> str:
    return _LIST_LABELS.get(name, f"{name} list")


def _speak_items(items: list[str]) -> str:
    """'milk, eggs and bread' — Alexa-style spoken list join."""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _handle_list_add(t):
    list_name, items = parse_list_add_request(t.transcript)
    added = B.list_store.add(list_name, items)
    label = _list_label(list_name)
    if not added:
        reply = f"That's already on your {label}."
    else:
        reply = f"Added {_speak_items(added)} to your {label}."
    log.info("List add: %s -> %s (added=%s)", list_name, items, added)
    return _speak(t, reply)


def _handle_list_query(t):
    list_name = extract_list_query_name(t.transcript)
    items = B.list_store.read(list_name)
    label = _list_label(list_name)
    if not items:
        reply = f"Your {label} is empty."
    else:
        reply = f"On your {label}: {_speak_items(items)}."
    log.info("List query: %s -> %d item(s)", list_name, len(items))
    return _speak(t, reply)


def _handle_list_remove(t):
    list_name, item = parse_list_remove_request(t.transcript)
    label = _list_label(list_name)
    if item is None:
        count = B.list_store.clear(list_name)
        reply = (
            f"Cleared {count} item{'s' if count != 1 else ''} from your {label}."
            if count else f"Your {label} was already empty."
        )
    else:
        removed = B.list_store.remove(list_name, item)
        reply = (
            f"Removed {removed} from your {label}."
            if removed else f"I don't see {item} on your {label}."
        )
    log.info("List remove: %s item=%s", list_name, item)
    return _speak(t, reply)


def _kind_word(text: str) -> str | None:
    """The one entry kind a request names ('timer', 'alarm', 'reminder'), if exactly one."""
    kinds = set(re.findall(r"\b(timer|alarm|reminder)s?\b", text.lower()))
    return kinds.pop() if len(kinds) == 1 else None


def _ask_which(t, action, rows, active, seconds=0.0):
    """Several entries match: ask, and hold the candidates for the answer."""
    rows = sorted(rows, key=lambda r: r["created_at"])
    _pending_timer_choice[t.conversation_id] = {
        "action": action, "ids": [r["id"] for r in rows], "seconds": seconds,
        "expires": time.time() + 60,
    }
    desc = _describe(active)
    bases = {_base_desc(r) for r in rows}
    n = _COUNT_SPOKEN.get(len(rows), str(len(rows)))
    if len(bases) == 1:
        base = bases.pop()
        lead = (f"You have {n} {base}s. " if base.endswith(("timer", "alarm"))
                else f"You have {n} of those. ")
        opts = [f"the {_ORDINAL_SPOKEN[min(i, 5)]}, " + (
                    f"at {_spoken_alarm(r)}" if r["kind"] == "alarm"
                    else f"with {_round_remaining(r['remaining_s'])} left")
                for i, r in enumerate(rows)]
    else:
        lead = ""
        opts = [f"the {desc[r['id']]}" for r in rows]
    if action == "cancel":
        opts.append("both" if len(rows) == 2 else "all of them")
    joined = ", ".join(opts[:-1]) + (", or " if len(opts) > 2 else " or ") + opts[-1]
    log.info("Timer %s: %d candidates, asking which", action, len(rows))
    return _speak(t, f"{lead}Which one? {joined[:1].upper()}{joined[1:]}?")


def _do_cancel(t, chosen, active):
    desc = _describe(active)
    B.timers.cancel_ids([r["id"] for r in chosen])
    if len(chosen) == 1:
        return _speak(t, f"Cancelled the {desc[chosen[0]['id']]}.")
    return _speak(t, "Cancelled both." if len(chosen) == 2 else f"Cancelled all {len(chosen)}.")


def _do_add(t, row, seconds, active):
    remaining = B.timers.add_time_id(row["id"], seconds)
    if remaining is None:
        return _speak(t, "That timer already finished.")
    _note_timer(t.conversation_id, row)
    name = _timer_name(row, active, _describe(active))
    log.info("Timer add: +%ds to %s", seconds, row["label"])
    return _speak(t, f"Added {format_remaining(seconds)}. "
                     f"{name[:1].upper()}{name[1:]} now has {_round_remaining(remaining)} left.")


def _handle_timer_cancel(t):
    cancel_type, phrase = parse_timer_cancel_request(t.transcript)
    kind = _kind_word(t.transcript)
    active = B.timers.list_active()
    if cancel_type == "all":
        rows = [r for r in active if not kind or r["kind"] == kind]
        if not rows:
            return _speak(t, f"You don't have any {kind}s set." if kind
                          else "You don't have any timers or alarms set.")
        return _do_cancel(t, rows, active)
    ref = parse_timer_ref(phrase, kind)
    rows = _select_timers(active, ref, t.conversation_id)
    log.info("Timer cancel: ref=%s -> %d match(es)", ref, len(rows))
    if len(rows) == 1:
        return _do_cancel(t, rows, active)
    if not rows:
        if not active:
            return _speak(t, "You don't have any timers or alarms set.")
        return _speak(t, f"I couldn't find {_ref_phrase(ref)}.")
    return _ask_which(t, "cancel", rows, active)


def _handle_timer_add(t):
    parsed = parse_add_time_request(t.transcript)
    if not parsed:
        return None
    seconds, phrase = parsed
    active = B.timers.list_active()
    ref = parse_timer_ref(phrase)
    ref.kind = None if ref.kind == "alarm" else ref.kind
    rows = _select_timers([r for r in active if r["kind"] != "alarm"], ref, t.conversation_id)
    if not rows:
        return _speak(t, "I couldn't find a running timer." if ref.empty()
                      else f"I couldn't find {_ref_phrase(ref)}.")
    if len(rows) > 1 and phrase == "it":
        # "add a minute to it" — the one we were just talking about
        last = _last_timer_ref.get(t.conversation_id)
        pick = [r for r in rows if last and r["id"] == last["tid"]]
        rows = pick or rows
    if len(rows) > 1:
        return _ask_which(t, "add", rows, active, seconds)
    return _do_add(t, rows[0], seconds, active)


def _handle_timer_reset(t):
    reset_label = parse_timer_reset_request(t.transcript)
    reset = B.timers.reset(reset_label)
    if not reset:
        return _speak(t, "I don't have a timer to restart.")
    # "25 minutes timer" is auto-generated — just say "the timer".
    name = "timer" if re.match(r"^[\d\s]*(hour|minute|second)", reset) else reset
    log.info("Timer reset: label=%r -> %s", reset_label, reset)
    return _speak(t, f"Restarted the {name}.")


def _handle_timer_status(t):
    ref = parse_timer_status_ref(t.transcript)
    if (ref.name and ref.seconds is None and ref.hour is None and ref.ordinal is None
            and not ref.other and not _kind_word(t.transcript)
            and not _select_timers(B.timers.list_active(), ref, t.conversation_id)):
        # "how much time is left in the game" — not one of ours; list them all
        ref = TimerRef()
    reply = format_timer_status_reply(ref, t.conversation_id)
    log.info("Timer status (ref=%s): %s", ref, reply)
    return _speak(t, reply)


_CHOICE_ALL_RE = re.compile(r"\b(?:both|all|every|each)\b", re.IGNORECASE)
_CHOICE_ABORT_RE = re.compile(
    r"\b(?:never\s*mind|nevermind|forget\s+it|neither|none|no\s+thanks?|"
    r"leave\s+(?:it|them)|don'?t\s+cancel|nothing)\b", re.IGNORECASE)


def _handle_pending_timer_choice(t):
    """A previous turn asked 'which one?' — this turn may be the answer
    ('the first one', 'the pasta one', 'the one with 3 minutes left', 'both')."""
    p = _pending_timer_choice.get(t.conversation_id)
    if not (p and time.time() < p["expires"] and t.transcript):
        return None
    text = t.transcript
    route = classify(text)
    if route not in ("llm", "garbage", "timer_cancel", "timer_add", "timer_status"):
        del _pending_timer_choice[t.conversation_id]   # a new request; route it
        return None
    if _CHOICE_ABORT_RE.search(text):
        del _pending_timer_choice[t.conversation_id]
        return _speak(t, "Okay, I'll leave them." if p["action"] == "cancel" else "Okay, never mind.")
    active = B.timers.list_active()
    rows = [r for r in active if r["id"] in p["ids"]]
    if not rows:
        del _pending_timer_choice[t.conversation_id]
        return None
    if route == "timer_cancel":
        typ, phrase = parse_timer_cancel_request(text)
        if typ == "all" and not re.search(r"\bboth\b", text, re.IGNORECASE):
            del _pending_timer_choice[t.conversation_id]   # "cancel all timers" — the real thing
            return None
        text = phrase or text
    ref = TimerRef()
    if p["action"] == "cancel" and _CHOICE_ALL_RE.search(text):
        chosen = rows
    else:
        ref = parse_timer_ref(text)
        ref.kind = None
        chosen = _select_timers(rows, ref, t.conversation_id) if not ref.empty() else []
        if not chosen and ref.seconds is not None:
            # "the one with 3 minutes left" — closest remaining time
            near = min(rows, key=lambda r: abs(r["remaining_s"] - ref.seconds))
            if abs(near["remaining_s"] - ref.seconds) <= 90:
                chosen = [near]
    if not chosen or (len(chosen) > 1 and p["action"] == "add"):
        if ref.empty():
            del _pending_timer_choice[t.conversation_id]   # not an answer; route it
            return None
        return _ask_which(t, p["action"], rows, active, p["seconds"])
    if len(chosen) > 1 and p["action"] == "cancel" and chosen != rows:
        return _ask_which(t, "cancel", chosen, active)
    del _pending_timer_choice[t.conversation_id]
    log.info("Timer choice (%s): %s", p["action"], [r["label"] for r in chosen])
    if p["action"] == "cancel":
        return _do_cancel(t, chosen, active)
    return _do_add(t, chosen[0], p["seconds"], active)


def _handle_snooze(t):
    snooze_seconds = parse_snooze_request(t.transcript)
    label, fired_at = _last_fired_alarm
    recent = label and (time.time() - fired_at) < _SNOOZE_WINDOW_S
    snooze_label = f"{label} snooze" if recent else "snooze alarm"
    spec = _TimerSpec(seconds=snooze_seconds, label=snooze_label, kind="alarm",
                      duration_s=0)
    B.timers.set(spec)
    log.info("Snooze: %s for %ds", snooze_label, snooze_seconds)
    return _speak(t, f"Snoozing for {format_remaining(snooze_seconds)}.")


def _handle_timer_set(t):
    spec = parse_timer_request(t.transcript)
    if not spec:
        return None
    tid = B.timers.set(spec)
    reply = format_set_reply(spec)
    active = B.timers.list_active()
    row = next((r for r in active if r["id"] == tid), None)
    if row:
        _note_timer(t.conversation_id, row)
        twins = [r for r in active if _base_desc(r) == _base_desc(row)]
        if len(twins) > 1 and row["kind"] == "timer":
            # Say which one this is, so "the second one" means something later.
            reply += f" That's your {_describe(active)[tid]}."
    log.info("Timer set: %s", reply)
    return _speak(t, reply)


def _handle_alarm_set(t):
    spec = parse_alarm_request(t.transcript)
    if not spec:
        return None
    B.timers.set(spec)
    reply = format_set_reply(spec)
    log.info("Alarm set: %s", reply)
    return _speak(t, reply)


def _handle_reminder_set(t):
    spec = parse_reminder_request(t.transcript)
    if spec:
        B.timers.set(spec)
        reply = format_set_reply(spec)
        log.info("Reminder set: %s", reply)
        return _speak(t, reply)

    # Under-specified: a day but no time. Ask, and stash the partial parse.
    partial = reminder_missing_time(t.transcript)
    if partial:
        message, day_phrase = partial
        _pending_reminders[t.conversation_id] = {
            "message": message, "day_phrase": day_phrase,
            "expires": time.time() + 60,
        }
        log.info("Reminder time pending for conv %s: %r %s",
                 t.conversation_id, message, day_phrase)
        return _speak(t, f"What time {day_phrase} would you like to be reminded?")
    return None


_REMINDER_ABORT_RE = re.compile(
    r"\b(never\s*mind|nevermind|forget it|cancel|don'?t\s+worry|skip it|no\s+thanks?)\b",
    re.IGNORECASE,
)
_DAYPART_WORD_RE = re.compile(
    r"\b(morning|afternoon|evening|night|noon|midnight)\b", re.IGNORECASE)


def _handle_pending_reminder(t):
    """A previous turn asked 'what time?' — this turn may be the answer."""
    pending = _pending_reminders.get(t.conversation_id)
    if not (pending and time.time() < pending["expires"] and t.transcript):
        return None

    reply = _replace_word_numbers(t.transcript).strip().rstrip(".?!,")
    msg, day = pending["message"], pending["day_phrase"]

    if _REMINDER_ABORT_RE.search(reply):
        del _pending_reminders[t.conversation_id]
        return _speak(t, "Okay, no reminder.")

    # "at 9" / "around 6" / "for 7:30" -> strip the preposition; we re-add "at".
    reply = re.sub(r"^(?:at|on|for|by|around|about|maybe)\s+", "", reply, flags=re.IGNORECASE)

    if _DAYPART_WORD_RE.search(reply):
        combined = f"remind me to {msg} {reply} {day}"
    elif re.search(r"\d", reply):
        combined = f"remind me to {msg} at {reply} {day}"
    else:
        # Not a time — drop the pending reminder and let the turn route normally.
        del _pending_reminders[t.conversation_id]
        return None

    spec = parse_reminder_request(combined)
    del _pending_reminders[t.conversation_id]
    if not spec:
        return _speak(t, "Sorry, I didn't catch a time. Ask me to set the reminder again.")
    B.timers.set(spec)
    reply_txt = format_set_reply(spec)
    log.info("Reminder set (after time prompt): %s", reply_txt)
    return _speak(t, reply_txt)


def _handle_news(t):
    source_filter = extract_news_source(t.transcript)
    topic_filter = extract_news_topic(t.transcript)
    category_filter = extract_news_category(t.transcript)
    if category_filter == "sports":
        # No sports RSS feed is configured — dedicated scores/results now live
        # in the sports skill, which claims a named team or F1 first. A bare
        # "sports news"/"sports headlines" with no team gets pointed there
        # instead of an empty RSS lookup.
        reply = "I don't have general sports headlines, but ask me about a specific team or F1 for scores and results."
        return _speak(t, reply)
    items = B.news_cache.top_items(source_filter, topic_filter, category_filter)
    log.info("News request: source=%s topic=%s category=%s",
             source_filter, topic_filter, category_filter)
    if not items:
        # No headlines to report — answer directly instead of handing the LLM
        # an empty context. Observed failure mode without this guard: asked to
        # confidently fill the gap, it fabricated a detailed, plausible-sounding
        # fake headline and attributed it to the named source.
        if topic_filter:
            reply = f"No headlines found about '{topic_filter}' from your news sources."
        elif category_filter:
            reply = f"No {category_filter} headlines currently available."
        else:
            reply = "No headlines currently available."
        log.info("News: %s", reply)
        return _speak(t, reply)
    _last_news_items[t.conversation_id] = {"items": items, "expires": time.time() + 300}
    t.extra_context = B.news_cache.format_for_prompt(source_filter, topic_filter, category_filter)
    t.max_tokens = settings.NEWS_MAX_TOKENS
    return None


def _handle_substitution(t):
    ingredient = extract_substitution_ingredient(t.transcript)
    sub_query = f"ingredient substitute for {ingredient}"
    try:
        _, results = B.searxng.search(sub_query)
        if results:
            t.extra_context = B.searxng.format_substitution_prompt(ingredient, results)
            t.grounding = t.extra_context
            t.max_tokens = settings.SEARCH_MAX_TOKENS
            log.info("Substitution: ingredient=%r results=%d", ingredient, len(results))
        else:
            log.info("Substitution: ingredient=%r returned no results", ingredient)
    except Exception as e:
        log.warning("SearXNG substitution search failed: %s", e)
    return None


def _run_search(t, query, engines):
    """Query SearXNG and attach snippets as grounded context."""
    live = engines == settings.SEARCH_NEWS_ENGINES
    fresh = bool(re.search(r"\b(19|20)\d{2}\b", query))
    try:
        direct_answer, results, query = B.searxng.search_best(
            query,
            engines=engines,
            count=settings.SEARCH_LIVE_RESULT_COUNT if live else 0,
            wiki=not live,
            categories="news" if live else "general",
            fresh=fresh,
        )
        if direct_answer or results:
            t.extra_context = B.searxng.format_for_prompt(
                query, direct_answer, results, live=live
            )
            t.grounding = t.extra_context
            # "How do I install a Moen 1225 cartridge" can't be answered
            # in 120 tokens without being cut off mid-step.
            t.max_tokens = (
                settings.SEARCH_MAX_TOKENS_LONG
                if _HOWTO_RE.search(t.transcript)
                else settings.SEARCH_MAX_TOKENS
            )
            log.info(
                "Search: query=%r answer=%s results=%d engines=%s",
                query, "yes" if direct_answer else "no", len(results),
                engines or settings.SEARCH_ENGINES,
            )
        else:
            log.info("Search: query=%r returned no results", query)
    except Exception as e:
        log.warning("SearXNG search failed: %s", e)


def _handle_search(t):
    query, engines = build_search_query(t.transcript)
    if query:
        _run_search(t, query, engines)
    return None


_HANDLERS = {
    "volume": _handle_volume,
    "music": _handle_music,
    "govee": _handle_govee,
    "tv": _handle_tv,
    "time_date": _handle_time_date,
    "weather": _handle_weather,
    "display": _handle_display,
    "memory_save": _handle_memory_save,
    "memory_forget_content": _handle_memory_forget_content,
    "memory_forget_last": _handle_memory_forget_last,
    "medicine_query": _handle_medicine_query,
    "list_add": _handle_list_add,
    "list_query": _handle_list_query,
    "list_remove": _handle_list_remove,
    "timer_cancel": _handle_timer_cancel,
    "timer_add": _handle_timer_add,
    "timer_reset": _handle_timer_reset,
    "timer_status": _handle_timer_status,
    "snooze": _handle_snooze,
    "timer_set": _handle_timer_set,
    "alarm_set": _handle_alarm_set,
    "reminder_set": _handle_reminder_set,
    "calc": _handle_calc,
    "sports": _handle_sports,
    "news": _handle_news,
    "substitution": _handle_substitution,
    "search": _handle_search,
    # "memory_query" and "llm" are handled in the LLM tail.
}


# ── Pending-memory disambiguation (pre-route) ────────────────────────────────


def _handle_pending_reply(t):
    """A previous turn asked 'who is this for?' — this turn may be the answer."""
    pending = _pending_memories.get(t.conversation_id)
    if not (pending and time.time() < pending["expires"] and t.transcript):
        return None
    person_reply = None
    m = household.people()["reply"].match(t.transcript)
    if m:
        person_reply = household.canonical(m.group(1))
    if not person_reply:
        person_reply = extract_remember_person(t.transcript)
    if not person_reply:
        return None

    if pending["fact"] == "__forget_last__":
        del _pending_memories[t.conversation_id]
        deleted = B.memory_store.delete_last(person_reply)
        reply = (
            f"Done, I've forgotten that {person_reply} {deleted['raw']}."
            if deleted
            else f"I don't have anything recent to forget for {person_reply}."
        )
    elif pending["fact"] == "__forget_content__":
        del _pending_memories[t.conversation_id]
        keyword = pending.get("keyword", "")
        delete_all = pending.get("delete_all", False)
        deleted = B.memory_store.delete_by_content(person_reply, keyword, delete_all)
        if deleted:
            if delete_all:
                reply = f"Done, I've forgotten {len(deleted)} memories about {keyword} for {person_reply}."
            else:
                reply = f"Done, I've forgotten that {person_reply} {deleted[0]['raw']}."
        else:
            reply = f"I don't have any memories about {keyword} for {person_reply}."
    elif pending["fact"] == "__medicine_query__":
        del _pending_memories[t.conversation_id]
        reply = _answer_medicine_query(person_reply)
    elif pending["fact"] == "__medicine_details__":
        del _pending_memories[t.conversation_id]
        clean = t.transcript.strip().rstrip(".,!?'\"")
        if not re.match(r"^took\s+", clean, re.IGNORECASE):
            clean = f"took {clean}"
        entry = B.memory_store.update_raw(pending["entry_id"], clean)
        if entry:
            ts = datetime.fromisoformat(entry["timestamp"]).strftime("%-I:%M %p")
            reply = f"Got it. I'll remember {person_reply} {entry['raw']} at {ts}."
            _last_memory_topic[t.conversation_id] = {
                "topic": entry["raw"],
                "expires": time.time() + 300,
            }
        else:
            reply = "Sorry, I couldn't update that memory."
    else:
        del _pending_memories[t.conversation_id]
        reply = _save_memory(t, person_reply, pending["fact"])
    return _speak(t, reply)


# ── LLM tail ─────────────────────────────────────────────────────────────────


# Follow-up phrasing itself ("tell", "more", "story", "else"...) rides along
# in every news follow-up and isn't a real signal for which headline is meant
# — filtered out before scoring so it can't dilute or fake a match.
_NEWS_FOLLOWUP_STOPWORDS = {"tell", "more", "story", "stories", "else", "any",
                            "detail", "details", "info", "information", "the"}


def _news_followup_context(t) -> str | None:
    """Re-ground a "tell me more about X" turn in the actual headline(s) just
    read, rather than letting the LLM answer from conversation history alone
    — which is exactly what invented a fake Iran/Saudi backstory and a fake
    OpenAI funding round in testing, with no real facts to draw from."""
    stashed = _last_news_items.get(t.conversation_id)
    if not stashed:
        return None
    items = stashed["items"]
    keywords = [
        kw for kw in (_extract_memory_keywords(t.transcript) or [])
        if kw.lower() not in _NEWS_FOLLOWUP_STOPWORDS
    ]
    best, best_score = None, 0
    if keywords:
        for item in items:
            hay = f"{item['title']} {item['summary']}".lower()
            score = sum(1 for kw in keywords if kw.lower() in hay)
            if score > best_score:
                best, best_score = item, score

    if best is not None:
        matched = [best]
        header = "The story being followed up on:"
        footer = ""
    else:
        # No headline matched what was asked — say so explicitly rather than
        # handing back every unrelated headline and trusting the LLM to
        # notice on its own that none of them fit.
        matched = items
        header = "None of these headlines match what was just asked about:"
        footer = ("\n\nNone of the above relate to the request — say plainly that "
                  "the headlines just read didn't cover it. Don't guess or reach "
                  "for the closest-sounding one.")

    lines = [header]
    for item in matched:
        src = B.news_cache.SOURCE_LABELS.get(item["source"], item["source"].title())
        body = item["summary"] or "(no further detail than this headline)"
        lines.append(f"[{src}] {item['title']} — {body}")
    return "\n".join(lines) + footer


def _llm_tail(t, route):
    """Context injection (memory follow-up, memory query, LLM search router)
    and the streaming LLM → per-sentence TTS → MQTT publish loop."""
    _cleanup_last_topics()
    if is_news_followup(t.transcript) and t.conversation_id in _last_news_items:
        t.extra_context = _news_followup_context(t)
        t.max_tokens = settings.NEWS_MAX_TOKENS
        log.info("News follow-up: %s", t.extra_context.splitlines()[0] if t.extra_context else None)
    elif _FOLLOWUP_RE.search(t.transcript) and t.conversation_id in _last_memory_topic:
        person = extract_query_person(t.transcript) or t.speaker
        last_topic = _last_memory_topic[t.conversation_id]["topic"]
        keywords = _extract_memory_keywords(last_topic) or [last_topic]
        t.extra_context = B.memory_store.format_for_prompt(
            person, days=1, keywords=keywords
        )
        t.max_tokens = 120
        log.info("Memory follow-up: person=%s topic=%s", person or "all", last_topic)
    elif route == "memory_query":
        # extract_query_person only matches an explicit name ("what did Sam
        # take"); a bare "what did I say" names nobody, so speaker ID is what
        # actually answers "I" correctly instead of searching every person.
        person = extract_query_person(t.transcript) or t.speaker
        keywords = _extract_memory_keywords(t.transcript)
        t.extra_context = B.memory_store.format_for_prompt(
            person, days=1, keywords=keywords
        )
        t.max_tokens = 120
        log.info("Memory query: person=%s keywords=%s", person or "all", keywords)

    # The regex layers passed on this one; ask the router model (the long
    # tail — ratings, prices, part numbers, how-to). Costs ~0.5s when it runs.
    if (
        route == "llm"
        and settings.SEARCH_ENABLED
        and t.extra_context is None
        and B.route_search_with_llm is not None
    ):
        routed = B.route_search_with_llm(t.transcript)
        if routed:
            query = routed
            engines = (
                settings.SEARCH_NEWS_ENGINES
                if _SEARCH_TEMPORAL_RE.search(routed) or _EVENT_WHEN_RE.search(routed)
                else None
            )
            # The router is told not to invent a year (it guessed wrong ones);
            # pin it here instead, where the rule is deterministic.
            if needs_fresh_results(t.transcript) or needs_fresh_results(routed):
                query = pin_year(routed)
            log.info("Search router: %r -> %r", t.transcript, query)
            _run_search(t, query, engines)

    # LLM streaming → TTS → MQTT publish. The first sentence is synthesized and
    # published on its own for fast first audio; the rest of the response is
    # held and synthesized as a single utterance so its prosody stays continuous
    # (per-sentence synthesis resets pitch and drops the falling final
    # intonation, which makes a multi-sentence answer sound disjointed).
    settings.mqtt_publish("antigua/status", {"state": "thinking"})
    t_llm = time.time()
    response_parts = []
    first_sentence = True
    server_base = B.audio_url_base()

    def _publish(text_chunk):
        wav_path = B.synthesize(text_chunk, lang=t.language)
        if wav_path:
            audio_url = f"{server_base}/audio/{Path(wav_path).name}"
            if not t.quiet:
                settings.mqtt_publish(t.play_topic, {"audio_url": audio_url})
            log.info("Chunk published: %s", text_chunk[:60])

    tail_parts = []
    for sentence in B.ask_llm_stream(
        t.transcript,
        conversation_id=t.conversation_id,
        extra_context=t.extra_context,
        max_tokens_override=t.max_tokens,
        # Answering from snippets is a grounding task — creativity here shows up
        # as invented dates and opponents, not as personality.
        temperature_override=0.1 if t.grounding else None,
        grounding_context=t.grounding,
        language=t.language,
    ):
        response_parts.append(sentence)
        if first_sentence:
            settings.mqtt_publish("antigua/status", {"state": "speaking"})
            first_sentence = False
            _publish(sentence)
        else:
            tail_parts.append(sentence)

    if tail_parts:
        _publish(" ".join(tail_parts))

    llm_time = round(time.time() - t_llm, 2)

    if response_parts:
        response = " ".join(response_parts)
        log.info("LLM stream [%.2fs]: %s", llm_time, response)
        return _resp(t, response, None, streaming=True, llm_time=llm_time)

    # Fallback if LLM yielded nothing
    response = "Hmm, I'm not quite sure about that one."
    wav_path = B.synthesize(response)
    return _resp(t, response, wav_path, llm_time=llm_time)


# ── Entry point ──────────────────────────────────────────────────────────────


def run_pipeline(audio_path, conversation_id="", follow_up=False, play_topic="antigua/play"):
    # 1. STT (speaker ID runs in a thread parallel to it — both read the same
    # WAV, so on a capable backend this costs approximately zero wall-clock
    # time; on the fallback, B.identify_speaker is None and nothing runs).
    settings.mqtt_publish("antigua/status", {"state": "transcribing"})
    speaker_result = {}
    speaker_thread = None
    if B.identify_speaker is not None:
        def _id_speaker():
            try:
                speaker_result["person"] = B.identify_speaker(audio_path)
            except Exception:
                log.warning("Speaker ID failed, falling through to disambiguation", exc_info=True)
                speaker_result["person"] = None
        speaker_thread = Thread(target=_id_speaker, daemon=True)
        speaker_thread.start()

    _cleanup_last_topics()
    prefer = _conv_language.get(conversation_id, {}).get("lang")
    stt_result = B.transcribe(audio_path, prefer=prefer) if prefer else B.transcribe(audio_path)
    language = stt_result.get("language", "en")
    if stt_result.get("lang_probs"):
        log.info("STT language: %s %s", language, stt_result["lang_probs"])
    transcript = strip_wake_prefix(correct_transcript(stt_result["text"]))
    if language == "en":   # medium.en is English-only
        transcript = _second_opinion(audio_path, transcript)

    if speaker_thread is not None:
        speaker_thread.join(timeout=5.0)  # generous — extraction is normally <0.5s

    return dispatch_text(
        transcript,
        conversation_id=conversation_id,
        follow_up=follow_up,
        speaker=speaker_result.get("person"),
        stt_time=stt_result["time_s"],
        confidence=stt_result.get("confidence", 1.0),
        play_topic=play_topic,
        audio_path=audio_path,
        language=language,
    )


def pick_language(probs, text, prefer=None, min_prob=0.85, min_words=3):
    """"en" or "es" from Whisper's language probabilities. Spanish needs clear
    evidence (a confident, several-word clip) unless the conversation is
    already in Spanish; anything else, Turkish included, is English."""
    p_es, p_en = probs.get("es", 0.0), probs.get("en", 0.0)
    if p_es <= p_en:
        return "en"
    if prefer == "es":
        return "es"
    return "es" if p_es >= min_prob and len(text.split()) >= min_words else "en"


_UNMATCHED = ("llm", "search", "garbage")


def _second_opinion(audio_path, transcript):
    """Over music, small.en turns one-word commands into nonsense ("Pause" →
    "Follow us", answered by the LLM). When music is playing and a short
    transcript matches no skill, ask medium.en (~2s); keep its version only
    if that one does match a skill."""
    if (B.transcribe_careful is None or B.music is None or not transcript.strip()
            or len(transcript.split()) > 4 or classify(transcript) not in _UNMATCHED
            or not B.music.playing()):
        return transcript
    try:
        better = strip_wake_prefix(correct_transcript(B.transcribe_careful(audio_path)["text"]))
    except Exception as e:
        log.warning("STT second opinion failed: %s", e)
        return transcript
    if better.strip() and classify(better) not in _UNMATCHED:
        log.info("STT second opinion: %r -> %r", transcript, better)
        return better
    log.info("STT second opinion kept %r (medium heard %r)", transcript, better)
    return transcript


def dispatch_text(transcript, conversation_id="", follow_up=False, *,
                  speaker=None, stt_time=0.0, confidence=1.0, quiet=False,
                  play_topic="antigua/play", audio_path=None, language="en"):
    """Everything after STT: garbage filter, classify, handler dispatch, LLM
    tail. Split out so a text request (tests, the /pipeline_text debug
    endpoint) drives the exact same path a spoken turn does. quiet=True
    suppresses antigua/play so a dry run stays silent on the real satellite."""
    # Assign conversation_id early — needed for pending-memory check before garbage filter
    if not conversation_id:
        conversation_id = os.urandom(8).hex()

    # A Spanish command runs as the English one the skills understand, and the
    # turn is Spanish from here on whatever STT detection said: a two-word
    # "Siguiente canción" is too short to detect but plainly Spanish.
    english = to_english_command(transcript) if transcript else None
    if english:
        log.info("Spanish command: %r -> %r", transcript, english)
        transcript, language = english, "es"

    t = _Turn(transcript, conversation_id, stt_time, speaker=speaker, quiet=quiet,
              play_topic=play_topic, audio_path=audio_path, language=language)
    if language == "es":
        _conv_language[conversation_id] = {"lang": "es", "expires": time.time() + _CONV_LANGUAGE_TTL}
    else:
        _conv_language.pop(conversation_id, None)

    # Pending memory BEFORE garbage filter — a bare one-word name would be rejected
    result = _handle_pending_reply(t)
    if result is not None:
        return result

    # Pending reminder time ("what time tomorrow?") — also before the garbage
    # filter, since a bare "six" is a valid answer.
    result = _handle_pending_reminder(t)
    if result is not None:
        return result

    # Pending "which timer?" — before the garbage filter too ("both", "first").
    result = _handle_pending_timer_choice(t)
    if result is not None:
        return result

    if not transcript or is_garbage_transcript(transcript):
        if follow_up or not transcript.strip():
            # VAD-opened follow-up window caught background noise (TV etc.),
            # or Whisper heard no words at all (a false wake word on music).
            # End the conversation silently — "didn't catch that" would be noise.
            log.info("Empty/garbage transcript — ending silently")
            t.transcript = ""
            return _resp(t, "", "", end_conversation=True)
        reply = random.choice(_DIDNT_CATCH)
        wav_path = B.synthesize(reply)
        t.transcript = ""
        return _resp(t, reply, wav_path)

    log.info("STT [%.2fs]: %s", stt_time, transcript)
    settings.mqtt_publish(
        "antigua/transcript",
        {"text": transcript, "confidence": confidence},
    )

    # 2. Dispatch on the classified route. Handlers return a response dict, or
    # None to continue into the LLM tail (with any context they attached).
    route = classify(transcript)
    # Weather-classifier miss log (grep 'weather_intent_miss') — a weather-ish
    # turn the routing regexes didn't claim, so it fell to the LLM. Review these
    # to widen _WEATHER_ROUTE_RE toward how the household actually phrases things.
    if route not in ("weather", "garbage") and _WEATHER_TOPIC_RE.search(transcript):
        log.info("weather_intent_miss: %r (routed %s)", transcript, route)
    handler = _HANDLERS.get(route)
    if handler is not None:
        result = handler(t)
        if result is not None:
            return result

    # 3. LLM tail: context injection + streaming response
    return _llm_tail(t, route)
