"""Shared state stores: timers, memories, conversation history.

Moved verbatim from antigua_server.py (Phase 2, antigua_core extraction);
config globals now read from antigua_core.settings at call time.
TimerManager takes an on_fire callback — alarm audio is a backend concern.
"""

import difflib
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock, Thread

from . import settings

log = logging.getLogger("antigua_core")

# ── Timer Manager ─────────────────────────────────────────────────────────────

_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                  "Saturday", "Sunday")


@dataclass
class TimerSpec:
    """A parsed request to set a timer, alarm, or reminder. Produced by
    classify.parse_timer_request / parse_alarm_request, consumed by
    TimerManager.set()."""

    seconds: float
    label: str
    kind: str = "timer"          # "timer" | "alarm" | "reminder"
    name: str | None = None      # user-given name ("pasta"); None = unnamed
    repeat: str | None = None    # None | "daily" | "weekdays" | "weekends"
                                 # | comma list of weekday indices, e.g. "0,2,4"
    duration_s: float | None = None   # for reset(); defaults to `seconds`
    wake: bool = False           # "wake me up …" phrasing, for label rebuilds
    hour: int | None = None      # alarm clock time, for label rebuilds on load
    minute: int = 0
    message: str | None = None   # reminder payload ("call the dentist"); spoken
                                 # on fire and echoed in the confirmation

    def __post_init__(self):
        if self.duration_s is None:
            self.duration_s = self.seconds


def _clock_str(hour: int, minute: int) -> str:
    return datetime(2000, 1, 1, hour, minute).strftime("%I:%M %p").lstrip("0")


def alarm_label(spec_or_entry) -> str:
    """(Re)build an alarm's spoken label from its time + target date, so a
    persisted 'alarm for 7:00 AM tomorrow' doesn't still say 'tomorrow' the
    next day."""
    e = spec_or_entry
    if e.hour is None:
        return e.label
    now = datetime.now()
    target = datetime.fromtimestamp(e.fires_at) if hasattr(e, "fires_at") else now
    suffix = ""
    if e.repeat:
        suffix = f", {_repeat_phrase(e.repeat)}"
    elif hasattr(e, "fires_at"):
        if target.date() == (now + timedelta(days=1)).date():
            suffix = " tomorrow"
        elif target.date() != now.date():
            suffix = f" on {target.strftime('%A')}"
    lead = f"{e.name} " if e.name else ("wake-up " if e.wake else "")
    return f"{lead}alarm for {_clock_str(e.hour, e.minute)}{suffix}"


def _repeat_phrase(repeat: str) -> str:
    if repeat == "daily":
        return "every day"
    if repeat == "weekdays":
        return "every weekday"
    if repeat == "weekends":
        return "every weekend"
    idx = [int(x) for x in repeat.split(",") if x != ""]
    return "every " + ", ".join(_WEEKDAY_NAMES[i] for i in idx)


def _repeat_matches(repeat: str, d: datetime) -> bool:
    wd = d.weekday()
    if repeat == "daily":
        return True
    if repeat == "weekdays":
        return wd < 5
    if repeat == "weekends":
        return wd >= 5
    return wd in {int(x) for x in repeat.split(",") if x != ""}


def _next_occurrence(repeat: str, from_ts: float) -> float:
    """Next fire time for a repeating alarm, keeping the same time of day."""
    base = datetime.fromtimestamp(from_ts)
    for step in range(1, 9):
        cand = base + timedelta(days=step)
        if _repeat_matches(repeat, cand):
            return cand.timestamp()
    return (base + timedelta(days=1)).timestamp()


@dataclass
class _Entry:
    tid: str
    label: str
    kind: str
    fires_at: float
    duration_s: float
    name: str | None = None
    repeat: str | None = None
    wake: bool = False
    hour: int | None = None
    minute: int = 0
    message: str | None = None
    set_s: float = 0.0           # length as first set ("the 5 minute timer"); add_time
                                 # doesn't change it, so the name still fits after
    origin: str = "local"        # "primary": adopted by the fallback (see adopt())
    created_at: float = field(default_factory=time.time)
    thread: object = None


# An alarm counts as rung by the primary if it was still answering this long
# after the alarm's time (fire → pop is well under a second).
_PRIMARY_FIRE_MARGIN_S = 2.0


class TimerManager:
    def __init__(self, on_fire=None):
        self._timers: dict[str, _Entry] = {}
        self._lock = Lock()
        # Injected: what to do when a timer fires (synthesize + publish alarm).
        # A backend concern — the primary and fallback announce differently.
        self._on_fire = on_fire or (lambda *a, **kw: None)  # (label, kind, message)

    # ── set ─────────────────────────────────────────────────────────────────

    def set(self, spec, label: str = "Timer") -> str:
        """Accepts a TimerSpec, or (seconds, label) for the legacy call form."""
        if not isinstance(spec, TimerSpec):
            spec = TimerSpec(seconds=float(spec), label=label)
        entry = _Entry(
            tid=uuid.uuid4().hex[:6],
            label=spec.label,
            kind=spec.kind,
            fires_at=time.time() + spec.seconds,
            duration_s=spec.duration_s if spec.duration_s is not None else spec.seconds,
            name=spec.name,
            repeat=spec.repeat,
            wake=spec.wake,
            hour=spec.hour,
            minute=spec.minute,
            message=spec.message,
            set_s=0.0 if spec.kind == "alarm" else float(spec.duration_s or spec.seconds),
        )
        self._arm(entry)
        settings.mqtt_publish(
            "antigua/timer_set",
            {"id": entry.tid, "label": entry.label, "kind": entry.kind,
             "fires_at": entry.fires_at},
        )
        self._persist()
        return entry.tid

    def _arm(self, entry: _Entry):
        tid = entry.tid

        def _run():
            while True:
                remaining = entry.fires_at - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 1.0))
            with self._lock:
                still_active = tid in self._timers
                self._timers.pop(tid, None)
            if not still_active:
                return  # cancelled / reset before its natural fire time
            log.info("%s fired: %s", entry.kind.capitalize(), entry.label)
            if not entry.repeat:
                # Drop it from disk now: the fallback mirrors this file, and a
                # stale copy of a rung alarm could ring again after a failover.
                self._persist()
            self._on_fire(entry.label, entry.kind, entry.message)
            if entry.repeat:
                nxt = replace(
                    entry, tid=uuid.uuid4().hex[:6],
                    fires_at=_next_occurrence(entry.repeat, entry.fires_at),
                    thread=None,
                )
                if nxt.kind == "alarm":
                    nxt.label = alarm_label(nxt)   # refresh "tomorrow"/day words
                self._arm(nxt)
                self._persist()

        entry.thread = Thread(target=_run, daemon=True, name=f"timer-{tid}")
        with self._lock:
            self._timers[tid] = entry
        entry.thread.start()
        log.info(
            "%s set: '%s' in %.0fs (fires %s)%s",
            entry.kind.capitalize(), entry.label,
            max(0, entry.fires_at - time.time()),
            datetime.fromtimestamp(entry.fires_at).strftime("%a %H:%M:%S"),
            f" repeating {entry.repeat}" if entry.repeat else "",
        )

    # ── query ──────────────────────────────────────────────────────────────

    def list_active(self):
        now = time.time()
        with self._lock:
            return [
                {
                    "id": e.tid,
                    "label": e.label,
                    "kind": e.kind,
                    "name": e.name,
                    "repeat": e.repeat,
                    "message": e.message,
                    "hour": e.hour,
                    "minute": e.minute,
                    "fires_at": e.fires_at,
                    "remaining_s": max(0, e.fires_at - now),
                    "set_s": e.set_s,
                    "created_at": e.created_at,
                }
                for e in sorted(self._timers.values(), key=lambda x: x.fires_at)
            ]

    def find(self, substring: str) -> list[dict]:
        """Active entries whose label or name contains `substring` (case-
        insensitive). Empty substring returns all."""
        s = substring.lower().strip()
        return [
            t for t in self.list_active()
            if not s or s in t["label"].lower()
            or (t["name"] and s in t["name"].lower())
        ]

    # ── mutate ─────────────────────────────────────────────────────────────

    def cancel(self, label_substring: str) -> list[str]:
        s = label_substring.lower().strip()
        cancelled = []
        with self._lock:
            for tid, e in list(self._timers.items()):
                if not s or s in e.label.lower() or (e.name and s in e.name.lower()):
                    cancelled.append(e.label)
                    self._timers.pop(tid, None)
        if cancelled:
            log.info("Cancelled matching '%s': %s", label_substring, cancelled)
            self._persist()
        return cancelled

    def reset(self, label_substring: str = "") -> str | None:
        """Restart a timer from its original duration. Alarms can't be reset
        (their duration is meaningless); returns None for them."""
        s = label_substring.lower().strip()
        with self._lock:
            match = next(
                (e for e in sorted(self._timers.values(), key=lambda x: x.fires_at)
                 if e.kind != "alarm"
                 and (not s or s in e.label.lower() or (e.name and s in e.name.lower()))
                 and e.duration_s > 0),
                None,
            )
            spec = None
            if match:
                spec = TimerSpec(seconds=match.duration_s, label=match.label,
                                 kind=match.kind, name=match.name,
                                 duration_s=match.duration_s)
        if spec is None:
            return None
        self.cancel(spec.label)
        self.set(spec)
        return spec.label

    def add_time(self, label_substring: str, seconds: float) -> tuple[str, float] | None:
        """Extend a running timer. Returns (label, new_remaining_s) or None."""
        s = label_substring.lower().strip()
        with self._lock:
            e = next(
                (x for x in sorted(self._timers.values(), key=lambda x: x.fires_at)
                 if x.kind != "alarm"
                 and (not s or s in x.label.lower() or (x.name and s in x.name.lower()))),
                None,
            )
            if e is None:
                return None
            e.fires_at += seconds
            e.duration_s += seconds
            result = (e.label, max(0, e.fires_at - time.time()))
        self._persist()
        return result

    def cancel_ids(self, tids) -> list[str]:
        """Cancel specific entries (ids from list_active); returns their labels."""
        with self._lock:
            cancelled = [self._timers.pop(t).label for t in tids if t in self._timers]
        if cancelled:
            log.info("Cancelled: %s", cancelled)
            self._persist()
        return cancelled

    def add_time_id(self, tid: str, seconds: float) -> float | None:
        """Extend one running timer; returns its new remaining seconds."""
        with self._lock:
            e = self._timers.get(tid)
            if e is None:
                return None
            e.fires_at += seconds
            e.duration_s += seconds
            remaining = max(0, e.fires_at - time.time())
        self._persist()
        return remaining

    def cancel_all(self, kind: str | None = None) -> list[str]:
        with self._lock:
            gone = [t for t, e in self._timers.items() if kind is None or e.kind == kind]
            cancelled = [self._timers.pop(t).label for t in gone]
        if cancelled:
            log.info("Cancelled all: %s", cancelled)
            self._persist()
        return cancelled

    # ── failover (fallback adopts the primary's alarms while it's down) ─────

    def snapshot(self) -> list[dict]:
        """This box's own timers in their on-disk form — served to the
        fallback so it can take them over if this box goes down."""
        with self._lock:
            return [_entry_to_dict(e) for e in self._timers.values() if e.origin == "local"]

    def adopt(self, entries: list[dict], primary_alive_until: float,
              grace_s: float = 300) -> int:
        """Arm the primary's timers here (fallback only). An entry already due
        rings now if the primary went quiet before it was due and it's at most
        grace_s late; one the primary outlived is treated as already rung.
        Adopted entries are never persisted here and are dropped by
        drop_adopted() once the primary is back."""
        now = time.time()
        armed = 0
        for d in entries:
            e = _entry_from_dict(d, fires_at=d["fires_at"])
            e.origin = "primary"
            if e.fires_at <= now:
                rung = e.fires_at + _PRIMARY_FIRE_MARGIN_S <= primary_alive_until
                if not rung and now - e.fires_at <= grace_s:
                    log.warning("Adopting overdue %s now: %s", e.kind, e.label)
                    e.fires_at = now
                elif e.repeat:
                    e.fires_at = _next_occurrence(e.repeat, e.fires_at)
                    while e.fires_at <= now:
                        e.fires_at = _next_occurrence(e.repeat, e.fires_at)
                else:
                    continue
            if e.kind == "alarm":
                e.label = alarm_label(e)
            self._arm(e)
            armed += 1
        log.info("Adopted %d of the primary's timer(s)/alarm(s)", armed)
        return armed

    def drop_adopted(self) -> int:
        with self._lock:
            gone = [tid for tid, e in self._timers.items() if e.origin == "primary"]
            for tid in gone:
                self._timers.pop(tid)
        if gone:
            log.info("Dropped %d adopted timer(s)/alarm(s) — primary owns them again", len(gone))
        return len(gone)

    # ── persistence ────────────────────────────────────────────────────────

    def _persist(self):
        try:
            settings.TIMER_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = self.snapshot()
            with open(settings.TIMER_STORE_PATH, "w") as f:
                json.dump(data, f)
        except Exception as e:
            log.warning("Timer persist failed: %s", e)

    def _load(self):
        if not settings.TIMER_STORE_PATH.exists():
            return
        try:
            with open(settings.TIMER_STORE_PATH) as f:
                data = json.load(f)
        except Exception as e:
            log.warning("Timer load failed: %s", e)
            return
        now = time.time()
        restored = 0
        for entry in data:
            kind = entry.get("kind", "timer")
            repeat = entry.get("repeat")
            fires_at = entry["fires_at"]
            if fires_at <= now and repeat:
                fires_at = _next_occurrence(repeat, fires_at)
            if fires_at <= now:
                log.info("Skipping expired %s: %s", kind, entry["label"])
                continue
            e = _entry_from_dict(entry, fires_at=fires_at)
            if kind == "alarm":
                e.label = alarm_label(e)   # refresh "tomorrow"/"on Friday"
            self._arm(e)
            restored += 1
        log.info("Restored %d timer(s)/alarm(s) from disk", restored)


def _entry_to_dict(e: _Entry) -> dict:
    return {"label": e.label, "kind": e.kind, "fires_at": e.fires_at,
            "duration_s": e.duration_s, "name": e.name, "repeat": e.repeat,
            "wake": e.wake, "hour": e.hour, "minute": e.minute,
            "message": e.message, "set_s": e.set_s, "created_at": e.created_at}


def _entry_from_dict(d: dict, fires_at: float) -> _Entry:
    return _Entry(
        tid=uuid.uuid4().hex[:6],
        label=d["label"], kind=d.get("kind", "timer"), fires_at=fires_at,
        duration_s=d.get("duration_s", d["fires_at"] - time.time()),
        name=d.get("name"), repeat=d.get("repeat"),
        wake=d.get("wake", False),
        hour=d.get("hour"), minute=d.get("minute", 0),
        message=d.get("message"),
        set_s=d.get("set_s", 0.0 if d.get("kind") == "alarm" else d.get("duration_s", 0.0)),
        created_at=d.get("created_at", time.time()),
    )


# ── Memory Store ─────────────────────────────────────────────────────────────


# Permanent-fact heuristics — memories matching these never expire
PERMANENT_FACT_PATTERNS = [
    re.compile(r"\b(?:allergic to|allergy|allergies)\b", re.IGNORECASE),
    re.compile(r"\bbirthday\s+(?:is|was)\b", re.IGNORECASE),
    re.compile(r"\bphone\s+(?:number|#)\b", re.IGNORECASE),
    re.compile(r"\bemail\s+(?:address|is)\b", re.IGNORECASE),
    re.compile(r"\balways\s+(?:use|take|eat|drink|wear)\b", re.IGNORECASE),
    re.compile(r"\bnever\s+(?:eat|drink|take|use|wear)\b", re.IGNORECASE),
    re.compile(r"\b(address|lives?\s+(?:at|in))\b", re.IGNORECASE),
    re.compile(
        r"\b(is|are)\s+(?:a|an)\s+(?:vegetarian|vegan|diabetic|lactose[- ]?intolerant)\b",
        re.IGNORECASE,
    ),
]

# Auto-tagging keywords for category-aware deletion
_MEMORY_TAGS = {
    "medicine": [
        "medicine",
        "pill",
        "vitamin",
        "meds",
        "dose",
        "ibuprofen",
        "tylenol",
        "aspirin",
        "prescription",
    ],
    "food": [
        "ate",
        "breakfast",
        "lunch",
        "dinner",
        "snack",
        "coffee",
        "tea",
        "meal",
        "restaurant",
    ],
    "pets": ["dog", "cat", "walk", "walked", "fed", "feed", "vet", "groomer"],
    "health": [
        "doctor",
        "appointment",
        "pain",
        "headache",
        "fever",
        "sick",
        "sore",
        "hurt",
    ],
    "sleep": ["bed", "nap", "slept", "woke", "wake", "insomnia", "tired", "rested"],
    "travel": ["trip", "flight", "airport", "hotel", "drove", "driving", "car", "gas"],
}


class MemoryStore:
    """Persistent voice notepad. Entries are tagged by person + timestamp,
    stored in a JSON file, and expire after settings.MEMORY_TTL_DAYS days."""

    def __init__(self, path: Path = None, ttl_days: int = None):
        # Defaults resolved at call time, not import time — settings.configure()
        # runs after this module is imported.
        self._path = path or settings.MEMORY_STORE_PATH
        self._ttl_days = ttl_days if ttl_days is not None else settings.MEMORY_TTL_DAYS
        self._lock = Lock()
        self._entries = []  # list of dicts
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._entries = data.get("memories", [])
                self._expire()
                log.info(
                    "MemoryStore: loaded %d entries from %s",
                    len(self._entries),
                    self._path,
                )
            except Exception as e:
                log.warning("MemoryStore: load failed (%s), starting fresh", e)
                self._entries = []
        else:
            self._entries = []

    def _save(self):
        try:
            self._path.write_text(json.dumps({"memories": self._entries}, indent=2))
        except Exception as e:
            log.error("MemoryStore: save failed: %s", e)

    def _expire(self):
        cutoff = (datetime.now() - timedelta(days=self._ttl_days)).isoformat()
        before = len(self._entries)
        self._entries = [
            e for e in self._entries if e.get("permanent") or e["timestamp"] >= cutoff
        ]
        removed = before - len(self._entries)
        if removed:
            log.info("MemoryStore: expired %d old entries", removed)

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def active(self) -> list[dict]:
        """Return all non-expired memory entries (for the display)."""
        self._expire()
        now = datetime.now().isoformat()
        return [
            {k: v for k, v in e.items() if k in ("id", "person", "raw", "timestamp", "tags", "permanent")}
            for e in self._entries
            if e.get("permanent") or e.get("expires_at", "") > now
        ]

    @staticmethod
    def _is_permanent(raw: str) -> bool:
        """Return True if the raw text looks like a permanent household fact."""
        return any(p.search(raw) for p in PERMANENT_FACT_PATTERNS)

    @staticmethod
    def _detect_tags(raw: str) -> list[str]:
        """Return auto-detected category tags for a memory entry."""
        raw_low = raw.lower()
        tags = []
        for tag, keywords in _MEMORY_TAGS.items():
            if any(kw in raw_low for kw in keywords):
                tags.append(tag)
        return tags

    # ── Write ─────────────────────────────────────────────────────────────────

    def add(self, person: str, raw: str) -> dict | None:
        """Save a new memory entry. Returns the saved entry, or None if a near-duplicate
        was found within the last 2 hours."""
        now = datetime.now()
        two_hours_ago = (now - timedelta(hours=2)).isoformat()
        norm_new = re.sub(r"[^\w\s]", "", raw).lower().strip()

        with self._lock:
            for e in reversed(self._entries):
                if e["person"].lower() != person.lower():
                    continue
                if e["timestamp"] < two_hours_ago:
                    break
                norm_old = re.sub(r"[^\w\s]", "", e["raw"]).lower().strip()
                # Exact match after stripping punctuation
                if norm_old == norm_new:
                    log.info(
                        "MemoryStore: skipped exact duplicate for %s — '%s'",
                        person,
                        raw,
                    )
                    return None
                # Substring containment (one is contained in the other)
                if (
                    len(norm_old) > 5
                    and len(norm_new) > 5
                    and (norm_old in norm_new or norm_new in norm_old)
                ):
                    log.info(
                        "MemoryStore: skipped substring duplicate for %s — '%s'",
                        person,
                        raw,
                    )
                    return None
                # Loose similarity for longer phrases
                if len(norm_old) > 20 and len(norm_new) > 20:
                    similarity = difflib.SequenceMatcher(
                        None, norm_old, norm_new
                    ).ratio()
                    if similarity >= 0.85:
                        log.info(
                            "MemoryStore: skipped similar duplicate for %s — '%s' (%.0f%% match)",
                            person,
                            raw,
                            similarity * 100,
                        )
                        return None

        permanent = self._is_permanent(raw)
        tags = self._detect_tags(raw)
        entry = {
            "id": uuid.uuid4().hex[:8],
            "person": person,
            "raw": raw,
            "timestamp": now.isoformat(),
            "expires_at": (now + timedelta(days=self._ttl_days)).isoformat(),
            "permanent": permanent,
            "tags": tags,
        }
        with self._lock:
            self._entries.append(entry)
            self._save()
        log.info(
            "MemoryStore: saved for %s — %s (tags=%s, permanent=%s)",
            person,
            raw,
            tags,
            permanent,
        )
        return entry

    def delete_last(self, person: str) -> dict | None:
        """Delete the most recent entry for a person. Returns the deleted entry or None."""
        with self._lock:
            for i in range(len(self._entries) - 1, -1, -1):
                if self._entries[i]["person"].lower() == person.lower():
                    entry = self._entries.pop(i)
                    self._save()
                    log.info(
                        "MemoryStore: deleted last entry for %s — %s",
                        person,
                        entry["raw"],
                    )
                    return entry
        return None

    def delete_by_content(
        self, person: str, keyword: str, delete_all: bool = False
    ) -> list[dict]:
        """Delete entries whose raw text or tags contain keyword (case-insensitive).
        Returns the deleted entries (most recent first)."""
        keyword_low = keyword.lower()
        deleted = []
        with self._lock:
            for i in range(len(self._entries) - 1, -1, -1):
                e = self._entries[i]
                if e["person"].lower() != person.lower():
                    continue
                match = keyword_low in e["raw"].lower()
                if not match:
                    match = any(keyword_low in t for t in e.get("tags", []))
                if match:
                    deleted.append(self._entries.pop(i))
                    if not delete_all:
                        break
            if deleted:
                self._save()
                for d in deleted:
                    log.info("MemoryStore: deleted entry for %s — %s", person, d["raw"])
        return deleted

    def update_raw(self, entry_id: str, new_raw: str) -> dict | None:
        """Update the raw text of an existing entry. Returns the updated entry or None."""
        with self._lock:
            for e in self._entries:
                if e["id"] == entry_id:
                    old_raw = e["raw"]
                    e["raw"] = new_raw
                    # Re-evaluate tags since the content changed
                    e["tags"] = self._detect_tags(new_raw)
                    self._save()
                    log.info(
                        "MemoryStore: updated entry %s from '%s' to '%s'",
                        entry_id,
                        old_raw,
                        new_raw,
                    )
                    return e
        return None

    # ── Query ─────────────────────────────────────────────────────────────────

    def query_today(self, person: str | None = None) -> list:
        """Return all entries for today, optionally filtered by person."""
        today = datetime.now().date().isoformat()
        with self._lock:
            return [
                e
                for e in self._entries
                if e["timestamp"].startswith(today)
                and (person is None or e["person"].lower() == person.lower())
            ]

    def format_for_prompt(
        self,
        person: str | None = None,
        days: int = 1,
        keywords: list[str] | None = None,
    ) -> str:
        """Format recent memories as LLM context. Covers the last `days` days.
        If `keywords` are given, only entries whose raw text contains at least
        one keyword (case-insensitive) are included."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        keyword_set = {k.lower() for k in keywords} if keywords else None
        with self._lock:
            entries = [
                e
                for e in self._entries
                if e["timestamp"] >= cutoff
                and (person is None or e["person"].lower() == person.lower())
                and (
                    keyword_set is None
                    or any(kw in e["raw"].lower() for kw in keyword_set)
                    or any(kw in t for t in e.get("tags", []) for kw in keyword_set)
                )
            ]
        if not entries:
            who = person or "anyone"
            hint = f" matching '{', '.join(keywords)}'" if keywords else ""
            return f"No memory records found for {who} in the last {days} day{'s' if days != 1 else ''}{hint}."
        lines = ["Memory records (voice notepad):"]
        for e in sorted(entries, key=lambda x: x["timestamp"]):
            ts = datetime.fromisoformat(e["timestamp"]).strftime("%-I:%M %p")
            day = datetime.fromisoformat(e["timestamp"]).strftime("%A")
            now_day = datetime.now().strftime("%A")
            day_label = "today" if day == now_day else day
            lines.append(f"- {e['person']} at {ts} {day_label}: {e['raw']}")
        return "\n".join(lines)


# ── List Store ───────────────────────────────────────────────────────────────


class ListStore:
    """Household-shared named lists (shopping, todo, ...).

    Sibling to MemoryStore, not a mode of it: lists are shared, permanent,
    and ordered; memories are per-person, TTL'd, and tagged. Shoehorning
    lists into MemoryStore would drag the "who is this for?" disambiguation
    into a shopping list, which is exactly wrong.
    """

    def __init__(self, path: Path = None):
        # Resolved at call time — settings.configure() runs after import.
        self._path = path or settings.LISTS_STORE_PATH
        self._lock = Lock()
        self._lists: dict[str, list[str]] = {}
        self._load()

    def _load(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._lists = data.get("lists", {})
                log.info(
                    "ListStore: loaded %d list(s) from %s", len(self._lists), self._path
                )
            except Exception as e:
                log.warning("ListStore: load failed (%s), starting fresh", e)
                self._lists = {}
        else:
            self._lists = {}

    def _save(self):
        try:
            self._path.write_text(json.dumps({"lists": self._lists}, indent=2))
        except Exception as e:
            log.error("ListStore: save failed: %s", e)

    def add(self, list_name: str, items: list[str]) -> list[str]:
        """Append items, skipping case-insensitive duplicates already present.
        Returns the items actually added (may be fewer than requested)."""
        added = []
        with self._lock:
            existing = self._lists.setdefault(list_name, [])
            existing_low = {i.lower() for i in existing}
            for item in items:
                if item.lower() not in existing_low:
                    existing.append(item)
                    existing_low.add(item.lower())
                    added.append(item)
            if added:
                self._save()
        if added:
            log.info("ListStore: added %s to %s", added, list_name)
        return added

    def read(self, list_name: str) -> list[str]:
        with self._lock:
            return list(self._lists.get(list_name, []))

    def remove(self, list_name: str, item: str) -> str | None:
        """Remove the first case-insensitive exact or substring match.
        Returns the removed item's original text, or None if nothing matched."""
        item_low = item.lower()
        with self._lock:
            existing = self._lists.get(list_name, [])
            for i, entry in enumerate(existing):
                if entry.lower() == item_low or item_low in entry.lower():
                    removed = existing.pop(i)
                    self._save()
                    log.info("ListStore: removed '%s' from %s", removed, list_name)
                    return removed
        return None

    def clear(self, list_name: str) -> int:
        """Remove every item from a list. Returns the count removed."""
        with self._lock:
            existing = self._lists.get(list_name, [])
            count = len(existing)
            if count:
                self._lists[list_name] = []
                self._save()
        if count:
            log.info("ListStore: cleared %d item(s) from %s", count, list_name)
        return count

    def all_lists(self) -> dict[str, list[str]]:
        """Return every named list (for the /lists endpoint)."""
        with self._lock:
            return {k: list(v) for k, v in self._lists.items()}


# ── Conversation Memory ────────────────────────────────────────────────────


class ConversationStore:
    """Thread-safe LRU store for conversation histories."""

    def __init__(self, ttl=None, max_hist=None):
        self._convs = OrderedDict()
        self._lock = Lock()
        # Resolved at call time — settings.configure() runs after import.
        self._ttl = ttl if ttl is not None else settings.CONVERSATION_TTL
        self._max_hist = max_hist if max_hist is not None else settings.MAX_HISTORY

    def _get_or_create_nolock(self, conv_id):
        """Must be called with self._lock already held."""
        if conv_id in self._convs:
            entry = self._convs.pop(conv_id)
            entry["last_used"] = time.time()
            self._convs[conv_id] = entry
            return entry
        entry = {"messages": [], "last_used": time.time()}
        self._convs[conv_id] = entry
        return entry

    def get_or_create(self, conv_id):
        with self._lock:
            return self._get_or_create_nolock(conv_id)

    def add_message(self, conv_id, role, content):
        with self._lock:
            entry = self._get_or_create_nolock(conv_id)
            entry["messages"].append({"role": role, "content": content})
            if len(entry["messages"]) > self._max_hist:
                entry["messages"] = [
                    entry["messages"][0],
                    *entry["messages"][-(self._max_hist - 1) :],
                ]
            entry["last_used"] = time.time()

    def get_messages(self, conv_id):
        with self._lock:
            if conv_id in self._convs:
                return list(self._convs[conv_id]["messages"])
        return []

    def cleanup(self):
        now = time.time()
        with self._lock:
            expired = [
                cid
                for cid, e in self._convs.items()
                if now - e["last_used"] > self._ttl
            ]
            for cid in expired:
                del self._convs[cid]
            if expired:
                log.info("Cleaned %d expired conversations", len(expired))


