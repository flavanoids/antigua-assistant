"""Announcements on other speakers: "tell the bedroom dinner's ready".

The words are synthesized in Antigua's voice and played through Music
Assistant's announcement command, which plays a chime, lowers any music on
that speaker, plays the clip and puts the music back. "kitchen" is the
satellite (MQTT antigua/play), not a Music Assistant player.

server.yaml:
  announce:
    rooms: {bedroom: "Bedroom (2)", upstairs: "Bedroom (2)", kitchen: kitchen}
    default: [bedroom]           # "announce X" said in the kitchen
    everywhere: [kitchen, bedroom]
    volume: 45                   # the speaker's level for the announcement
    chime: true
"""

import logging
import re
from threading import Thread

from .music import MusicError

log = logging.getLogger("antigua_core")

KITCHEN = "kitchen"
_EVERY_RE = r"every(?:one|body|where)|the\s+(?:whole\s+)?house|all\s+(?:the\s+)?(?:rooms|speakers)"


class Announcer:
    def __init__(self, cfg: dict, ma=None):
        a = cfg.get("announce") or {}
        self.ma = ma                                 # music.MusicAssistant, or None
        self.rooms: dict[str, str] = {k.lower(): v for k, v in (a.get("rooms") or {}).items()}
        self.default = [r.lower() for r in a.get("default") or [r for r in self.rooms if r != KITCHEN][:1]]
        self.everywhere = [r.lower() for r in a.get("everywhere") or list(self.rooms)]
        self.volume = a.get("volume", 45)
        self.chime = bool(a.get("chime", True))
        self.background = True                       # tests turn this off
        self._ids: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.rooms)

    # ── parsing ─────────────────────────────────────────────────────────

    def _room_alt(self) -> str:
        names = sorted(self.rooms, key=len, reverse=True)
        return "|".join(re.escape(n) for n in names) or "(?!)"

    def parse(self, text: str):
        """(rooms or None for the default, message) or None."""
        if not text or not self.enabled:
            return None
        rooms = self._room_alt()
        place = rf"(?:(?:in|to|on)\s+)?(?:the\s+)?(?P<room>{rooms})(?:\s+speaker|\s+homepod)?|(?P<all>{_EVERY_RE})"
        s = re.sub(r"^\s*(?:hey\s+)?antigua[,\s]+", "", text.strip(), flags=re.IGNORECASE)
        s = re.sub(r"^(?:please\s+|can\s+you\s+|could\s+you\s+)+", "", s, flags=re.IGNORECASE)
        # "tell the bedroom (that) X", "tell everyone X"
        m = re.match(rf"^(?:tell|let)\s+(?:{place})\s+(?:know\s+)?(?:that\s+)?(?P<msg>.+)$", s, re.IGNORECASE)
        if not m:
            # "announce (that) X [in the bedroom]", "announce in the bedroom that X"
            m = re.match(rf"^(?:announce|broadcast)\s+(?:{place})\s+(?:that\s+)?(?P<msg>.+)$", s, re.IGNORECASE)
        if not m:
            m = re.match(rf"^(?:make\s+an\s+announcement|announce|broadcast)[:,]?\s+(?:that\s+)?(?P<msg>.+?)"
                         rf"(?:[,\s]+(?:{place}))?\s*[.!?]*$", s, re.IGNORECASE)
        if not m:
            return None
        msg = m.group("msg").strip(" ,.!?")
        if not msg or len(msg.split()) < 2 and not m.groupdict().get("room"):
            return None
        if m.groupdict().get("all"):
            where = list(self.everywhere)
        elif m.groupdict().get("room"):
            where = [m.group("room").lower()]
        else:
            where = None
        return where, msg

    # ── playing ─────────────────────────────────────────────────────────

    def _player_id(self, name: str) -> tuple[str, bool]:
        players = [p for p in self.ma.cmd("players/all") or []
                   if p.get("name", "").lower() == name.lower()]
        players.sort(key=lambda p: (not p.get("available"), p.get("type") != "player"))
        if not players:
            raise MusicError(f"no Music Assistant player named {name!r}")
        return players[0]["player_id"], bool(players[0].get("available"))

    def _play(self, player_id: str, url: str, name: str):
        try:
            self.ma.cmd("players/cmd/play_announcement", timeout=120, player_id=player_id,
                        url=url, pre_announce=self.chime, volume_level=self.volume)
        except MusicError as e:
            log.warning("Announce on %s failed: %s", name, e)

    def announce(self, rooms: list[str], url: str, kitchen_play=None) -> tuple[list[str], list[str]]:
        """Start the clip on each room; (rooms it went to, rooms that failed)."""
        done, failed = [], []
        for room in dict.fromkeys(rooms):
            player = self.rooms.get(room)
            if player is None:
                failed.append(room)
                continue
            if player == KITCHEN:
                if kitchen_play is not None:
                    kitchen_play(url)
                    done.append(room)
                continue
            if self.ma is None:
                failed.append(room)
                continue
            try:
                pid, available = self._player_id(player)
            except MusicError as e:
                log.warning("Announce: %s", e)
                failed.append(room)
                continue
            if not available:
                failed.append(room)
                continue
            if self.background:
                Thread(target=self._play, args=(pid, url, player), daemon=True).start()
            else:
                self._play(pid, url, player)
            done.append(room)
        log.info("Announced on %s%s", ", ".join(done) or "nothing",
                 f" (failed: {', '.join(failed)})" if failed else "")
        return done, failed


def spoken_message(msg: str, speaker: str | None) -> str:
    text = msg[:1].upper() + msg[1:]
    if not re.search(r"[.!?]$", text):
        text += "."
    return f"{speaker} says: {text}" if speaker else text


def _place(room: str) -> str:
    return "upstairs" if room == "upstairs" else f"the {room}"


def reply(done: list[str], failed: list[str]) -> str:
    if not done:
        return (f"I couldn't reach {_join([_place(r) for r in failed])}."
                if failed else "I don't have anywhere to announce that.")
    out = f"Okay, I announced it in {_join([_place(r) for r in done])}."
    if failed:
        out += f" I couldn't reach {_join([_place(r) for r in failed])}."
    return out.replace("in upstairs", "upstairs")


def _join(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + f" and {xs[-1]}"


# The server's Announcer, so classify() can recognise room names without a
# backend in hand; None = no announce: block, and nothing parses as one.
active: Announcer | None = None


def install(a: Announcer | None) -> None:
    global active
    active = a


def parse(text: str):
    return active.parse(text) if active is not None else None
