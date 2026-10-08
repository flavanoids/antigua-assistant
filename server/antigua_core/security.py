"""Home security through Home Assistant: the Eufy doorbell and garage
cameras, and the front door lock.

HA's eufy_security integration does the Eufy side. Asked to show a camera,
we start its P2P livestream (HA pushes it into go2rtc), put PinedaDisplay's
camera card up, and stop the livestream the moment the card is gone — dismissed,
replaced or timed out — so a battery camera isn't left streaming.

A doorbell ring puts that camera up on its own; motion and person
detections don't (too many false alarms), they're only logged.

Every lock change, doorbell ring and person/motion detection is written to a
local SQLite log by a poller, so "when was the front door locked?" and "who
triggered the garage cam?" are answered from here, not from HA's history,
and survive HA's recorder purge. The poller also refreshes each camera's
snapshot for the display's traffic-cam card on its own (slow) schedule.

There is no unlock: see intents/security.py.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

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

    def states(self) -> dict[str, dict]:
        return {s["entity_id"]: s for s in self._req("GET", "/api/states") or []}

    def state(self, entity_id: str) -> dict | None:
        return self._req("GET", f"/api/states/{entity_id}")

    def call(self, domain: str, service: str, entity_id: str, **data) -> None:
        self._req("POST", f"/api/services/{domain}/{service}",
                  json={"entity_id": entity_id, **data})


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
    snapshot_minutes: float = 0   # 0 = no snapshots for the display


@dataclass
class Lock:
    entity: str               # lock.front_door — disabled until the shared Eufy
                              # account may operate it
    status: str | None = None # sensor.front_door_lock_status — readable regardless
    battery: str | None = None
    title: str = "front door"


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
                   snapshot_hours=d.get("snapshot_hours", cls.snapshot_hours))


# ── the event log ────────────────────────────────────────────────────────────

class EventLog:
    """Append-only: (ts, subject, event, detail, source).

    subject: "front_door_lock" | a camera name. event: locked, unlocked,
    jammed, ring, person, motion. source: "voice:Sam", "voice", or "" for
    a change seen from HA (keypad, app, auto-lock)."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (ts REAL, subject TEXT,"
                       " event TEXT, detail TEXT, source TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS events_by ON events (subject, event, ts)")

    def _db(self):
        return sqlite3.connect(self._path, timeout=5)

    def add(self, subject: str, event: str, detail: str = "", source: str = "",
            ts: float | None = None) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                       (ts or time.time(), subject, event, detail, source))

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


class Security:
    def __init__(self, ha: HomeAssistant, cfg: Config, events: EventLog, pineda=None):
        self.ha = ha
        self.cfg = cfg
        self.events = events
        self.pineda = pineda
        self._live: dict | None = None          # {camera, until, token}
        self._live_lock = threading.Lock()
        self._seen: dict[str, str] = {}         # entity → last state the poller saw
        self._snap_due: dict[str, float] = {}
        self._stop = threading.Event()

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

    # ── live view ──

    def show_camera(self, name: str, seconds: float | None = None) -> None:
        """Start the camera's livestream and put it on the display; a watcher
        stops the livestream when the card comes down."""
        cam = self.cfg.cameras[name]
        seconds = seconds or self.cfg.live_seconds
        self.ha.call("eufy_security", "start_p2p_livestream", cam.entity)
        try:
            self.pineda.show_takeover(
                {"kind": "camera", "camera": name, "title": cam.title, "caption": ""}, seconds)
        except Exception:
            self._stop_stream(cam)
            raise
        with self._live_lock:
            previous = self._live
            token = object()
            self._live = {"camera": name, "until": time.time() + seconds, "token": token}
        if previous and previous["camera"] != name:
            self._stop_stream(self.cfg.cameras[previous["camera"]])
        threading.Thread(target=self._watch_live, args=(name, token), daemon=True,
                         name=f"security-live-{name}").start()

    def _show_ring(self, name: str) -> None:
        """Someone rang: the doorbell's live video goes up on the display."""
        try:
            self.show_camera(name)
        except Exception:
            log.exception("Security: showing the %s camera for a ring", name)

    def caption(self, text: str) -> None:
        self.pineda.update_takeover({"caption": text})

    def _card_up(self, name: str) -> bool:
        try:
            cur = self.pineda.takeover()
        except Exception:
            return True          # can't tell; the deadline still stops it
        card = (cur or {}).get("card") or {}
        return card.get("kind") == "camera" and card.get("camera") == name

    def _watch_live(self, name: str, token) -> None:
        time.sleep(2)
        while True:
            with self._live_lock:
                live = self._live
                if live is None or live["token"] is not token:
                    return       # replaced by another show; that one owns the stream
                done = time.time() >= live["until"] or not self._card_up(name)
                if done:
                    self._live = None
            if done:
                self._stop_stream(self.cfg.cameras[name])
                return
            time.sleep(1.5)

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
        threading.Thread(target=self._run, daemon=True, name="security-poll").start()

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
        for entity, (subject, kind) in self._watched().items():
            s = states.get(entity)
            if s is None:
                continue
            now, was = s["state"], self._seen.get(entity)
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
                self.events.add(subject, kind)
                log.info("Security: %s %s", subject, kind)
                if kind == "ring":   # motion and person are too noisy to show
                    threading.Thread(target=self._show_ring, args=(subject,), daemon=True,
                                     name=f"security-ring-{subject}").start()

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
        try:
            start, end = (int(h) * 60 + int(m) for h, m in
                          (x.split(":") for x in self.cfg.snapshot_hours.split("-")))
        except ValueError:
            return True
        now = time.localtime()
        minute = now.tm_hour * 60 + now.tm_min
        return start <= minute < end if start <= end else (minute >= start or minute < end)

    def _snapshots(self) -> None:
        if self.cfg.snapshots_dir is None or not self._in_snapshot_hours():
            return
        for cam in self.cfg.cameras.values():
            if not cam.snapshot_minutes:
                continue
            due = self._snap_due.get(cam.name)
            if due is None:
                due = self._snap_due[cam.name] = self._first_due(cam)
            if time.time() < due or self._live:
                continue          # not yet, or a live view is using a camera
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
            if not (self._live and self._live["camera"] == cam.name):
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
            tmp.replace(out / f"{cam.name}.jpg")
        else:
            try:     # keep the last photo's time, just refresh the battery
                old = json.loads((out / f"{cam.name}.json").read_text())
                meta["taken"] = old.get("taken")
            except (OSError, ValueError):
                pass
        tmp = out / f".{cam.name}.json"
        tmp.write_text(json.dumps(meta))
        tmp.replace(out / f"{cam.name}.json")
        log.info("Security: %s snapshot %s (battery %s%%)", cam.name,
                 "saved" if jpeg else "failed", battery)
        return jpeg is not None
