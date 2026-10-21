"""Who's home, from which phones are on the Wi-Fi, and reminders for arriving.

Each household member in server.yaml may list `devices:`, the MAC addresses
of their phones (on an iPhone: Settings > Wi-Fi > (i) > Private Wi-Fi Address,
with "Rotate Wi-Fi Address" off, so it stays the same on this network).
Presence ARP-scans for them: a sleeping iPhone still answers ARP from its
Wi-Fi chip, where it ignores pings. Someone is home while any of their devices
has answered in the last `away_after_minutes`; the grace period covers a phone
that misses a few scans.

Arrival reminders ("remind me to take out the trash when I get home") wait in
data/arrival_reminders.json and are spoken in the kitchen when that person's
phone comes back.
"""

import ipaddress
import json
import logging
import re
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread

from . import household, settings

log = logging.getLogger("antigua_core")

_MAC_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
_SCAN_LINE_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\b", re.IGNORECASE)


def _mac(raw) -> str | None:
    m = str(raw or "").strip().lower().replace("-", ":")
    return m if _MAC_RE.match(m) else None


def arp_scan(interface: str, targets: list[str] | None) -> dict[str, str]:
    """MAC -> IP of everything that answered. targets=None scans the subnet."""
    cmd = ["arp-scan", "-I", interface, "-q", "-x", "--retry=3", "--timeout=400"]
    cmd += targets if targets else ["--localnet"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Presence: arp-scan failed: %s", e)
        return {}
    seen = {}
    for line in out.splitlines():
        m = _SCAN_LINE_RE.match(line)
        if m:
            seen[m.group(2).lower()] = m.group(1)
    return seen


def neighbor_mac(ip: str) -> str | None:
    """The MAC the kernel has for a LAN address (a phone that just sent us a
    request is always in the table)."""
    try:
        out = subprocess.run(["ip", "neigh", "show", ip], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"lladdr\s+(\S+)", out)
    return _mac(m.group(1)) if m else None


class ArrivalReminders:
    """Reminders waiting for someone to get home."""

    TTL_DAYS = 30   # one nobody came home for is forgotten after this

    def __init__(self, path: Path | None = None):
        self.path = path or settings.DATA_DIR / "arrival_reminders.json"
        self._lock = Lock()
        try:
            self._rows: list[dict] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self._rows = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._rows, indent=1))
        tmp.replace(self.path)

    def add(self, person: str, message: str, by: str | None = None, kind: str = "remind") -> dict:
        row = {"id": uuid.uuid4().hex[:8], "person": person, "message": message,
               "by": by, "kind": kind, "created": time.time()}
        with self._lock:
            self._rows.append(row)
            self._save()
        return row

    def pending(self, person: str | None = None) -> list[dict]:
        cutoff = time.time() - self.TTL_DAYS * 86400
        with self._lock:
            return [r for r in self._rows if r["created"] > cutoff
                    and (person is None or r["person"] == person)]

    def take(self, person: str) -> list[dict]:
        """Remove and return this person's reminders (they just got home)."""
        cutoff = time.time() - self.TTL_DAYS * 86400
        with self._lock:
            due = [r for r in self._rows if r["person"] == person and r["created"] > cutoff]
            kept = [r for r in self._rows if r["person"] != person and r["created"] > cutoff]
            if len(kept) != len(self._rows):
                self._rows = kept
                self._save()
        return due


class Presence:
    """Polls the LAN for each person's devices; calls on_arrive(person)."""

    def __init__(self, cfg: dict, scan=arp_scan, on_arrive=None):
        p = cfg.get("presence") or {}
        self.interface = p.get("interface", "eno1")
        self.interval = float(p.get("scan_seconds", 30))
        self.away_after = float(p.get("away_after_minutes", 15)) * 60
        self.full_scan_every = float(p.get("full_scan_seconds", 120))
        self.scan = scan
        self.on_arrive = on_arrive or (lambda person: None)
        self.devices: dict[str, str] = {}          # MAC -> person
        for member in cfg.get("household") or []:
            for raw in member.get("devices") or []:
                mac = _mac(raw)
                if mac:
                    self.devices[mac] = member["name"]
                else:
                    log.warning("Presence: %r for %s is not a MAC address", raw, member["name"])
        self.people = sorted(set(self.devices.values()))
        self._ip: dict[str, str] = {}               # MAC -> last IP it answered from
        self._seen: dict[str, float] = {}           # MAC -> when it last answered
        self._home: dict[str, bool] = {}            # person -> home (absent: not settled)
        self._last_full = 0.0
        self._t0: float | None = None               # first scan
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.devices)

    def tracks(self, person: str) -> bool:
        return person in self.people

    def start(self):
        if not self.enabled:
            return
        log.info("Presence: tracking %d device(s) for %s", len(self.devices), ", ".join(self.people))
        Thread(target=self._loop, name="presence", daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception:   # noqa: BLE001 — a bad scan must not end the thread
                log.exception("Presence: scan failed")
            time.sleep(self.interval)

    def tick(self, now: float | None = None):
        """One scan: the known addresses, and the whole subnet when a device
        has no address yet or has gone quiet (its address may have changed)."""
        now = time.time() if now is None else now
        if self._t0 is None:
            self._t0 = now
        targets = sorted({self._ip[m] for m in self.devices if m in self._ip})
        quiet = [m for m in self.devices
                 if m not in self._ip or now - self._seen.get(m, 0) > 2 * self.interval]
        found = self.scan(self.interface, targets) if targets else {}
        if quiet and now - self._last_full >= self.full_scan_every:
            self._last_full = now
            found.update(self.scan(self.interface, None))
        arrived = []
        with self._lock:
            for mac, ip in found.items():
                if mac in self.devices:
                    self._ip[mac], self._seen[mac] = ip, now
            for person in self.people:
                home = any(now - self._seen.get(m, -1e12) <= self.away_after
                           for m, p in self.devices.items() if p == person)
                was = self._home.get(person)
                if was is None and not home and now - self._t0 < self.away_after:
                    continue   # just started and not seen yet: not "away" until the grace runs out
                self._home[person] = home
                if was is not None and home != was:
                    log.info("Presence: %s %s", person, "arrived home" if home else "left")
                    if home:
                        arrived.append(person)
                elif was is None:
                    log.info("Presence: %s is %s", person, "home" if home else "away")
        for person in arrived:
            self.on_arrive(person)

    def is_home(self, person: str) -> bool | None:
        """True/False, or None when they aren't tracked or it isn't known yet."""
        with self._lock:
            return self._home.get(person)

    def last_seen(self, person: str) -> float | None:
        with self._lock:
            times = [self._seen[m] for m, p in self.devices.items() if p == person and m in self._seen]
        return max(times) if times else None

    def snapshot(self) -> dict:
        return {p: {"home": self.is_home(p), "last_seen": self.last_seen(p)} for p in self.people}

    def person_for_ip(self, ip: str, lookup=neighbor_mac) -> str | None:
        """Whose phone is at this address, for a request it just made."""
        with self._lock:
            for mac, known in self._ip.items():
                if known == ip:
                    return self.devices[mac]
        try:
            if not ipaddress.ip_address(ip).is_private:
                return None
        except ValueError:
            return None
        mac = lookup(ip)
        return self.devices.get(mac) if mac else None


# ── Phrasing ─────────────────────────────────────────────────────────────────


def _names_alt() -> str:
    return household.people()["alt"]


_WHO_HOME_RE = re.compile(
    r"^\s*(?:hey\s+antigua[,\s]+)?(?:(?:do\s+you\s+know|tell\s+me)\s+)?"
    r"(?:who(?:'s|\s+is|\s+all\s+is)\s+(?:at\s+)?home"
    r"|is\s+(?:any(?:one|body)|some(?:one|body))\s+(?:at\s+)?home"
    r"|is\s+the\s+house\s+empty"
    r"|(?:is\s+)?(?:every(?:one|body))\s+(?:at\s+)?home)\b",
    re.IGNORECASE)


def _person_home_re():
    alt = _names_alt()
    return re.compile(
        rf"^\s*(?:hey\s+antigua[,\s]+)?(?:(?:is|has)\s+({alt})\s+(?:at\s+home|home|back(?:\s+home)?|"
        rf"(?:gotten|got|come|made\s+it)\s+(?:back\s+)?home)(?:\s+yet)?"
        rf"|did\s+({alt})\s+(?:get|come|make\s+it)\s+(?:back\s+)?home(?:\s+yet)?)\s*[?.!]*\s*$",
        re.IGNORECASE)


def parse_presence_query(text: str) -> str | None:
    """'*' for "who's home", a household name for "is Alex home", else None."""
    if not text:
        return None
    if _WHO_HOME_RE.search(text):
        return "*"
    m = _person_home_re().match(text)
    if m:
        return household.canonical(m.group(1) or m.group(2))
    return None


_ARRIVE = (r"(?:get(?:s)?|come(?:s)?|arrive(?:s)?|make(?:s)?\s+it|am|is|are|'m|'s|'re)\s+"
           r"(?:back\s+)?home|(?:get(?:s)?|come(?:s)?)\s+back")


def _arrival_res():
    alt = _names_alt()
    who = rf"(?:i|we|he|she|they|{alt})"
    clause = re.compile(
        rf"[,\s]*\b(?:when(?:ever)?|once|as\s+soon\s+as|after)\s+(?P<who>{who})(?:'m|'s|'re)?\s*"
        rf"(?:{_ARRIVE})\b[,\s]*", re.IGNORECASE)
    ask = re.compile(
        rf"^\s*(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(?P<verb>remind|tell)\s+"
        rf"(?P<target>me|us|him|her|them|{alt})\b\s*(?:that\s+)?(?P<msg>.*?)\s*[.!?]*\s*$",
        re.IGNORECASE)
    return clause, ask


_TO_YOU = [(re.compile(rf"\b{a}\b", re.IGNORECASE), b) for a, b in (
    ("myself", "yourself"), ("my", "your"), ("me", "you"), ("i'm", "you're"), ("i", "you"),
    ("himself", "yourself"), ("herself", "yourself"), ("his", "your"), ("him", "you"),
    ("their", "your"), ("them", "you"), ("her", "your"))]


def _to_second_person(msg: str) -> str:
    for rx, sub in _TO_YOU:
        msg = rx.sub(sub, msg)
    return msg


def parse_arrival_reminder(text: str, speaker: str | None):
    """"Remind me to X when I get home" -> (person, message, kind); kind is
    "remind" (message reads after "remind you") or "tell" (after "tell you").
    person is None when it's "me" and the speaker isn't known. None when this
    isn't an arrival reminder."""
    if not text:
        return None
    clause_re, ask_re = _arrival_res()
    m = clause_re.search(text)
    if not m:
        return None
    who = m.group("who").lower()
    rest = (text[:m.start()] + " " + text[m.end():]).strip(" ,")
    a = ask_re.match(rest)
    if not a:
        return None
    target = a.group("target").lower()
    named = household.canonical(target) or household.canonical(who)
    if target in ("me", "us") or (who in ("i", "we") and target not in ("him", "her", "them")
                                   and not household.canonical(target)):
        person = speaker
    else:
        person = named
    msg = re.sub(r"\s+", " ", a.group("msg")).strip(" ,")
    if not msg:
        return None
    kind = a.group("verb").lower()
    if kind == "remind" and not re.match(r"(?:to|about|that)\b", msg, re.IGNORECASE):
        msg = f"to {msg}"
    if kind == "tell" and re.match(r"to\b", msg, re.IGNORECASE):
        kind = "remind"     # "tell Alex to check the mail" is a reminder
    return person, _to_second_person(msg[:1].lower() + msg[1:]), kind


def set_reply(person: str, speaker: str | None, presence: "Presence | None") -> str:
    you = person == speaker
    whom, who = ("you", "you get") if you else (person, "they get")
    reply = f"Okay, I'll remind {whom} when {who} home."
    if presence is not None and not presence.tracks(person):
        reply += (f" But I can't tell when {who} home yet: I don't know which phone is "
                  + ("yours." if you else f"{person}'s."))
    elif presence is not None and presence.is_home(person):
        reply = (f"Okay. {'You are' if you else person + ' is'} home now, "
                 f"so I'll remind {whom} the next time {who} home.")
    return reply


def arrival_text(person: str, rows: list[dict]) -> str:
    parts = []
    for r in rows:
        verb = "tell you" if r.get("kind") == "tell" else "remind you"
        asker = "You" if not r.get("by") or r["by"] == person else r["by"]
        parts.append(f"{asker} asked me to {verb} {r['message']}.")
    return f"Welcome home, {person}. " + " ".join(parts)


def _clock(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    day = "" if d.date() == datetime.now().date() else (
        " yesterday" if (datetime.now().date() - d.date()).days == 1 else d.strftime(" on %A"))
    return d.strftime("%-I:%M %p") + day


def query_reply(who: str, presence: "Presence | None") -> str:
    if presence is None or not presence.enabled:
        return "I can't tell who's home yet. Nobody's phone is set up for me to watch."
    if who != "*":
        if not presence.tracks(who):
            return f"I don't know which phone is {who}'s, so I can't tell if they're home."
        home = presence.is_home(who)
        if home is None:
            return f"I'm still checking for {who}'s phone. Ask me again in a minute."
        if home:
            return f"Yes, {who} is home."
        seen = presence.last_seen(who)
        return f"No, {who} isn't home." + (f" Their phone was last here at {_clock(seen)}." if seen else "")
    home = [p for p in presence.people if presence.is_home(p)]
    away = [p for p in presence.people if presence.is_home(p) is False]
    if not home and not away:
        return "I'm still checking. Ask me again in a minute."
    if not home:
        return "Nobody's home right now."
    names = _join(home)
    reply = f"{names} {'is' if len(home) == 1 else 'are'} home."
    if away:
        reply += f" {_join(away)} {'is' if len(away) == 1 else 'are'} out."
    return reply


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
