"""Timers, alarms, snooze and reminders: durations, clock times, days, labels."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import stores as _stores
from ..stores import TimerSpec


_TIMER_STATUS_RE = re.compile(
    r"how\s+(?:much\s+time|long)\s+(?:is\s+)?(?:left|remaining|remain|do\s+i\s+have)"
    r"|how\s+(?:much|long).{0,20}?\bon\s+(?:my\s+|the\s+)?[\w ]{1,20}?(?:timer|alarm)"
    r"|time\s+(?:left|remaining)\s+on"
    r"|(?:check|status\s+of|what.?s\s+(?:left\s+on|on))\s+(?:my\s+|the\s+)?(?:timer|alarm)"
    r"|(?:timer|alarm)\s+status"
    r"|what\s+timers?\s+(?:are\s+)?(?:running|active|set|going|left)"
    r"|(?:do\s+i\s+have|are\s+there)\s+any\s+(?:timers?|alarms?|reminders?)"
    r"|when(?:'s| is| does)\s+(?:my\s+|the\s+)?(?:next\s+)?(?:alarm|reminder)"
    r"|what\s+(?:are\s+|'s\s+|is\s+)?(?:my\s+|the\s+)?reminders?\b"
    r"|what\s+(?:am\s+i|do\s+i\s+need)\s+.{0,25}?\bremind"
    r"|(?:list|show)\s+(?:my\s+)?(?:timers?|alarms?|reminders?)"
    # "how long until my gym alarm", "how much time until my 8 AM alarm"
    r"|how\s+(?:much\s+time|long)\s+(?:is\s+(?:there\s+)?)?(?:until|till|til|before)\s+"
    r"(?:my\s+|the\s+)?.{0,25}?\b(?:timer|alarm|reminder)"
    # "when is my gym alarm", "when does the pasta timer go off"
    r"|when(?:'s|\s+is|\s+does|\s+will)\s+(?:my\s+|the\s+).{0,25}?\b(?:timer|alarm|reminder)"
    # "how long on the second one", "how much time on the other one"
    r"|how\s+(?:much\s+time|long)\s+(?:is\s+)?(?:left\s+)?(?:on|for)\s+(?:the\s+|my\s+)?"
    r"(?:first|second|third|fourth|last|other|older|newer)\s+one\b"
    # "what timers do I have", "which alarms are set"
    r"|(?:what|which)\s+(?:timers?|alarms?|reminders?)\s+(?:do\s+i\s+have|are\s+there|have\s+i)",
    re.IGNORECASE,
)
# Is this an alarm request at all?
_ALARM_TRIGGER_RE = re.compile(
    r"\b(?:set\s+(?:an?\s+)?alarm|wake\s+me(?:\s+up)?|alarm\s+(?:for|at)|"
    r"(?:every|each)\s+(?:morning|day|night|evening|weekday|weekend|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?)",
    re.IGNORECASE,
)
# Clock time anywhere in the request. Words may sit between the trigger and it
# ("alarm for every weekday at 7").
_ALARM_RE = re.compile(
    r"\b(?:at\s+|for\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|o'?clock)?"
    r"(?:\s+(?:on\s+|next\s+)?(tomorrow|today|tonight|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday))?",
    re.IGNORECASE,
)
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def resolve_day_offset(word, now):
    """Days from `now` to a spoken day-word. Shared by the alarm parser and the
    weather timeframe resolver so 'this Thursday' means the same thing to both.

    'today'/'tonight'/part-of-day -> 0, 'tomorrow' -> 1, a weekday name -> its
    next occurrence (0 when it is today). None if the word isn't a day.
    """
    w = (word or "").lower().strip()
    if w in ("today", "tonight", "this morning", "this afternoon", "this evening"):
        return 0
    if w == "tomorrow":
        return 1
    if w in _WEEKDAYS:
        return (_WEEKDAYS[w] - now.weekday()) % 7
    return None
_SNOOZE_RE = re.compile(
    r"(?:snooze\s+(?:for\s+)?|give\s+me\s+(?:a\s+)?)(\d+)\s*(second|minute|hour)s?",
    re.IGNORECASE,
)
_TIMER_RESET_RE = re.compile(
    r"(?:reset|restart)\s+(?:my\s+|the\s+)?([a-z]+)\s+(?:timer|alarm)"
    r"|(?:reset|restart)\s+(?:my\s+|the\s+)?(?:timer|alarm)\b",
    re.IGNORECASE,
)

_SKIP_NAMES = frozenset(
    ["the", "a", "an", "my", "this", "that", "one", "big", "small", "quick"]
)

_NAMED_SUFFIX_RE = re.compile(
    r"\b(?:named|called)\s+([a-z][a-z0-9\s]*?)"
    r"(?=\s+for\s+\d"
    r"|\s+(?:tomorrow|today|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|[\s.,!?]*$)",
    re.IGNORECASE,
)


def _extract_name(text):
    """Pull an explicit 'named X' / 'called X' name out of text.

    Returns (name_or_None, text_with_that_phrase_removed).
    """
    m = _NAMED_SUFFIX_RE.search(text)
    if not m:
        return None, text
    name = m.group(1).strip().lower()
    if not name or name in _SKIP_NAMES:
        return None, text
    return name, (text[:m.start()] + text[m.end():]).strip()


_WORD_TO_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}


def _replace_word_numbers(text: str) -> str:
    """Replace spelled-out numbers like 'five' with digits so timer regexes match."""
    # "five minutes" -> "5 minutes"
    # "twenty five" -> "25" (compound)
    # "two hours" -> "2 hours"
    # Only replace whole words that aren't part of other words.
    _tens = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy",
             "eighty", "ninety")
    _ones = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")

    # Compound first: "twenty five" / "twenty-five" -> "25"
    text = re.sub(
        r"\b(" + "|".join(_tens) + r")[\s-]+(" + "|".join(_ones) + r")\b",
        lambda m: str(_WORD_TO_NUM[m.group(1).lower()] + _WORD_TO_NUM[m.group(2).lower()]),
        text,
        flags=re.IGNORECASE,
    )
    # "an hour and a half" keeps its words for the fuzzy matcher; "and a half"
    # after a bare number becomes ".5": "2 and a half hours" -> "2.5 hours"
    text = re.sub(r"\b(\d+)\s+and\s+a\s+half\b", r"\1.5", text, flags=re.IGNORECASE)

    def _repl(m):
        return str(_WORD_TO_NUM.get(m.group(1).lower(), m.group(1)))

    text = re.sub(
        r"\b(" + "|".join(re.escape(w) for w in _WORD_TO_NUM) + r")\b",
        _repl,
        text,
        flags=re.IGNORECASE,
    )
    return text


_UNIT_SECONDS = {
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
}
_DURATION_TERM_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(hours?|hrs?|minutes?|mins?|seconds?|secs?|h|m|s)\b",
    re.IGNORECASE,
)
# "half an hour", "a quarter of an hour", "an hour and a half", "a minute and a half"
_FUZZY_DURATION = [
    (re.compile(r"\b(?:an?\s+)?hour\s+and\s+a\s+half\b", re.I), 5400),
    (re.compile(r"\bhalf\s+(?:an?\s+)?hour\b", re.I), 1800),
    (re.compile(r"\b(?:a\s+)?quarter\s+(?:of\s+)?(?:an?\s+)?hour\b", re.I), 900),
    (re.compile(r"\bthree\s+quarters\s+of\s+an\s+hour\b", re.I), 2700),
    (re.compile(r"\b(?:a\s+)?minute\s+and\s+a\s+half\b", re.I), 90),
    (re.compile(r"\bhalf\s+a\s+minute\b", re.I), 30),
    (re.compile(r"\b(?:an?\s+)?hour\b", re.I), 3600),
    (re.compile(r"\b(?:a\s+)?minute\b", re.I), 60),
]


def resolve_duration(text):
    """Total seconds for any duration phrasing in `text`, or None.

    Sums every "<n> <unit>" term ("1 hour 30 minutes" -> 5400) and understands
    the common fuzzy forms ("half an hour", "an hour and a half")."""
    text = _replace_word_numbers(text)
    total = 0.0
    for m in _DURATION_TERM_RE.finditer(text):
        unit = m.group(2).lower()
        total += float(m.group(1)) * _UNIT_SECONDS.get(unit, _UNIT_SECONDS.get(unit[0], 0))
    if total > 0:
        return total
    for rx, secs in _FUZZY_DURATION:
        if rx.search(text):
            return float(secs)
    return None


_TIMER_NAME_RES = [
    # "named X" / "called X" (already stripped by _extract_name, kept for safety)
    re.compile(r"\b(?:named|called)\s+(?:the\s+|my\s+)?([a-z][a-z ]{0,24}?)"
               r"(?=\s+(?:for|in|timer)\b|[\s.,!?]*$)", re.I),
    # "... timer for the pasta" / "... for my laundry"
    re.compile(r"\btimer\s+(?:for|called|named)\s+(?:the\s+|my\s+)?"
               r"([a-z][a-z ]{0,24}?)(?=\s+(?:please|now|thanks)\b|[\s.,!?]*$)", re.I),
    # "the pasta timer" / "a 10 minute laundry timer" — word(s) right before "timer"
    re.compile(r"\b(?:for\s+|set\s+|start\s+)?(?:a|an|the|my)?\s*"
               r"(?:\d+(?:\.\d+)?\s*(?:hours?|minutes?|seconds?|mins?|secs?)\s+)?"
               r"([a-z][a-z ]{0,24}?)\s+timer\b", re.I),
]
_NAME_STOP = _SKIP_NAMES | {
    "set", "start", "give", "me", "another", "second", "seconds", "minute",
    "minutes", "hour", "hours", "new", "countdown", "count", "down", "please",
    "and", "half", "quarter", "for", "in", "of",
}


def _extract_timer_name(text):
    """Best-effort user name for a timer ('pasta', 'banana bread'), or None."""
    for rx in _TIMER_NAME_RES:
        m = rx.search(text)
        if not m:
            continue
        raw = re.sub(r"\s+", " ", m.group(1).strip().lower())
        if resolve_duration(raw):           # "an hour and a half" is not a name
            continue
        name = " ".join(w for w in raw.split() if w not in _NAME_STOP)
        if name and not name.isdigit() and name not in _NAME_STOP:
            return name
    return None


def parse_timer_request(text):
    """Return a TimerSpec for a timer/reminder request, or None."""
    norm = _replace_word_numbers(text)
    seconds = resolve_duration(norm)
    if seconds is None or seconds <= 0:
        return None
    # Must actually look like a timer/reminder, not just any sentence with "5
    # minutes" in it. classify()'s route order already filters a lot; this is
    # the last guard.
    if not re.search(r"\b(timer|countdown|count\s*down|remind\s+me|"
                     r"set\s+(?:a|an|the)\b|start\s+(?:a|an|the)\b|in\s+\d)", norm, re.I):
        return None
    name = _extract_timer_name(norm)
    is_reminder = bool(re.search(r"\bremind\s+me\b", norm, re.I)) and not name
    kind = "reminder" if is_reminder else "timer"
    if name:
        label = f"{name} timer"
    elif is_reminder:
        label = "reminder"
    else:
        label = _humanize_seconds(seconds) + " timer"
    return TimerSpec(seconds=seconds, label=label, kind=kind, name=name)


def _humanize_seconds(seconds):
    s = int(round(seconds))
    parts = []
    for unit, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        n, s = divmod(s, size)
        if n:
            parts.append(f"{n} {unit}{'s' if n != 1 else ''}")
    return " ".join(parts) or "0 seconds"


_CANCEL_VERB = r"(?:cancel|stop|delete|remove|clear|kill|turn\s+off)"
_TIMER_NOUN = r"(?:timers?|alarms?|reminders?)"
_PICK_ONE = r"(?:first|second|third|fourth|last|other|older|oldest|newer|newest|earlier|later)\s+one"


def parse_timer_cancel_request(text):
    """Return ('all', None) or ('label', ref_phrase) or None. The ref phrase
    ('pasta', '5 minute', 'first', '' for "cancel the timer") goes to
    parse_timer_ref; the handler asks which one when it matches several."""
    # "cancel all timers", "stop every alarm", "clear my timers", "cancel both timers"
    if re.search(_CANCEL_VERB + r"\s+(?:all|every|my|both)\s+(?:(?:of\s+)?(?:the\s+|my\s+)?)?"
                 + _TIMER_NOUN + r"(?:\s+and\s+(?:alarms?|reminders?))?", text, re.IGNORECASE):
        return "all", None
    if re.search(_CANCEL_VERB + r"\s+(?:everything|both(?:\s+of\s+them)?|all\s+of\s+(?:them|it)|"
                 r"the\s+rest)\b", text, re.IGNORECASE):
        return "all", None
    # "cancel the first one", "delete the other one" — not "turn off"/"stop",
    # which say "the other one" about lights and music too
    m = re.search(r"(?:cancel|delete|kill)\s+(?:the\s+|my\s+)?(" + _PICK_ONE + r")\b",
                  text, re.IGNORECASE)
    if m:
        return "label", m.group(1).lower()
    # "cancel my pasta timer", "stop the 7 AM alarm", "cancel the dentist reminder"
    m = re.search(_CANCEL_VERB + r"\s+(?:my\s+|the\s+)?(.+?)\s+" + _TIMER_NOUN + r"\b",
                  text, re.IGNORECASE)
    if m:
        return "label", re.sub(r"\s+", " ", m.group(1).strip().lower())
    # "cancel the timer", "stop the alarm" — generic
    if re.search(_CANCEL_VERB + r"\s+(?:the\s+|that\s+|my\s+)?" + _TIMER_NOUN + r"\b",
                 text, re.IGNORECASE):
        return "label", ""
    return None


_ADD_TIME_RE = re.compile(
    r"\b(?:add|give\s+it|tack\s+on|extend\s+(?:it|the\s+timer)\s+(?:by)?)\s+"
    r"(.+?)\s+(?:more\s+)?(?:to\s+(?:the\s+|my\s+)?(.+?)\s*(?:timer)?)?$",
    re.IGNORECASE,
)


def parse_add_time_request(text):
    """'add 2 minutes to the 5 minute timer' -> (120, '5 minute'), or None.
    The amount comes only from before 'to', so the target's own length
    ('5 minute') isn't summed into it."""
    if not re.search(r"\b(?:add|extend|tack\s+on|give\s+it)\b", text, re.IGNORECASE):
        return None
    if not re.search(r"\btimer\b|\bit\b|\bone\b", text, re.IGNORECASE):
        return None
    amount, target = text, ""
    m = re.search(r"\bto\s+(?:the\s+|my\s+)?(.+?)\s*(?:timer|one)?[\s.?!]*$", text, re.IGNORECASE)
    m2 = re.search(r"\bextend\s+(?:the\s+|my\s+)?(.*?)\s*(?:timer|one)\s+by\s+(.+)$", text, re.IGNORECASE)
    if m2:
        target, amount = m2.group(1), m2.group(2)
    elif m:
        amount, target = text[:m.start()], m.group(1)
    secs = resolve_duration(re.sub(r"\s+more\b", "", amount, flags=re.IGNORECASE))
    if not secs:
        return None
    target = target.strip().lower()
    if target in ("it", "that", "this") or (not target and re.search(r"\bgive\s+it\b", text, re.IGNORECASE)):
        target = "it"            # the timer we were just talking about
    return secs, target


def parse_snooze_request(text):
    """Return seconds or None."""
    m = _SNOOZE_RE.search(text)
    if m:
        return int(m.group(1)) * {"second": 1, "minute": 60, "hour": 3600}[m.group(2).lower()]
    # Bare "snooze" without time
    if re.search(r"\bsnooze\b", text, re.IGNORECASE):
        return 300  # default 5 minutes
    return None


def parse_timer_status_request(text):
    """Return True if user is asking about timer status."""
    return bool(_TIMER_STATUS_RE.search(text))


def parse_timer_reset_request(text):
    """Return a label substring ('' = any) if the user asked to reset/restart, else None."""
    m = _TIMER_RESET_RE.search(text)
    if not m:
        return None
    name = (m.group(1) or "").lower()
    if name and name not in _SKIP_NAMES:
        return name
    return ""

@dataclass
class TimerRef:
    """Which timer/alarm/reminder a request points at. Every field is an
    optional filter; all empty means "whichever one there is"."""
    kind: str | None = None        # "timer" | "alarm" | "reminder"
    name: str | None = None        # "pasta", "gym", "dentist" (name/label/message)
    seconds: float | None = None   # the length it was set for ("the 5 minute timer")
    hour: int | None = None        # clock ("my 8 AM alarm")
    minute: int = 0
    meridiem: bool = False         # am/pm said, so hour is exact rather than mod 12
    ordinal: int | None = None     # 0 first set, 1 second, ..., -1 last set
    other: bool = False            # "the other one": not the one we last talked about

    def empty(self) -> bool:
        return (self.name is None and self.seconds is None and self.hour is None
                and self.ordinal is None and not self.other)


_ORDINAL_WORDS = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3, "last": -1, "older": 0, "oldest": 0, "earlier": 0,
    "earliest": 0, "newer": -1, "newest": -1, "later": -1, "latest": -1,
}
_REF_CLOCK_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|o'?clock)"
    r"|\b(\d{1,2}):(\d{2})\b"
    r"|\b(\d{1,2})(?=\s+alarm\b)", re.IGNORECASE)
_REF_STOP = frozenset((
    "the", "a", "an", "my", "your", "this", "that", "one", "ones", "i", "set", "started",
    "made", "for", "on", "of", "to", "in", "at", "with", "it", "is", "was", "are", "left",
    "remaining", "go", "goes", "off", "ring", "rings", "done", "called", "named", "which",
    "timer", "timers", "alarm", "alarms", "reminder", "reminders", "and", "time", "how",
    "much", "long", "until", "till", "before", "when", "what's", "whats", "hey", "antigua",
    "please", "about", "there", "more", "minutes", "minute", "hours", "hour", "seconds",
    "second", "mins", "min", "secs", "sec", "hr", "hrs", "by", "just", "now", "other",
    "half", "quarter",
))


def parse_timer_ref(phrase: str, kind: str | None = None) -> TimerRef:
    """Parse the part of a request that names a timer ('the first 5 minute
    timer', 'my gym alarm', 'my 8 AM alarm', 'eggs', 'the other one')."""
    text = _replace_word_numbers(phrase or "").lower().replace("-", " ")
    ref = TimerRef(kind=kind)
    k = re.search(r"\b(timer|alarm|reminder)s?\b", text)
    if k and not ref.kind:
        ref.kind = k.group(1)
    if re.search(r"\bother\b", text):
        ref.other = True
    words = re.findall(r"[a-z0-9.]+", text)
    for i, w in enumerate(words):
        # "the 30 second timer" is a length, not the second timer
        if w in _ORDINAL_WORDS and not (w == "second" and i and re.match(r"[\d.]+$", words[i - 1])):
            ref.ordinal = _ORDINAL_WORDS[w]
            break
    m = _REF_CLOCK_RE.search(text)
    if m:
        if m.group(1):
            hour, minute, mer = int(m.group(1)), int(m.group(2) or 0), m.group(3).replace(".", "")
            if mer.startswith("p") and hour != 12:
                hour += 12
            elif mer.startswith("a") and hour == 12:
                hour = 0
            ref.meridiem = mer[0] in "ap"
        elif m.group(4):
            hour, minute = int(m.group(4)), int(m.group(5))
        else:
            hour, minute = int(m.group(6)), 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            ref.hour, ref.minute = hour, minute
            text = text[:m.start()] + " " + text[m.end():]
    ref.seconds = resolve_duration(text) if re.search(r"\d|hour|minute", text) else None
    if ref.seconds is not None:
        text = _DURATION_TERM_RE.sub(" ", text)
    words = [w for w in re.findall(r"[a-z][a-z']*", text)
             if w not in _REF_STOP and w not in _ORDINAL_WORDS]
    if words:
        ref.name = " ".join(words)
    return ref


_STATUS_TARGET_RES = [
    # "time left on the pasta timer", "how long left on the laundry"
    re.compile(r"\b(?:left|remaining)\s+(?:on|for|in)\s+(.+)$", re.IGNORECASE),
    # "how long until my gym alarm", "when does the pasta timer go off"
    re.compile(r"\b(?:until|till|til|before)\s+(.+)$", re.IGNORECASE),
    re.compile(r"\bwhen(?:'s|\s+is|\s+does|\s+will)\s+(.+)$", re.IGNORECASE),
    # "how much time is on the eggs timer", "how long for the rice"
    re.compile(r"\b(?:on|for|of)\s+((?:my|the)\s+.+)$", re.IGNORECASE),
]


def parse_timer_status_ref(text: str) -> TimerRef:
    """The timer a status question is about; an empty TimerRef means 'all'."""
    kind_words = set(re.findall(r"\b(timer|alarm|reminder)s?\b", text.lower()))
    kind = kind_words.pop() if len(kind_words) == 1 else None
    for rx in _STATUS_TARGET_RES:
        m = rx.search(text.strip().rstrip("?.!"))
        if m:
            target = re.sub(r"\b(?:go(?:es)?\s+off|ring|be\s+done|finish|is\s+set)\b.*$",
                            "", m.group(1), flags=re.IGNORECASE)
            ref = parse_timer_ref(target, kind)
            if ref.kind != kind and kind is None:
                ref.kind = None          # "which" noun came only from the target
            return ref
    return TimerRef(kind=kind)


_REPEAT_RE = re.compile(
    r"\b(?:every|each)\s+"
    r"(day|morning|night|evening|weekday|weekdays|weekend|weekends|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b",
    re.IGNORECASE,
)
_DAILY_WORDS = {"day", "morning", "night", "evening"}


def _parse_repeat(text):
    """Recurrence spec for an alarm, or None. Collects every 'every <X>' clause
    so 'every Monday and Wednesday' -> '0,2'."""
    hits = [m.group(1).lower() for m in _REPEAT_RE.finditer(text)]
    # also "monday and friday" chained after one "every"
    if hits:
        chained = re.findall(
            r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            text, re.IGNORECASE)
        hits += [c.lower() for c in chained]
    if not hits:
        return None
    days = set()
    for h in hits:
        if h in _DAILY_WORDS:
            return "daily"
        if h.startswith("weekday"):
            return "weekdays"
        if h.startswith("weekend"):
            return "weekends"
        if h in _WEEKDAYS:
            days.add(_WEEKDAYS[h])
    if len(days) == 7:
        return "daily"
    return ",".join(str(d) for d in sorted(days)) if days else None


_RELATIVE_TIME_RE = re.compile(
    r"\b(?:in|after)\s+(?:a|an|\d+|half|quarter|\w+teen|twenty|thirty|forty|fifty)\b.*?"
    r"\b(seconds?|minutes?|hours?|secs?|mins?|hrs?)\b", re.IGNORECASE)


_DAYPART_DEFAULTS = {
    "morning": (8, 0), "afternoon": (14, 0), "evening": (19, 0),
    "night": (21, 0), "tonight": (21, 0), "noon": (12, 0), "midnight": (0, 0),
}
_DAYPART_RE = re.compile(
    r"\b(?:this\s+|tomorrow\s+|later\s+)?"
    r"(morning|afternoon|evening|tonight|noon|midnight|night)\b", re.IGNORECASE)


def _resolve_clock_time(src: str, now: datetime, *, allow_daypart: bool = False):
    """Pull a clock time out of `src` (word-numbers already normalised) and
    resolve it to a concrete future datetime.

    Handles am/pm inference, day words ("on Friday", "tomorrow") and
    recurrence ("every weekday at 7"). With allow_daypart, a bare daypart word
    ("tonight", "tomorrow morning") resolves to a default hour. Shared by
    parse_alarm_request and parse_reminder_request. Returns
    (target, hour, minute, repeat) or None.
    """
    # Prefer a time introduced by "at"/"for"; fall back to any bare hour.
    m = next((mm for mm in _ALARM_RE.finditer(src)
              if src[max(0, mm.start() - 5):mm.start()].strip().endswith(("at", "for"))),
             None) or _ALARM_RE.search(src)
    if not m:
        dm = _DAYPART_RE.search(src) if allow_daypart else None
        if not dm:
            return None
        hour, minute = _DAYPART_DEFAULTS[dm.group(1).lower()]
        repeat = _parse_repeat(src)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        dayw = re.search(r"\b(tomorrow|today|monday|tuesday|wednesday|thursday|"
                         r"friday|saturday|sunday)\b", src, re.IGNORECASE)
        offset = resolve_day_offset(dayw.group(1).lower(), now) if dayw else None
        if offset:
            if offset == 0 and dayw.group(1).lower() in _WEEKDAYS and target <= now:
                offset = 7
            target += timedelta(days=offset)
        while target <= now or (repeat and not _stores._repeat_matches(repeat, target)):
            target += timedelta(days=1)
        return target, hour, minute, repeat
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    meridiem = (m.group(3) or "").lower().replace(".", "")
    if meridiem == "oclock" or meridiem == "o'clock":
        meridiem = ""
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem:
        # No am/pm stated. "morning" forces AM; "afternoon/evening/tonight"
        # forces PM; otherwise 5-11 -> AM (wake-up range), 1-4 and 12 -> PM.
        if re.search(r"\b(afternoon|evening|tonight|p\.?m)\b", src, re.I):
            hour = hour % 12 + 12
        elif re.search(r"\b(morning|a\.?m)\b", src, re.I):
            hour = hour % 12
        elif 1 <= hour <= 4 or hour == 12:
            hour = hour % 12 + 12

    repeat = _parse_repeat(src)
    day_word = (m.group(4) or "").lower()
    if not day_word:
        # _ALARM_RE only captures a day word directly after the time; catch
        # "on Friday remind me at 9" / "at 9 on Friday" (its \s* eats the gap).
        dw = re.search(r"\b(?:on\s+|next\s+)?(tomorrow|today|tonight|monday|tuesday|"
                       r"wednesday|thursday|friday|saturday|sunday)\b", src, re.IGNORECASE)
        if dw:
            day_word = dw.group(1).lower()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if repeat:
        while target <= now or not _stores._repeat_matches(repeat, target):
            target += timedelta(days=1)
    else:
        offset = resolve_day_offset(day_word, now)
        if offset is None:
            if target <= now:
                target += timedelta(days=1)
        else:
            if offset == 0 and day_word in _WEEKDAYS and target <= now:
                offset = 7
            target += timedelta(days=offset)
    return target, target.hour, target.minute, repeat


def parse_alarm_request(text):
    """Return a TimerSpec for an alarm request, or None."""
    if not _ALARM_TRIGGER_RE.search(text):
        return None
    # "wake me up in 5 minutes" is a timer, not a clock alarm.
    if _RELATIVE_TIME_RE.search(_replace_word_numbers(text)):
        return None
    name, stripped = _extract_name(text)
    src = _replace_word_numbers(stripped if name else text)
    now = datetime.now()
    resolved = _resolve_clock_time(src, now)
    if resolved is None:
        return None
    target, hour, minute, repeat = resolved
    spec = TimerSpec(
        seconds=(target - now).total_seconds(), label="", kind="alarm",
        name=name, repeat=repeat, duration_s=0, wake="wake" in text.lower(),
        hour=hour, minute=minute,
    )
    # Build the label off the resolved spec so it stays consistent with reloads.
    spec.label = _stores.alarm_label(_AlarmLabelView(spec, target))
    return spec


# A reminder is a timer/alarm that carries an action phrase ("call the
# dentist"), spoken back on fire. It can be set from a duration OR a clock
# time; with no trigger time it isn't a reminder we can set and falls through
# to memory / the LLM.

_REMINDER_TRIGGER_RE = re.compile(
    r"\b(?:remind\s+me\b|(?:set|make|create)\s+(?:a|an)\s+reminder\b|"
    r"don'?t\s+let\s+me\s+forget\b)",
    re.IGNORECASE,
)
_REMINDER_LEAD_RE = re.compile(
    r"^.*?\b(?:remind\s+me|reminder|don'?t\s+let\s+me\s+forget)\b[\s,:]*",
    re.IGNORECASE,
)
_DAYPART = r"(?:morning|afternoon|evening|night)"
_REMINDER_LEAD_TIME_RE = re.compile(
    r"^(?:(?:in|after|at|by|around|about|on)\s+.+?"
    r"|(?:tomorrow|this|later)\s+" + _DAYPART +
    r"|tomorrow|tonight|today|noon|midnight|next\s+\w+|every\s+.+?)"
    r"\s+(?:to|that|,)\s+",
    re.IGNORECASE,
)
_REMINDER_FILLER_RE = re.compile(r"^(?:to|that|about|for|of)\s+", re.IGNORECASE)
_REMINDER_TAIL_TIME_RE = re.compile(
    r"(?:\s+|^)(?:"
    r"(?:in|after)\s+(?:a|an|\d[\d.]*|half|quarter|one|two|three|four|five|six|seven|eight|"
    r"nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|\w+teen)[\w\s.]*?"
    r"(?:seconds?|minutes?|hours?|mins?|secs?|hrs?)"
    r"|at\s+\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|o'?clock)?"
    r"(?:\s+(?:on\s+|next\s+)?(?:tomorrow|today|tonight|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday))?"
    r"|every\s+[\w\s]+?(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)?"
    r"|(?:tomorrow|this|later)\s+" + _DAYPART +
    r"|in\s+the\s+" + _DAYPART +
    r"|(?:on\s+|next\s+)?(?:tomorrow|tonight|today|noon|midnight)"
    r"|this\s+(?:morning|afternoon|evening|weekend)"
    r"|(?:on\s+|next\s+)(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r")[\s,.]*$",
    re.IGNORECASE,
)


def _extract_reminder_message(text: str):
    """The action phrase from a reminder request ('call the dentist'), or None
    if what's left after stripping the framing and the time is empty."""
    s = _REMINDER_LEAD_RE.sub("", text.strip(), count=1).strip()
    s = _REMINDER_LEAD_TIME_RE.sub("", s, count=1)
    prev = None
    while prev != s:
        prev, s = s, _REMINDER_FILLER_RE.sub("", s, count=1)
    for _ in range(3):
        stripped = _REMINDER_TAIL_TIME_RE.sub("", s).strip(" ,.")
        if stripped == s:
            break
        s = stripped
    s = re.sub(r"\s+", " ", s).strip(" ,.?!")
    return s.lower() or None


def parse_reminder_request(text):
    """Return a TimerSpec(kind='reminder') with a .message, or None."""
    if not _REMINDER_TRIGGER_RE.search(text):
        return None
    now = datetime.now()
    norm = _replace_word_numbers(text)
    message = _extract_reminder_message(text)

    secs = resolve_duration(norm)
    clock_signal = re.search(
        r"\bat\s+\d|\b\d{1,2}\s*(?:a\.?m|p\.?m|o'?clock)|\bevery\b|"
        r"\b(?:tomorrow|tonight|today|noon|midnight|"
        r"(?:this|tomorrow|later)\s+(?:morning|afternoon|evening|night)|"
        r"in\s+the\s+(?:morning|afternoon|evening)|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        norm, re.IGNORECASE)
    # Relative form ("in 40 minutes") wins unless the phrasing is clearly a
    # clock time ("at 3", "every morning", "tonight").
    if secs and not clock_signal:
        return TimerSpec(seconds=secs, label=message or "reminder",
                         kind="reminder", message=message)

    resolved = _resolve_clock_time(norm, now, allow_daypart=True)
    if resolved is not None:
        target, hour, minute, repeat = resolved
        return TimerSpec(
            seconds=(target - now).total_seconds(), label=message or "reminder",
            kind="reminder", message=message, repeat=repeat, duration_s=0,
            hour=hour, minute=minute,
        )
    return None


_REMINDER_DAY_RE = re.compile(
    r"\b(?:(?:on|next)\s+)?(tomorrow|today|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)\b",
    re.IGNORECASE,
)


def reminder_missing_time(text: str):
    """A reminder that names a day ("...tomorrow", "...on Friday") but no time.

    Returns (message, day_phrase) so the pipeline can ask "what time?" instead
    of guessing a point inside the day. None when the request either has no day
    or already carries a usable time (a clock time, a daypart, or a duration).
    """
    if not _REMINDER_TRIGGER_RE.search(text):
        return None
    norm = _replace_word_numbers(text)
    if resolve_duration(norm) and _RELATIVE_TIME_RE.search(norm):
        return None
    if _resolve_clock_time(norm, datetime.now(), allow_daypart=True) is not None:
        return None
    m = _REMINDER_DAY_RE.search(norm)
    if not m:
        return None
    message = _extract_reminder_message(text)
    if not message:
        return None
    day = m.group(1).lower()
    day_phrase = day if day in ("tomorrow", "today") else f"on {day}"
    return message, day_phrase


class _AlarmLabelView:
    """Adapter so stores.alarm_label() (which reads .fires_at) can label a
    freshly parsed spec that only knows its target datetime."""

    def __init__(self, spec, target):
        self.hour, self.minute = spec.hour, spec.minute
        self.name, self.wake, self.repeat = spec.name, spec.wake, spec.repeat
        self.label = ""
        self.fires_at = target.timestamp()
