"""Music skill: Apple Music through Music Assistant, driven deterministically.

music_intents.parse_music() decides what was asked; this module resolves it
against Music Assistant's catalogue search (Apple Music) and plays it on a
Music Assistant player (an AirPlay speaker). Replies are templates — the LLM
is never involved.

Music Assistant runs on the primary (server/music/docker-compose.yml). Both
Antigua servers talk to it over its HTTP API (POST /api, bearer token), so
music keeps working from the backup as long as the primary's container is up.

Why the native API and not MA's built-in MCP server: the MCP's album briefs
drop album_type, and "newest album" needs it to skip singles and EPs.
"""

import difflib
import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Lock, RLock, Thread, Timer

import requests

from . import settings
from .music_intents import MusicIntent, parse_music

log = logging.getLogger("antigua_core.music")


class MusicError(Exception):
    pass


def _norm(s: str) -> str:
    """Lowercase, accents and punctuation off, feat./versions dropped."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", s)              # (Deluxe), [Remastered]
    s = re.sub(r"\s+-\s+(single|ep)$", "", s)
    s = re.sub(r"\b(feat|ft)\.?\s.*$", "", s)
    s = s.replace("&", "and").replace("+", "plus")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _sound(s: str) -> str:
    """Crude sounds-like key, so "chapel room" lands near "chappell roan"."""
    s = _norm(s)
    for a, b in (("ph", "f"), ("ck", "k"), ("qu", "kw"), ("x", "ks"), ("y", "i"), ("z", "s")):
        s = s.replace(a, b)
    s = re.sub(r"c(?=[aou]|\b)", "k", s)
    s = re.sub(r"(?<=[a-z])h", "", s)
    return re.sub(r"([a-z])\1+", r"\1", s)


# Apple's public top-100 feeds: popular names that aren't in the library yet.
_CHARTS = [f"https://rss.marketingtools.apple.com/api/v2/us/music/most-played/100/{k}.json"
           for k in ("songs", "albums")]


# Deezer's public search (no key) as a spelling corrector for names the user
# asks for that aren't in the library: its fuzzy search turns "chapel room"
# into Chappell Roan and reports popularity, which MA's search doesn't.
# Names only; playback stays on Apple Music.
_DEEZER = "https://api.deezer.com/search/{}"
_POPULAR_FANS = 50_000      # artist nb_fan
_POPULAR_RANK = 600_000     # track rank (Espresso ~980k; filler ~40k)


def _artists(item: dict) -> str:
    names = [a.get("name", "") for a in item.get("artists") or [] if a.get("name")]
    if not names:
        return ""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _spoken_list(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


class MusicAssistant:
    """Sync client for Music Assistant's JSON API."""

    def __init__(self, url: str, token: str, timeout: float = 10):
        self.url = url.rstrip("/") + "/api"
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["Authorization"] = f"Bearer {token}"

    def cmd(self, command: str, timeout: float | None = None, **args):
        try:
            r = self._s.post(self.url, json={"command": command, "args": args,
                                             "message_id": uuid.uuid4().hex[:8]},
                             timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise MusicError(f"{command}: {e}") from e
        if r.status_code != 200:
            raise MusicError(f"{command}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.content else None


@dataclass
class _Pending:
    """The last albums Antigua read out, so "play the second one" works."""
    albums: list
    artist: str
    at: float


class MusicControl:
    PENDING_TTL = 600          # a spoken album list stays pickable for 10 minutes

    def __init__(self, cfg: dict, token: str):
        m = cfg.get("music") or {}
        self.ma = MusicAssistant(m.get("url", "http://127.0.0.1:8095"), token,
                                 float(m.get("timeout_seconds", 10)))
        # "library" too: MA returns anything already in the Apple Music
        # library as a library item and drops it from the apple_music results.
        self.providers = m.get("providers") or ["library", "apple_music"]
        self.catalog = m.get("catalog_provider", "apple_music")
        self.default_player = settings.MUSIC_DEFAULT_PLAYER
        # MA player name → how Antigua says it ("on the soundbar")
        self.labels = m.get("labels") or {}
        self.track_radio = bool(m.get("track_radio", True))   # keep going after a song, like Alexa
        self.lyrics_search = bool(m.get("lyrics_search", True))
        # Volume a player starts at when music begins from idle (None = leave it)
        self.default_volume = m.get("default_volume")
        self._ids: dict[str, str] = {}        # MA player name → player_id
        self._active: str | None = None       # player name Antigua last played on
        self._pending: _Pending | None = None
        # Names Whisper might have misheard (library artists/albums + charts),
        # offered back to it as hints when a request matches nothing well.
        self._vocab: list[str] = []
        self._vocab_at = 0.0
        self._vocab_loading = False
        self._rehear = None                   # set per request by handle()
        self.play_in_background = True        # tests turn this off
        self._played_path = settings.DATA_DIR / "music_played.json"
        try:
            self._played: set[str] = set(json.loads(self._played_path.read_text()))
        except (OSError, ValueError):
            self._played = set()
        self._lock = Lock()
        # Ducking: players near the kitchen mic drop to duck_ratio of their
        # volume while the user is talking (the bridge's reSpeaker VAD says
        # when that starts and stops), so commands don't have to be yelled.
        # [] turns it off, e.g. when the speaker's host ducks locally (the
        # satellite does, off the same MQTT message, ~30x faster).
        self.duck_players = m.get("duck_players", [self.default_player])
        self.duck_ratio = float(m.get("duck_ratio", 0.25))
        self.duck_timeout = float(m.get("duck_timeout_seconds", 25))
        self._ducked: dict[str, int] = {}     # player name → volume to restore
        self._duck_lock = RLock()   # _apply_listening holds it across _duck/_unduck
        self._duck_timer: Timer | None = None
        self._listening = False               # latest antigua/listening state

    # ── players ──────────────────────────────────────────────────────────

    def _player_name(self, speaker_key: str | None) -> str:
        if speaker_key:
            return settings.MUSIC_SPEAKERS[speaker_key]
        return self.default_player

    def _player_id(self, name: str) -> str:
        """The player Music Assistant shows by that name. players/get_by_name
        can return a per-protocol child (e.g. the AirPlay side of a speaker
        MA merged into a "universal player"); queues live on the parent."""
        if name not in self._ids:
            players = [p for p in self.ma.cmd("players/all") or []
                       if p.get("name", "").lower() == name.lower()]
            players.sort(key=lambda p: (p.get("type") != "player", not p.get("available")))
            if not players:
                raise MusicError(f"no Music Assistant player named {name!r}")
            self._ids[name] = players[0]["player_id"]
        return self._ids[name]

    def playing(self) -> bool:
        """Is any configured player playing right now (ducked counts)? Decides
        whether a bare "turn it up" is for the music or the TV."""
        if self._ducked:
            return True
        for name in dict.fromkeys([self._active or self.default_player,
                                   *settings.MUSIC_SPEAKERS.values()]):
            try:
                q = self.ma.cmd("player_queues/get", queue_id=self._player_id(name), timeout=3)
            except MusicError:
                continue
            if q and q.get("state") == "playing":
                return True
        return False

    def _where(self, name: str) -> str:
        """ " on the soundbar" — empty for the default speaker."""
        if name == self.default_player:
            return ""
        return " on " + self.labels.get(name, f"the {name}")

    def _control_target(self, speaker_key: str | None) -> str:
        """Which player a pause/next/etc. is for: the one named, else the one
        Antigua last started, else whatever configured player is playing,
        else the default."""
        if speaker_key:
            return self._player_name(speaker_key)
        if self._active:
            return self._active
        for name in dict.fromkeys(settings.MUSIC_SPEAKERS.values()):
            try:
                q = self.ma.cmd("player_queues/get", queue_id=self._player_id(name))
            except MusicError:
                continue
            if q and q.get("state") == "playing":
                return name
        return self.default_player

    # ── catalogue ────────────────────────────────────────────────────────

    def _search(self, query: str, types: list[str], limit: int = 10) -> dict:
        return self.ma.cmd("music/search", search_query=query, media_types=types,
                           limit=limit, providers=self.providers) or {}

    def _load_vocab(self):
        names = set()
        try:
            for kind in ("artists", "albums"):
                off = 0
                while True:
                    page = self.ma.cmd(f"music/{kind}/library_items", limit=500, offset=off,
                                       timeout=60) or []
                    names.update(x["name"] for x in page if x.get("name"))
                    if len(page) < 500:
                        break
                    off += 500
            for url in _CHARTS:   # nice to have; the library names matter more
                try:
                    for x in requests.get(url, timeout=10).json()["feed"]["results"]:
                        names.update((x["name"], x["artistName"]))
                except Exception as e:
                    log.warning("Music: chart names unavailable: %s", e)
            self._vocab, self._vocab_at = sorted(names), time.time()
            log.info("Music: %d names for re-hearing", len(names))
        except Exception as e:
            log.warning("Music: loading re-hearing names failed: %s", e)
        finally:
            self._vocab_loading = False

    def refresh_names(self):
        """Reload library + chart names in the background (every 12h at most)."""
        if time.time() - self._vocab_at > 12 * 3600 and not self._vocab_loading:
            self._vocab_loading = True
            Thread(target=self._load_vocab, daemon=True, name="music-vocab").start()

    def _popular(self, heard: str) -> set[str]:
        """Popular artists, songs and albums Deezer finds for the heard text.
        Empty when Deezer is unreachable — re-hearing then uses known names only."""
        def get(kind):
            try:
                r = requests.get(_DEEZER.format(kind), params={"q": heard, "limit": 10}, timeout=3)
                return r.json().get("data") or []
            except Exception as e:
                log.warning("Music: Deezer %s search failed: %s", kind, e)
                return []
        with ThreadPoolExecutor(3) as ex:
            artists, tracks, albums = ex.map(get, ("artist", "track", "album"))
        names = {a["name"] for a in artists if (a.get("nb_fan") or 0) >= _POPULAR_FANS}
        for t in tracks:
            if (t.get("rank") or 0) >= _POPULAR_RANK:
                names.update(n for n in (t.get("title"), (t.get("artist") or {}).get("name")) if n)
        names.update(a["title"] for a in albums if a.get("record_type") == "album"
                     and (a.get("artist") or {}).get("name") in names)
        return names

    def hint_names(self, heard: str, k: int = 15, popular=()) -> list[str]:
        """Up to k names that sound most like what Whisper heard: Deezer's
        popular matches first, then known names."""
        cands = set(self._vocab) | self._played
        try:   # Apple's own spelling correction ("billy eyelash" → Billie Eilish)
            for items in self._search(heard, ["artist", "track", "album"], 5).values():
                if isinstance(items, list):
                    cands.update(i["name"] for i in items if i.get("name"))
        except MusicError:
            pass
        parts = [_sound(p) for p in re.split(r"\s+by\s+", heard) if p.strip()]

        def score(name):
            key = _sound(name)
            return max(difflib.SequenceMatcher(None, p, key).ratio() for p in parts)
        top = sorted(popular, key=score, reverse=True)[:k // 2]
        return top + [n for n in sorted(cands, key=score, reverse=True) if n not in top][:k - len(top)]

    def _learn(self, *names):
        """Anything played is a name worth recognising next time."""
        new = {n for n in names if n} - self._played
        if new:
            self._played |= new
            try:
                self._played_path.write_text(json.dumps(sorted(self._played)))
            except OSError as e:
                log.warning("Music: saving played names failed: %s", e)

    def _known(self, text: str, extra=()) -> bool:
        self.refresh_names()
        keys = {_norm(n) for n in (*self._vocab, *self._played, *extra)}
        return all(_norm(p) in keys for p in re.split(r"\s+by\s+", text) if p.strip())

    def _reheard(self, it: MusicIntent) -> MusicIntent:
        """Whisper mishears names over music, and the catalogue is big enough
        that the mishearing is usually some real, obscure song ("Chapel Room"
        by Blind Bullets). So when the name isn't one we know (library, charts,
        played before) or a popular one on Deezer, re-transcribe with
        sound-alike names as hints and take that if it now names one of them.
        Whisper re-listening is the check: Deezer's guess alone ("tauro imoa"
        → Taio Cruz) is often wrong."""
        heard = it.raw or f"{it.query} {it.artist}".strip()
        if (not self._rehear or it.action in ("resume", "pick", "lyrics")
                or not heard or self._known(heard)):
            return it
        popular = self._popular(heard)
        if self._known(heard, popular):
            return it
        hints = self.hint_names(heard, popular=popular)
        try:
            new = parse_music(self._rehear(hints) or "")
        except Exception as e:
            log.warning("Music: re-hearing failed: %s", e)
            return it
        again = new and new.kind == "play" and (new.raw or f"{new.query} {new.artist}".strip())
        if not again or _norm(again) == _norm(heard) or not self._known(again, popular):
            log.info("Music: kept %r (re-heard %r; hints: %s)", heard, again, hints[:5])
            return it
        log.info("Music: re-heard %r as %r", heard, again)
        return replace(new, speaker=new.speaker or it.speaker)

    def _find_artist(self, name: str) -> dict | None:
        artists = self._search(name, ["artist"], 5).get("artists") or []
        best = max(artists, key=lambda a: _sim(a["name"], name), default=None)
        return best if best and _sim(best["name"], name) >= 0.8 else None

    def _catalog_ref(self, item: dict) -> tuple[str, str]:
        """(item_id, provider) in the Apple Music catalogue. A library item's
        own albums are only the ones saved to the library, not the discography."""
        for m in item.get("provider_mappings") or []:
            if m.get("provider_domain") == self.catalog:
                return m["item_id"], m["provider_instance"]
        return item["item_id"], item["provider"]

    def _albums(self, artist: dict) -> list[dict]:
        item_id, provider = self._catalog_ref(artist)
        albums = self.ma.cmd("music/artists/artist_albums", item_id=item_id,
                             provider_instance_id_or_domain=provider) or []
        # Clean/explicit twins and re-releases: keep the first of each name.
        seen, out = set(), []
        for a in albums:
            key = (_norm(a["name"]), a.get("album_type"))
            if key not in seen:
                seen.add(key)
                out.append(a)
        return out

    # Apple types plenty of singles, remix packs and EPs as "album"; the
    # version string and name suffix give them away. Anniversary/remaster
    # reissues would otherwise pose as the newest album (B'DAY, 2026).
    _NOT_STUDIO_NAME = re.compile(r"(?:-\s*EP|\bEP|remix(?:es)?|remix\s+pack)\s*$|video\s+album|karaoke",
                                  re.IGNORECASE)
    _NOT_STUDIO = re.compile(r"\b(single|remix(es)?|mixes|instrumentals?|ep|anniversary|remaster(ed)?|"
                             r"video\s+album|karaoke|live)\b", re.IGNORECASE)

    @classmethod
    def _studio(cls, albums: list[dict]) -> list[dict]:
        """Full-length releases, oldest first. Falls back to EPs, then anything."""
        def studio(a):
            return (a.get("album_type") == "album"
                    and not cls._NOT_STUDIO.search(a.get("version") or "")
                    and not cls._NOT_STUDIO_NAME.search(a["name"]))
        for keep in (studio, lambda a: a.get("album_type") in ("album", "ep", "unknown"), lambda a: True):
            sel = [a for a in albums if keep(a)]
            if sel:
                return sorted(sel, key=lambda a: a.get("year") or 0)
        return []

    def _essentials(self, artist: dict) -> dict | None:
        """Apple's "<Artist> Essentials" playlist, if it has one."""
        want = f"{artist['name']} essentials"
        for p in self._search(want, ["playlist"], 8).get("playlists") or []:
            if _sim(p["name"], want) >= 0.9:
                return p
        return None

    # ── playing ──────────────────────────────────────────────────────────

    def _play(self, player: str, media, *, radio=False, shuffle=False):
        pid = self._player_id(player)
        uris = [m["uri"] for m in media] if isinstance(media, list) else media["uri"]
        if self.default_volume is not None:
            try:
                q = self.ma.cmd("player_queues/get", queue_id=pid, timeout=3) or {}
                if q.get("state") != "playing":
                    self.ma.cmd("players/cmd/volume_set", player_id=pid, timeout=3,
                                volume_level=int(self.default_volume))
                    self._forget_duck(player)
            except MusicError as e:
                log.warning("Music: default volume on %s: %s", player, e)
        # MA answers play_media only once the stream is running: ~3.5s fetching
        # a playlist's tracks from Apple Music plus AirPlay startup. Don't hold
        # "Playing X" behind that; the music starts no later either way.
        args = dict(queue_id=pid, media=uris, option="replace", radio_mode=radio, shuffle=shuffle)
        if self.play_in_background:
            Thread(target=self._send_play, args=(args,), daemon=True, name="music-play").start()
        else:
            self.ma.cmd("player_queues/play_media", **args)
        self._active = player
        for m in media if isinstance(media, list) else [media]:
            self._learn(m.get("name"), *(a.get("name") for a in m.get("artists") or []))

    def _send_play(self, args: dict):
        try:
            self.ma.cmd("player_queues/play_media", timeout=30, **args)
        except MusicError as e:
            log.warning("Music: play_media failed: %s", e)

    def _play_artist(self, artist: dict, player: str, shuffle: bool) -> str:
        self._learn(artist["name"])
        ess = self._essentials(artist)
        if ess:
            self._play(player, ess, shuffle=shuffle)
            return f"Playing {ess['name']}{self._where(player)}."
        self._play(player, artist, radio=True, shuffle=shuffle)
        return f"Playing {artist['name']}{self._where(player)}."

    def _play_album(self, album: dict, player: str, shuffle: bool) -> str:
        self._play(player, album, shuffle=shuffle)
        by = _artists(album)
        return f"Playing {album['name']}{f' by {by}' if by else ''}{self._where(player)}."

    def _play_track(self, track: dict, player: str) -> str:
        self._play(player, track, radio=self.track_radio)
        by = _artists(track)
        return f"Playing {track['name']}{f' by {by}' if by else ''}{self._where(player)}."

    # ── resolution ───────────────────────────────────────────────────────

    def _best(self, items, name, artist="", min_name=0.85, min_artist=0.7):
        def score(i):
            s = _sim(i["name"], name)
            if artist:
                s += _sim(_artists(i), artist) * 0.5
            return s
        for i in sorted(items or [], key=score, reverse=True):
            if _sim(i["name"], name) >= min_name and (not artist or _sim(_artists(i), artist) >= min_artist
                                                      or _norm(artist) in _norm(_artists(i))):
                return i
        return None

    def _pending_albums(self) -> list:
        if self._pending and time.time() - self._pending.at < self.PENDING_TTL:
            return self._pending.albums
        return []

    def _track_by_lyrics(self, lyrics: str) -> dict | None:
        """Find a song from a remembered line via web search (SearXNG): lyric
        sites title their pages "Artist – Title Lyrics", which beats any
        catalogue search at matching a chorus."""
        if not self.lyrics_search:
            return None
        try:
            r = requests.get(f"{settings.SEARCH_URL}/search",
                             params={"q": f'"{lyrics}" lyrics', "format": "json"},
                             timeout=settings.SEARCH_TIMEOUT)
            results = r.json().get("results", [])[:8]
        except (requests.RequestException, ValueError) as e:
            log.warning("Lyrics search failed: %s", e)
            return None
        # Titles come as "Artist – Title Lyrics" (Genius, AZLyrics) or
        # "Title - Artist | …"; count each unordered pair, try both orders.
        votes: dict[tuple, int] = {}
        for res in results:
            m = re.match(r"^(.+?)\s+[–—-]\s+(.+?)\s+(?:Lyrics|lyrics)\b", res.get("title", ""))
            if not m:
                continue
            a, b = (re.sub(r"\s*\|.*$|\s*\((?:English\s+)?Translation\)|\s+\d{4}\s+Song$|[\"“”]", "",
                           x).strip() for x in m.groups())
            if not a or not b or re.search(r"\b(?:song\s+and|video\s+with|karaoke)\b", f"{a} {b}", re.I):
                continue
            votes[(a, b)] = votes.get((a, b), 0) + 1
        for (a, b), _ in sorted(votes.items(), key=lambda kv: -kv[1])[:4]:
            tracks = self._search(f"{a} {b}", ["track"], 8).get("tracks")
            t = self._best(tracks, b, a, min_name=0.75) or self._best(tracks, a, b, min_name=0.75)
            if t:
                log.info("Lyrics %r → %s by %s", lyrics, t["name"], _artists(t))
                return t
        return None

    # ── ducking (called from MQTT antigua/listening: the user started/stopped talking)

    def on_listening(self, active: bool):
        # Each call just records the wanted state; the worker applies the
        # latest one, so a short utterance whose "stopped" lands before the
        # duck has finished still ends up restored.
        self._listening = active
        Thread(target=self._apply_listening, daemon=True, name="music-duck").start()

    def _apply_listening(self):
        with self._duck_lock:
            if self._listening:
                self._duck()
            else:
                self._unduck()

    def _duck(self):
        with self._duck_lock:
            for name in self.duck_players:
                if name in self._ducked:
                    continue
                try:
                    pid = self._player_id(name)
                    q = self.ma.cmd("player_queues/get", queue_id=pid, timeout=3) or {}
                    if q.get("state") != "playing":
                        continue
                    level = (self.ma.cmd("players/get", player_id=pid, timeout=3) or {}).get("volume_level")
                    if not level:
                        continue
                    self.ma.cmd("players/cmd/volume_set", player_id=pid, timeout=3,
                                volume_level=max(3, int(level * self.duck_ratio)))
                    self._ducked[name] = level
                    log.info("Music: ducked %s %s → %s", name, level, int(level * self.duck_ratio))
                except MusicError as e:
                    log.warning("Music: duck %s failed: %s", name, e)
            if self._ducked:
                if self._duck_timer:
                    self._duck_timer.cancel()
                # Safety net in case the "stopped talking" message is lost.
                self._duck_timer = Timer(self.duck_timeout, self._unduck)
                self._duck_timer.daemon = True
                self._duck_timer.start()

    def _unduck(self, only: str | None = None):
        with self._duck_lock:
            for name in [only] if only else list(self._ducked):
                level = self._ducked.pop(name, None)
                if level is None:
                    continue
                try:
                    self.ma.cmd("players/cmd/volume_set", player_id=self._player_id(name),
                                volume_level=level, timeout=3)
                    log.info("Music: restored %s to %s", name, level)
                except MusicError as e:
                    log.warning("Music: unduck %s failed: %s", name, e)
            if not self._ducked and self._duck_timer:
                self._duck_timer.cancel()
                self._duck_timer = None

    def _forget_duck(self, name: str):
        """A volume the user just set shouldn't be undone by the restore."""
        with self._duck_lock:
            self._ducked.pop(name, None)

    # ── entry points ─────────────────────────────────────────────────────

    def handle(self, intent: MusicIntent, rehear=None) -> str:
        """Run a parsed request; return what Antigua should say ("" = nothing).
        rehear(hints) -> transcript re-runs STT with name hints (None = can't)."""
        with self._lock:
            self._rehear = rehear
            try:
                if intent.kind == "control":
                    return self._control(intent)
                if intent.kind == "info":
                    return self._info(intent)
                return self._play_intent(self._reheard(intent))
            except MusicError as e:
                log.warning("Music %s/%s failed: %s", intent.kind, intent.action, e)
                return "I couldn't reach the music server."
            finally:
                self._rehear = None

    def _play_intent(self, it: MusicIntent) -> str:
        player = self._player_name(it.speaker)
        a = it.action

        if a == "resume":
            q = self.ma.cmd("player_queues/get", queue_id=self._player_id(player)) or {}
            if q.get("current_item"):
                self.ma.cmd("player_queues/resume", queue_id=self._player_id(player))
                self._active = player
                return ""
            return "What would you like to hear?"

        if a == "pick":
            albums = self._pending_albums()
            if not albums:
                return "Which album? Ask me for an artist's albums first."
            if it.ordinal is None:
                if len(albums) > 1:
                    return f"Which one? {_spoken_list([x['name'] for x in albums])}."
                pick = albums[0]
            else:
                idx = it.ordinal - 1 if it.ordinal > 0 else len(albums) - 1
                if idx >= len(albums):
                    return f"I only mentioned {len(albums)}."
                pick = albums[idx]
            return self._play_album(pick, player, it.shuffle)

        if a in ("newest", "oldest"):
            artist = self._find_artist(it.artist)
            if not artist:
                return f"I couldn't find {it.artist} on Apple Music."
            studio = self._studio(self._albums(artist))
            if not studio:
                return f"I couldn't find any albums by {artist['name']}."
            album = studio[-1] if a == "newest" else studio[0]
            return self._play_album(album, player, it.shuffle)

        if a == "year":
            artist = self._find_artist(it.artist)
            if not artist:
                return f"I couldn't find {it.artist} on Apple Music."
            hits = [x for x in self._albums(artist)
                    if x.get("year") and it.year_from <= x["year"] <= it.year_to]
            span = str(it.year_from) if it.year_from == it.year_to else f"the {str(it.year_from)[2:]}s"
            if not hits:
                return f"I couldn't find anything {artist['name']} released in {span}."
            hits.sort(key=lambda x: x.get("year") or 0)
            self._play(player, hits, shuffle=it.shuffle)
            return f"Playing {artist['name']}'s music from {span}{self._where(player)}."

        if a == "lyrics":
            track = self._track_by_lyrics(it.query)
            if not track:
                return "I couldn't work out which song that is."
            return self._play_track(track, player)

        if a == "artist":
            artist = self._find_artist(it.artist)
            if not artist:
                return f"I couldn't find {it.artist} on Apple Music."
            return self._play_artist(artist, player, it.shuffle)

        if a == "album":
            q = f"{it.query} {it.artist}".strip()
            album = (self._best(self._pending_albums(), it.query, it.artist, 0.75)
                     or self._best(self._search(q, ["album"]).get("albums"), it.query, it.artist, 0.7))
            if not album:
                return f"I couldn't find the album {it.query}."
            return self._play_album(album, player, it.shuffle)

        if a == "track":
            res = self._search(f"{it.query} {it.artist}".strip(), ["track"])
            track = self._best(res.get("tracks"), it.query, it.artist, 0.75)
            if not track and it.artist:
                # "Stand by Me" parses as "Stand" by "Me": try the whole phrase as a title.
                track = self._best(self._search(it.raw, ["track"]).get("tracks"), it.raw, "", 0.85)
            if not track:
                return f"I couldn't find {it.raw}."
            return self._play_track(track, player)

        # "any": what was named? Previously-listed album → artist → song →
        # album → playlist (genres, moods) → lyrics → best song hit.
        q = it.query
        pend = self._best(self._pending_albums(), q, "", 0.75)
        if pend:
            return self._play_album(pend, player, it.shuffle)
        res = self._search(q, ["artist", "track", "album", "playlist"])
        artist = self._best(res.get("artists"), q, "", 0.9)
        # A top-3 song with that exact title beats an artist of the same name
        # unless the artist is in the library: "play Halo" is Beyoncé's song,
        # not an obscure artist called HALO.
        top_track = self._best((res.get("tracks") or [])[:3], q, "", 0.95)
        if artist and top_track and artist.get("provider") != "library":
            artist = None
        playlists = res.get("playlists") or []

        def play_playlist():
            p = max(playlists, key=lambda p: _sim(p["name"], q))
            self._play(player, p, shuffle=True)
            return f"Playing {p['name']}{self._where(player)}."

        if artist:
            return self._play_artist(artist, player, it.shuffle)
        if it.genre and playlists:
            return play_playlist()
        track = top_track or self._best(res.get("tracks"), q, "", 0.9)
        if track:
            return self._play_track(track, player)
        album = self._best(res.get("albums"), q, "", 0.9)
        if album:
            return self._play_album(album, player, it.shuffle)
        if playlists and len(q.split()) <= 3:
            return play_playlist()
        if len(q.split()) >= 4:
            track = self._track_by_lyrics(q)
            if track:
                return self._play_track(track, player)
        tracks = res.get("tracks") or []
        if tracks:
            return self._play_track(tracks[0], player)
        return f"I couldn't find {q} on Apple Music."

    def _info(self, it: MusicIntent) -> str:
        artist = self._find_artist(it.artist)
        if not artist:
            return f"I couldn't find {it.artist} on Apple Music."
        studio = self._studio(self._albums(artist))
        name = artist["name"]
        if not studio:
            return f"I couldn't find any albums by {name}."

        def fmt(x):
            return f"{x['name']}, from {x['year']}" if x.get("year") else x["name"]

        if it.action == "newest":
            pick = [studio[-1]]
            reply = f"{name}'s newest album is {fmt(pick[0])}."
        elif it.action == "oldest":
            pick = [studio[0]]
            reply = f"{name}'s first album is {fmt(pick[0])}."
        elif it.action == "older":
            pick = studio[:3]
            reply = f"{name}'s earliest albums are {_spoken_list([fmt(x) for x in pick])}."
        elif it.action == "newer":
            pick = list(reversed(studio[-3:]))
            reply = f"{name}'s newest albums are {_spoken_list([fmt(x) for x in pick])}."
        else:
            pick = list(reversed(studio[-4:]))
            count = f"{len(studio)} albums" if len(studio) != 1 else "one album"
            reply = f"{name} has {count}. The most recent are {_spoken_list([fmt(x) for x in pick])}."
        self._pending = _Pending(pick, name, time.time())
        return reply + (" Want me to play it?" if len(pick) == 1 else " Want me to play one?")

    def _control(self, it: MusicIntent) -> str:
        a = it.action
        if a == "transfer":
            if not self._active:
                return "Nothing is playing to move."
            target = self._player_name(it.speaker)
            if target == self._active:
                return "It's already playing there."
            self.ma.cmd("player_queues/transfer", source_queue_id=self._player_id(self._active),
                        target_queue_id=self._player_id(target), auto_play=True)
            self._active = target
            label = self.labels.get(target, "the kitchen speaker" if target == self.default_player
                                    else f"the {target}")
            return f"Moving the music to {label}."

        player = self._control_target(it.speaker)
        pid = self._player_id(player)
        q = self.ma.cmd("player_queues/get", queue_id=pid) or {}
        cur = q.get("current_item")

        if a == "now_playing":
            if not cur or q.get("state") not in ("playing", "paused"):
                return "Nothing's playing right now."
            mi = cur.get("media_item") or {}
            by = _artists(mi)
            album = (mi.get("album") or {}).get("name")
            reply = f"This is {mi.get('name') or cur.get('name')}"
            reply += f" by {by}" if by else ""
            reply += f", from {album}" if album and _norm(album) != _norm(mi.get("name", "")) else ""
            return reply + "."
        if a.startswith("volume"):
            if a == "volume_set":
                self._forget_duck(player)
                self.ma.cmd("players/cmd/volume_set", player_id=pid, volume_level=it.level)
                return f"Music volume {it.level}."
            self._unduck(player)      # step from the real level, not the ducked one
            self.ma.cmd(f"players/cmd/{a}", player_id=pid)
            return ""
        if not cur:
            return "Nothing's playing right now."
        if a == "pause":
            self.ma.cmd("player_queues/pause", queue_id=pid)
        elif a == "resume":
            self.ma.cmd("player_queues/resume", queue_id=pid)
        elif a == "next":
            self.ma.cmd("player_queues/next", queue_id=pid)
        elif a == "previous":
            self.ma.cmd("player_queues/previous", queue_id=pid)
        elif a == "restart_track":
            self.ma.cmd("player_queues/seek", queue_id=pid, position=0)
        elif a == "restart_queue":
            self.ma.cmd("player_queues/play_index", queue_id=pid, index=0)
        elif a.startswith("repeat_"):
            mode = {"repeat_one": "one", "repeat_all": "all", "repeat_off": "off"}[a]
            self.ma.cmd("player_queues/repeat", queue_id=pid, repeat_mode=mode)
            return {"one": "Repeating this song.", "all": "Repeating the whole queue.",
                    "off": "Repeat is off."}[mode]
        elif a in ("shuffle_on", "shuffle_off"):
            self.ma.cmd("player_queues/shuffle", queue_id=pid, shuffle_enabled=a == "shuffle_on")
            return "Shuffling." if a == "shuffle_on" else "Shuffle is off."
        self._active = player
        # Transport controls answer with silence, like Alexa: the music doing
        # the thing is the confirmation.
        return ""
