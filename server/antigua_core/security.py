"""Home security through Home Assistant: the Eufy doorbell and garage
cameras, and the front door lock.

HA's eufy_security integration does the Eufy side. Asked to show a camera,
we ask PinedaDisplay (its MCP camera_show tool) to put it up; pineda-web
starts the P2P livestream (HA pushes it into go2rtc) and stops it the moment
the card is gone — dismissed, replaced or timed out — so a battery camera
isn't left streaming even if we crash.

A doorbell ring puts that camera up on its own; so does motion or a person
at a camera with show_on_motion (the front door: 30 s, then the display goes
back to whatever it was doing), at most once per motion cooldown and only
during motion_hours (not overnight). Only a person speaks an announcement
by default (announce_on): the doorbell reports wind and passing cars as
motion, and "somebody's at the door" for those was the false alarm.

Every lock change, doorbell ring and person/motion detection is written to a
local SQLite log by a poller, so "when was the front door locked?" and "who
triggered the garage cam?" are answered from here, not from HA's history,
and survive HA's recorder purge. The poller also refreshes each camera's
snapshot for the display's traffic-cam card on its own (slow) schedule.

When a camera finishes charging, the poller restarts HA's Eufy bridge
add-on: the bridge pushes the charging status live but never a new battery
level, so without the restart the level stays wherever it was before the
charge (a reload of the integration isn't enough).

There is no unlock: see intents/security.py.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import requests
import websocket

log = logging.getLogger("antigua.security")


class SecurityError(RuntimeError):
    """Home Assistant (or go2rtc) couldn't be reached or refused."""


def read_token(path: str | Path) -> str | None:
    """HA_TOKEN=... from a root-only env file."""
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith("HA_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError as e:
        log.warning("Security: can't read the HA token: %s", e)
    return None


class HomeAssistant:
    def __init__(self, url: str, token: str, timeout: float = 8):
        self.url = url.rstrip("/")
        self._s = requests.Session()
        self._s.headers["Authorization"] = f"Bearer {token}"
        self._token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, **kw):
        try:
            r = self._s.request(method, f"{self.url}{path}", timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise SecurityError(f"Home Assistant unreachable: {e}") from e
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise SecurityError(f"Home Assistant {path}: HTTP {r.status_code}")
        return r.json() if r.content else None

    def watch(self, entities: list[str], on_state, stop: threading.Event) -> None:
        """Until `stop`: HA's WebSocket API pushes each change to one of
        `entities` — on_state({entity_id: new_state}) the moment it happens,
        rather than at the next poll. Reconnects after 5 s when dropped.

        Subscribes to state_changed events and filters here, because HA
        refuses subscribe_trigger to non-admin users, and Antigua's HA user is
        non-admin (security review 2026-10-16). state_changed is on HA's
        allowlist for them and is filtered by the user's entity permissions."""
        url = ("ws" + self.url[4:] if self.url.startswith("http") else self.url) + "/api/websocket"
        wanted = set(entities)
        while not stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(url, timeout=15)
                json.loads(ws.recv())                                    # auth_required
                ws.send(json.dumps({"type": "auth", "access_token": self._token}))
                if json.loads(ws.recv()).get("type") != "auth_ok":
                    raise SecurityError("Home Assistant refused the token")
                ws.send(json.dumps({"id": 1, "type": "subscribe_events",
                                    "event_type": "state_changed"}))
                ok = json.loads(ws.recv())
                if not ok.get("success"):
                    raise SecurityError(f"subscribe failed: {ok.get('error')}")
                log.info("Security: Home Assistant pushes %d entities", len(entities))
                ws.settimeout(60)
                while not stop.is_set():
                    try:
                        msg = json.loads(ws.recv())
                    except websocket.WebSocketTimeoutException:
                        ws.ping()                                        # quiet, not dead
                        continue
                    data = (msg.get("event") or {}).get("data") or {}
                    if data.get("entity_id") in wanted and data.get("new_state"):
                        on_state({data["entity_id"]: data["new_state"]})
            except Exception as e:  # noqa: BLE001 — logged; the poller covers the gap
                log.warning("Security: Home Assistant push dropped: %s", e)
            finally:
                if ws is not None:
                    ws.close()
            stop.wait(5)

    def states(self) -> dict[str, dict]:
        return {s["entity_id"]: s for s in self._req("GET", "/api/states") or []}

    def state(self, entity_id: str) -> dict | None:
        return self._req("GET", f"/api/states/{entity_id}")

    def call(self, domain: str, service: str, entity_id: str, **data) -> None:
        self._req("POST", f"/api/services/{domain}/{service}",
                  json={"entity_id": entity_id, **data})

    def restart_addon(self, slug: str) -> None:
        """Admin tokens only: HA refuses hassio services to other users."""
        self._req("POST", "/api/services/hassio/addon_restart", json={"addon": slug})


@dataclass
class Camera:
    name: str                 # "front_door" — pineda-web's [cameras.streams] key
    entity: str               # camera.doorbell
    title: str                # "Front door"
    stream: str               # go2rtc stream name (the device serial)
    battery: str | None = None
    motion: str | None = None
    person: str | None = None
    ring: str | None = None
    charging: str | None = None   # sensor.doorbell_charging_status — a charge ending
                                  # restarts the Eufy add-on for a fresh level
    snapshot_minutes: float = 0   # 0 = no snapshots for the display
    announce_motion: bool = False # speak unprompted when motion/person is detected
    announce_on: list[str] = field(default_factory=lambda: ["person"])
                                  # which detections speak: plain motion (wind, cars,
                                  # shadows) only with "motion" listed here
    motion_announcement: str | None = None  # optional custom phrase
    person_name: str | None = None          # optional sensor for person identity (e.g. sensor.doorbell_person_name)
    show_on_motion: float = 0     # put the live video up this many seconds on motion; 0 = don't
    pip_camera: str | None = None # secondary camera for Picture-in-Picture


@dataclass
class Lock:
    entity: str               # lock.front_door — disabled until the shared Eufy
                              # account may operate it
    status: str | None = None # sensor.front_door_lock_status — readable regardless
    battery: str | None = None
    title: str = "front door"


def in_hours(hours_str: str) -> bool:
    """True when the current local time falls within 'HH:MM-HH:MM' (inclusive start, exclusive end)."""
    if not hours_str:
        return True
    try:
        start, end = (int(h) * 60 + int(m) for h, m in
                      (x.split(":") for x in hours_str.split("-")))
    except ValueError:
        return True
    now = time.localtime()
    minute = now.tm_hour * 60 + now.tm_min
    return start <= minute < end if start <= end else (minute >= start or minute < end)


@dataclass
class Config:
    cameras: dict[str, Camera] = field(default_factory=dict)
    lock: Lock | None = None
    go2rtc: str = "http://127.0.0.1:1984"
    live_seconds: float = 60
    snapshots_dir: Path | None = None
    low_battery: int = 20
    poll_seconds: float = 2
    snapshot_hours: str = "05:00-23:00"   # the display's camera window; none overnight
    motion_hours: str = "07:00-23:00"     # speak motion announcements during these hours
    motion_cooldown_seconds: float = 60.0 # seconds before announcing motion again on a camera
    eufy_addon: str | None = None         # HA add-on slug of the eufy-security-ws bridge
    # With a non-admin HA user, press this input_button instead; an HA
    # automation does the hassio.addon_restart in HA's own context.
    eufy_restart_button: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        cams = {n: Camera(name=n, **c) for n, c in (d.get("cameras") or {}).items()}
        lock = Lock(**d["lock"]) if d.get("lock") else None
        snap = d.get("snapshots_dir")
        return cls(cameras=cams, lock=lock, go2rtc=d.get("go2rtc", cls.go2rtc),
                   live_seconds=d.get("live_seconds", cls.live_seconds),
                   snapshots_dir=Path(snap) if snap else None,
                   low_battery=d.get("low_battery", cls.low_battery),
                   poll_seconds=d.get("poll_seconds", cls.poll_seconds),
                   snapshot_hours=d.get("snapshot_hours", cls.snapshot_hours),
                   motion_hours=d.get("motion_hours", cls.motion_hours),
                   motion_cooldown_seconds=float(d.get("motion_cooldown_seconds",
                                                       cls.motion_cooldown_seconds)),
                   eufy_addon=d.get("eufy_addon"),
                   eufy_restart_button=d.get("eufy_restart_button"))


# ── the event log ────────────────────────────────────────────────────────────

class EventLog:
    """Append-only: (ts, subject, event, detail, source).

    subject: "front_door_lock" | a camera name. event: locked, unlocked,
    jammed, ring, person, motion. source: "voice:Sam", "voice", or "" for
    a change seen from HA (keypad, app, auto-lock). Kept 30 days, which
    outlasts HA's recorder purge; pruned at startup and daily after that."""

    KEEP_S = 30 * 86400

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (ts REAL, subject TEXT,"
                       " event TEXT, detail TEXT, source TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS events_by ON events (subject, event, ts)")
            self._prune(db)

    def _prune(self, db) -> None:
        db.execute("DELETE FROM events WHERE ts < ?", (time.time() - self.KEEP_S,))
        self._pruned = time.time()

    def _db(self):
        return sqlite3.connect(self._path, timeout=5)

    def add(self, subject: str, event: str, detail: str = "", source: str = "",
            ts: float | None = None) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                       (ts or time.time(), subject, event, detail, source))
            if time.time() - self._pruned > 86400:
                self._prune(db)

    def last(self, subject: str, events: tuple[str, ...]) -> dict | None:
        q = (f"SELECT ts, subject, event, detail, source FROM events WHERE subject = ?"
             f" AND event IN ({','.join('?' * len(events))}) ORDER BY ts DESC LIMIT 1")
        with self._db() as db:
            row = db.execute(q, (subject, *events)).fetchone()
        return dict(zip(("ts", "subject", "event", "detail", "source"), row)) if row else None

    def recent_source(self, subject: str, event: str, within: float) -> str | None:
        """The source of a matching event logged in the last `within` seconds
        (a voice lock, so the change HA then reports is credited to it)."""
        row = self.last(subject, (event,))
        if row and time.time() - row["ts"] < within and row["source"]:
            return row["source"]
        return None


# ── the skill ────────────────────────────────────────────────────────────────

_LOCKED = {"locked": "locked", "unlocked": "unlocked", "jammed": "jammed",
           "locking": "locking", "unlocking": "unlocking", "open": "unlocked"}


def lock_word(state: str | None) -> str | None:
    """HA lock / eufy lock_status sensor state → locked | unlocked | jammed |
    locking | unlocking, or None when unknown."""
    return _LOCKED.get((state or "").strip().lower())


def clean_names(people: Sequence[str] | None) -> tuple[list[str], bool]:
    """Clean, capitalize, and deduplicate person names. Returns (names, has_unknown)."""
    if not people:
        return [], False
    clean: list[str] = []
    has_unknown = False
    for p in people:
        if not p or not isinstance(p, str):
            continue
        p_clean = p.strip()
        if not p_clean:
            continue
        if p_clean.lower() in ("unknown", "unidentified", "stranger", "guest", "none", "null"):
            has_unknown = True
            continue
        formatted = " ".join(word.capitalize() for word in p_clean.split())
        if formatted not in clean:
            clean.append(formatted)
    return clean, has_unknown


def _single_person_phrase(name: str, loc: str) -> str:
    """Natural contraction for a single visitor: 'Sam's at the front door',
    using 'is' for names ending in sibilants ('James is at the front door')."""
    if name.lower().endswith(("s", "z", "x", "sh", "ch")):
        return f"{name} is at {loc}."
    return f"{name}'s at {loc}."


def format_visitor_announcement(
    camera_title: str,
    people: Sequence[str] | None = None,
    has_unknown: bool = False,
    fallback_announcement: str | None = None,
) -> str:
    """Format a natural spoken announcement for visitor(s) at a camera.

    Examples:
      - None / [] -> "Somebody is at the front door."
      - ["Sam"] -> "Sam's at the front door."
      - ["Maria", "Vivicca"] -> "Maria is at the front door with Vivicca."
      - ["Sam", "Jordan", "Maria"] -> "Sam, Jordan, and Maria are at the front door."
      - ["Sam"], has_unknown=True -> "Sam is at the front door with a guest."
      - ["Maria", "Vivicca"], has_unknown=True -> "Maria and Vivicca are at the front door with a guest."
    """
    clean_people, extra_unknown = clean_names(people)
    has_unknown = has_unknown or extra_unknown

    loc = camera_title.strip()
    if not loc.lower().startswith("the "):
        loc = f"the {loc.lower()}"
    else:
        loc = loc.lower()

    if not clean_people:
        if fallback_announcement:
            return fallback_announcement
        return f"Somebody is at {loc}."

    if len(clean_people) == 1:
        name = clean_people[0]
        if has_unknown:
            return f"{name} is at {loc} with a guest."
        return _single_person_phrase(name, loc)

    if len(clean_people) == 2:
        if has_unknown:
            return f"{clean_people[0]} and {clean_people[1]} are at {loc} with a guest."
        return f"{clean_people[0]} is at {loc} with {clean_people[1]}."

    joined = ", ".join(clean_people[:-1]) + f", and {clean_people[-1]}"
    if has_unknown:
        return f"{joined} are at {loc} with a guest."
    return f"{joined} are at {loc}."


def extract_people_from_state(s: dict | None) -> tuple[list[str], bool]:
    """Extract recognized person names and unknown visitor flag from a HA state object or attribute dict."""
    if not s or not isinstance(s, dict):
        return [], False
    attrs = s.get("attributes", {}) if isinstance(s.get("attributes"), dict) else {}
    candidates: list[str] = []

    # 1. State value if entity is a text sensor (not on/off/unavailable)
    state_val = s.get("state")
    if isinstance(state_val, str) and state_val.lower() not in (
        "on", "off", "unavailable", "unknown", "idle", "none", "clear"
    ):
        candidates.append(state_val)

    # 2. Common attributes from Eufy BionicMind, Frigate sub_labels, Double Take
    for key in ("person_name", "sub_label", "sub_labels", "faces", "matched_faces", "detected_faces", "persons"):
        val = attrs.get(key)
        if val is None:
            continue
        if isinstance(val, (list, tuple, set)):
            for item in val:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict) and "name" in item:
                    candidates.append(str(item["name"]))
                elif isinstance(item, dict) and "label" in item:
                    candidates.append(str(item["label"]))
        elif isinstance(val, str) and val.strip():
            for item in val.split(","):
                candidates.append(item.strip())
        elif isinstance(val, dict):
            candidates.extend(str(k) for k in val.keys())

    return clean_names(candidates)


class Security:
    def __init__(self, ha: HomeAssistant, cfg: Config, events: EventLog, pineda=None,
                 announce=None):
        self.ha = ha
        self.cfg = cfg
        self.events = events
        self.pineda = pineda
        self.announce = announce
        self._seen: dict[str, str] = {}         # entity → last state the poller saw
        self._snap_due: dict[str, float] = {}
        self._charged: str | None = None        # a camera done charging, level not refreshed
        self._last_restart = 0.0
        self._last_motion_announce: dict[str, float] = {}
        self._last_motion_show: dict[str, float] = {}
        self._stop = threading.Event()
        self._poll_lock = threading.Lock()      # the push and the poller take turns
        self.listeners: dict[str, list] = {}    # entity → callbacks fed each state seen

    def listen(self, entity: str, fn) -> None:
        """Feed `fn` every state of `entity` the push or the poller sees
        (repeats included: the callback dedups). Call before start()."""
        self.listeners.setdefault(entity, []).append(fn)

    # ── the lock ──

    def lock_state(self) -> str | None:
        L = self.cfg.lock
        if L is None:
            return None
        for entity in (L.entity, L.status):
            if not entity:
                continue
            s = self.ha.state(entity)
            if s and s["state"] not in ("unavailable", "unknown"):
                word = lock_word(s["state"])
                if word:
                    return word
        return None

    def can_operate(self) -> bool:
        L = self.cfg.lock
        s = self.ha.state(L.entity) if L else None
        return bool(s) and s["state"] not in ("unavailable", "unknown")

    def lock(self, source: str = "voice") -> str:
        """"already" | "locking" | "unavailable" (no permission / offline)."""
        if self.lock_state() == "locked":
            return "already"
        if not self.can_operate():
            return "unavailable"
        self.events.add("front_door_lock", "lock_requested", source=source)
        self.ha.call("lock", "lock", self.cfg.lock.entity)
        return "locking"

    # ── live view (pineda-web runs the livestream while the card is up) ──

    def show_camera(self, name: str, seconds: float | None = None, pip: str | None = None) -> None:
        if name not in self.cfg.cameras:
            raise SecurityError(f"no camera {name!r}")
        cam = self.cfg.cameras[name]
        target_pip = pip if pip is not None else cam.pip_camera
        self.pineda.show_camera(name, seconds or self.cfg.live_seconds, pip=target_pip)

    def _show_unasked(self, name: str, seconds: float | None, why: str, pip: str | None = None) -> None:
        """A ring or motion: the camera goes up on its own."""
        try:
            self.show_camera(name, seconds, pip=pip)
            log.info("Security: showing %s%s for %s", name, f" (with pip {pip})" if pip else "", why)
        except Exception:
            log.exception("Security: showing the %s camera for %s", name, why)

    def caption(self, text: str) -> None:
        self.pineda.caption_camera(text)

    def _live(self) -> str | None:
        """The camera pineda-web is streaming (None: none, or can't tell)."""
        try:
            return self.pineda.status().get("camera_streaming") if self.pineda else None
        except Exception:
            return None

    def _stop_stream(self, cam: Camera) -> None:
        try:
            self.ha.call("eufy_security", "stop_p2p_livestream", cam.entity)
            log.info("Security: stopped the %s livestream", cam.name)
        except SecurityError as e:
            log.warning("Security: stopping the %s livestream: %s", cam.name, e)

    # ── batteries ──

    def battery(self, what: str) -> int | None:
        entity = (self.cfg.lock.battery if what == "lock" and self.cfg.lock
                  else getattr(self.cfg.cameras.get(what), "battery", None))
        s = self.ha.state(entity) if entity else None
        try:
            return int(float(s["state"]))
        except (TypeError, ValueError, KeyError):
            return None

    # ── the poller: event log + snapshots ──

    def start(self) -> None:
        """The poller, and HA pushing changes as they happen: a ring or
        motion acts at once instead of up to poll_seconds later. Either
        sees a change once (poll_once remembers what it saw); the poller
        covers a dropped connection."""
        threading.Thread(target=self._run, daemon=True, name="security-poll").start()
        pushed = sorted(set(self._watched()) | set(self.listeners)
                        | {c.charging for c in self.cfg.cameras.values() if c.charging})
        threading.Thread(target=self.ha.watch, args=(pushed, self.poll_once, self._stop),
                         daemon=True, name="security-push").start()

    def _watched(self) -> dict[str, tuple[str, str]]:
        """entity → (subject, kind) the poller logs."""
        w = {}
        L = self.cfg.lock
        if L:
            for e in (L.entity, L.status):
                if e:
                    w[e] = ("front_door_lock", "lock")
        for c in self.cfg.cameras.values():
            for kind in ("ring", "person", "motion"):
                e = getattr(c, kind)
                if e:
                    w[e] = (c.name, kind)
        return w

    def poll_once(self, states: dict[str, dict]) -> None:
        """React to `states` (all of HA's, from the poller, or one pushed
        change): log changes, show a ringing or moving camera, announce."""
        with self._poll_lock:
            self._poll_once(states)

    def _poll_once(self, states: dict[str, dict]) -> None:
        self._watch_charging(states)
        for entity, fns in self.listeners.items():
            s = states.get(entity)
            for fn in fns if s is not None else ():
                try:
                    fn(s)
                except Exception:
                    log.exception("Security: listener for %s failed", entity)
        for entity, (subject, kind) in self._watched().items():
            s = states.get(entity)
            if s is None:
                continue
            now, was = s["state"], self._seen.get(entity)
            if now in ("unavailable", "unknown") and was not in (None, "unavailable", "unknown"):
                continue         # a bridge restart: keep the last real state, so its
                                 # return isn't logged as a change
            self._seen[entity] = now
            if was is None or now == was:
                continue         # first look, or no change
            if kind == "lock":
                word = lock_word(now)
                if word in ("locked", "unlocked", "jammed"):
                    last = self.events.last(subject, ("locked", "unlocked", "jammed"))
                    if last and last["event"] == word and time.time() - last["ts"] < 30:
                        continue     # the entity and the status sensor both reported it
                    source = self.events.recent_source(subject, "lock_requested", 60) \
                        if word == "locked" else None
                    self.events.add(subject, word, source=source or "")
                    log.info("Security: front door %s%s", word, f" ({source})" if source else "")
            elif now == "on":
                log.info("Security: %s %s", subject, kind)
                if kind == "ring":
                    self.events.add(subject, kind)
                    cam = self.cfg.cameras.get(subject)
                    pip = cam.pip_camera if cam else None
                    threading.Thread(target=self._show_unasked, args=(subject, None, "a ring", pip),
                                     daemon=True, name=f"security-ring-{subject}").start()
                elif kind in ("motion", "person"):
                    self._check_motion_show(subject, kind)
                    cam = self.cfg.cameras.get(subject)
                    announced = (self._check_motion_announce(subject, kind, states=states)
                                 if cam and kind in cam.announce_on else None)
                    if not announced:
                        self.events.add(subject, kind)

    def _watch_charging(self, states: dict[str, dict]) -> None:
        """A charge ending (Charging → anything real) queues an add-on restart,
        done once no live view needs the bridge, and at most once an hour."""
        if not (self.cfg.eufy_addon or self.cfg.eufy_restart_button):
            return
        for c in self.cfg.cameras.values():
            s = states.get(c.charging) if c.charging else None
            if s is None or s["state"] in ("unavailable", "unknown"):
                continue
            key = f"charging:{c.charging}"
            was = self._seen.get(key)
            self._seen[key] = s["state"]
            if was == "Charging" and s["state"] != "Charging":
                self._charged = c.name
        if (self._charged and time.time() - self._last_restart > 3600
                and self._live() is None):
            name, self._charged = self._charged, None
            self._last_restart = time.time()
            threading.Thread(target=self._restart_bridge, args=(name,), daemon=True,
                             name="security-eufy-restart").start()

    def _restart_bridge(self, name: str) -> None:
        how = self.cfg.eufy_restart_button or self.cfg.eufy_addon
        log.info("Security: %s finished charging; restarting the Eufy bridge (%s) for a "
                 "fresh battery level", name, how)
        try:
            if self.cfg.eufy_restart_button:
                self.ha.call("input_button", "press", self.cfg.eufy_restart_button)
            else:
                self.ha.restart_addon(self.cfg.eufy_addon)
        except SecurityError as e:      # a slow restart can outlast the timeout; it still runs
            log.warning("Security: restarting the Eufy bridge (%s): %s", how, e)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once(self.ha.states())
                self._snapshots()
            except SecurityError as e:
                log.debug("Security poll: %s", e)
            except Exception:
                log.exception("Security poll failed")
            self._stop.wait(self.cfg.poll_seconds)

    def _in_snapshot_hours(self) -> bool:
        return in_hours(self.cfg.snapshot_hours)

    def _in_motion_hours(self) -> bool:
        return in_hours(self.cfg.motion_hours)

    def _check_motion_show(self, subject: str, kind: str) -> None:
        """Motion or a person at a show_on_motion camera: its live video up
        for that long, once per motion cooldown (motion and person arrive
        together; someone lingering doesn't keep re-opening it), and only
        during motion_hours, like the announcements."""
        cam = self.cfg.cameras.get(subject)
        if not cam or not cam.show_on_motion or self.pineda is None:
            return
        if not self._in_motion_hours():
            return               # asleep: no screen lighting up, no battery spent
        now = time.time()
        if now - self._last_motion_show.get(subject, 0.0) < self.cfg.motion_cooldown_seconds:
            return
        self._last_motion_show[subject] = now
        threading.Thread(target=self._show_unasked, args=(subject, cam.show_on_motion, kind, cam.pip_camera),
                         daemon=True, name=f"security-motion-{subject}").start()

    def _resolve_camera(self, name_or_alias: str) -> Camera | None:
        """Resolve a camera by key, title, entity, or stream."""
        if name_or_alias in self.cfg.cameras:
            return self.cfg.cameras[name_or_alias]
        target = name_or_alias.lower().replace("-", "_").replace(" ", "_")
        for k, c in self.cfg.cameras.items():
            if k.lower() == target:
                return c
            if c.title.lower() == name_or_alias.lower():
                return c
            if c.entity.lower() == name_or_alias.lower():
                return c
            if c.stream and c.stream.lower() == name_or_alias.lower():
                return c
        return None

    def announce_visitor(
        self,
        camera_name: str,
        people: Sequence[str] | None = None,
        has_unknown: bool = False,
        source: str = "",
    ) -> str | None:
        """Speak a visitor announcement for a camera if enabled and within motion hours.

        Returns the spoken phrase, or None if skipped/debounced.
        """
        if not self.announce:
            return None
        cam = self._resolve_camera(camera_name)
        if not cam or not cam.announce_motion:
            return None
        if not self._in_motion_hours():
            log.info("Security: skipping %s visitor announcement outside motion hours (%s)",
                     cam.name, self.cfg.motion_hours)
            return None
        now = time.time()
        last = self._last_motion_announce.get(cam.name, 0.0)
        cooldown = self.cfg.motion_cooldown_seconds
        if now - last < cooldown:
            log.debug("Security: %s visitor announcement debounced (%.1fs < %.1fs)",
                      cam.name, now - last, cooldown)
            return None
        self._last_motion_announce[cam.name] = now
        phrase = format_visitor_announcement(cam.title, people, has_unknown,
                                             fallback_announcement=cam.motion_announcement)
        if cam.show_on_motion and self.pineda is not None:
            # Its live video goes up too (_check_motion_show, same hours): say where.
            phrase = phrase.rstrip(" .") + ", live on Pineda Display."

        clean, unk = clean_names(people)
        unk = unk or has_unknown
        if clean and unk:
            detail = f"{', '.join(clean)} + guest"
        elif clean:
            detail = ", ".join(clean)
        elif unk:
            detail = "guest"
        else:
            detail = ""

        self.events.add(cam.name, "person", detail=detail, source=source)
        log.info("Security: announced visitor at %s: %r (detail: %r)", cam.name, phrase, detail)
        threading.Thread(target=self._do_announce, args=(phrase,), daemon=True,
                         name=f"security-announce-{cam.name}").start()
        return phrase

    def _check_motion_announce(
        self,
        subject: str,
        kind: str,
        states: dict[str, dict] | None = None,
    ) -> str | None:
        cam = self.cfg.cameras.get(subject)
        if not cam or not cam.announce_motion:
            return None
        people: list[str] = []
        has_unknown = False
        if states:
            if cam.person_name and cam.person_name in states:
                people, has_unknown = extract_people_from_state(states[cam.person_name])
            if not people:
                for sensor_kind in ("person", "motion"):
                    entity_id = getattr(cam, sensor_kind, None)
                    if entity_id and entity_id in states:
                        p, unk = extract_people_from_state(states[entity_id])
                        if p:
                            people.extend(p)
                        if unk:
                            has_unknown = True
        return self.announce_visitor(subject, people=people, has_unknown=has_unknown)

    def _do_announce(self, text: str) -> None:
        try:
            if callable(self.announce):
                self.announce(text)
        except Exception:
            log.exception("Security: announcement failed")

    def _snapshots(self) -> None:
        if self.cfg.snapshots_dir is None or not self._in_snapshot_hours():
            return
        for cam in self.cfg.cameras.values():
            if not cam.snapshot_minutes:
                continue
            due = self._snap_due.get(cam.name)
            if due is None:
                due = self._snap_due[cam.name] = self._first_due(cam)
            if time.time() < due:
                continue
            if self._live() is not None:
                continue          # a live view is using a camera
            self._snap_due[cam.name] = time.time() + cam.snapshot_minutes * 60
            threading.Thread(target=self.snapshot, args=(cam,), daemon=True,
                             name=f"security-snap-{cam.name}").start()

    def _first_due(self, cam: Camera) -> float:
        """After a restart, the next snapshot keeps the old schedule — a
        restart shouldn't wake every camera."""
        meta = self.cfg.snapshots_dir / f"{cam.name}.json"
        try:
            taken = json.loads(meta.read_text())["taken"]
            return taken + cam.snapshot_minutes * 60
        except (OSError, ValueError, KeyError):
            return time.time()

    def snapshot(self, cam: Camera) -> bool:
        """Wake the camera just long enough for one frame: livestream on,
        a JPEG from go2rtc, livestream off. Saved with its battery level for
        the display's traffic-cam card."""
        out = self.cfg.snapshots_dir
        out.mkdir(parents=True, exist_ok=True)
        jpeg = None
        try:
            self.ha.call("eufy_security", "start_p2p_livestream", cam.entity)
            deadline = time.time() + 20
            while time.time() < deadline and jpeg is None:
                time.sleep(2)
                try:
                    r = requests.get(f"{self.cfg.go2rtc}/api/frame.jpeg",
                                     params={"src": cam.stream}, timeout=10)
                    if r.ok and r.content[:2] == b"\xff\xd8":
                        jpeg = r.content
                except requests.RequestException:
                    pass
        except SecurityError as e:
            log.warning("Security: snapshot of %s: %s", cam.name, e)
        finally:
            if self._live() != cam.name:
                self._stop_stream(cam)
        battery = None
        try:
            battery = self.battery(cam.name)
        except SecurityError:
            pass
        meta = {"camera": cam.name, "title": cam.title, "battery": battery,
                "every": cam.snapshot_minutes,
                "low_battery": self.cfg.low_battery,
                "taken": time.time() if jpeg else None}
        if jpeg:
            tmp = out / f".{cam.name}.jpg"
            tmp.write_bytes(jpeg)
            tmp.chmod(0o644)   # the display reads these as another user; the unit's umask is 077
            tmp.replace(out / f"{cam.name}.jpg")
        else:
            try:     # keep the last photo's time, just refresh the battery
                old = json.loads((out / f"{cam.name}.json").read_text())
                meta["taken"] = old.get("taken")
            except (OSError, ValueError):
                pass
        tmp = out / f".{cam.name}.json"
        tmp.write_text(json.dumps(meta))
        tmp.chmod(0o644)
        tmp.replace(out / f"{cam.name}.json")
        log.info("Security: %s snapshot %s (battery %s%%)", cam.name,
                 "saved" if jpeg else "failed", battery)
        return jpeg is not None
