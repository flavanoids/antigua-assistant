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
import math
import random
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Lock, RLock, Thread, Timer

import requests

from . import settings
from .music_intents import Correction, MusicIntent, parse_music

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


def _key(s: str) -> str:
    """_norm without a leading "the": "The Killers" and "killers" are one name."""
    return re.sub(r"^the\s+", "", _norm(s))


def _sim(a: str, b: str) -> float:
    a, b = _key(a), _key(b)
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


def _match(heard: str, name: str) -> float:
    """How well a catalogue name fits what was heard: spelling or sound
    ("beyond say" ~ Beyoncé), whichever is closer."""
    text = _sim(heard, name)
    a, b = _sound(_key(heard)), _sound(_key(name))
    if not a or not b:
        return text
    return max(text, 0.95 * difflib.SequenceMatcher(None, a, b).ratio())


# Covers, karaoke, soundtrack re-recordings and the like: rarely what a bare
# title means. Checked on the raw name/version/artists, before _norm drops
# the bracketed part that gives them away.
_NOT_ORIGINAL = re.compile(
    r"\b(?:karaoke|tribute|cover(?:ed)?|in\s+the\s+style\s+of|made\s+famous|originally\s+performed|"
    r"instrumental|lullab(?:y|ies)|music\s+box|8[\s-]?bit|piano\s+version|acoustic\s+version|"
    r"sped\s+up|slowed|nightcore|string\s+quartet|rockabye|cast|from\s+[\"“].+[\"”]|"
    r"from\s+the\s+\w+(?:\s+\w+)?\s+(?:film|movie|series|soundtrack)|:\s*sing$)\b|:\s*sing$",
    re.IGNORECASE)
_LIVE_REMIX = re.compile(r"\b(?:live|remix(?:es)?|demo)\b", re.IGNORECASE)


@dataclass
class _Cand:
    item: dict
    kind: str        # artist | album | track | playlist
    score: float = 0.0
    pos: int = 99    # best position in any Apple result list


# Apple's public top-100 feeds: popular names that aren't in the library yet.
_CHARTS = [f"https://rss.marketingtools.apple.com/api/v2/us/music/most-played/100/{k}.json"
           for k in ("songs", "albums")]


# Deezer's public search (no key) as a spelling corrector for names the user
# asks for that aren't in the library: its fuzzy search turns "chapel room"
# into Chappell Roan and reports popularity, which MA's search doesn't.
# Names only; playback stays on Apple Music.
_DEEZER = "https://api.deezer.com/search/{}"
# Apple's public iTunes search: its song order tracks US popularity far better
# than MA's catalogue search ("flowers" → Miley Cyrus first). No key; about
# 20 calls a minute allowed, so one per request at most.
_ITUNES = "https://itunes.apple.com/search"
_POPULAR_FANS = 50_000      # artist nb_fan
_POPULAR_RANK = 600_000     # track rank (Espresso ~980k; filler ~40k)


def _artists(item: dict) -> str:
    names = [a.get("name", "") for a in item.get("artists") or [] if a.get("name")]
    if not names:
        return ""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _artist_names(item: dict) -> list[str]:
    return [a.get("name", "") for a in item.get("artists") or [] if a.get("name")]


def _credits(item: dict) -> list[str]:
    """Each credited artist, with collaborations split: Apple sometimes credits
    one artist called "Eslabon Armado & Peso Pluma"."""
    out = []
    for n in _artist_names(item):
        out += [p for p in re.split(r"\s*(?:&|,|\band\b|\bfeat\.?|\bft\.?|\bx\b)\s*", n) if p.strip()]
    return out


def _spoken_time(secs: float, coarse: bool = False) -> str:
    """ "3 minutes 27 seconds", "half an hour", "1 hour 5 minutes"."""
    s = int(round(secs))
    if coarse and s >= 90:
        s = int(round(s / 60)) * 60
    if s == 1800:
        return "half an hour"
    parts = []
    for unit, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        n, s = divmod(s, size)
        if n:
            parts.append(f"{n} {unit}{'s' if n != 1 else ''}")
    return " ".join(parts) or "0 seconds"


def _spoken_left(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)} seconds"
    m = round(secs / 60)
    return f"about {m} minute{'s' if m != 1 else ''}"


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
        self.store_country = m.get("store_country", "US")    # iTunes popularity order
        # Volume a player starts at when music begins from idle (None = leave it)
        self.default_volume = m.get("default_volume")
        self._ids: dict[str, str] = {}        # MA player name → player_id
        self._active: str | None = None       # player name Antigua last played on
        self._pending: _Pending | None = None
        self._last: list[_Cand] = []          # the ranked pool behind the last play
        self._last_it: MusicIntent | None = None
        self._last_played: _Cand | None = None
        self._last_at = 0.0
        self._rejected: set[str] = set()      # uris corrected away from, this request
        # Corrections are remembered per request: "play hello" → Adele once
        # corrected. {request key: {"want": uri, "not": [uris]}}
        self._prefs_path = settings.DATA_DIR / "music_prefs.json"
        try:
            self._prefs: dict[str, dict] = json.loads(self._prefs_path.read_text())
        except (OSError, ValueError):
            self._prefs = {}
        self._fans_path = settings.DATA_DIR / "music_artist_pop.json"
        try:
            self._fans: dict[str, tuple] = json.loads(self._fans_path.read_text())
        except (OSError, ValueError):
            self._fans = {}
        # Names Whisper might have misheard (library artists/albums + charts),
        # offered back to it as hints when a request matches nothing well.
        self._vocab: list[str] = []
        self._vocab_at = 0.0
        self._vocab_loading = False
        self._rehear = None                   # set per request by handle()
        self._option = "replace"              # play_media queue option for this request
        self._everywhere_now = False          # this request plays everywhere
        # "Everywhere": every configured speaker unless music.everywhere lists some
        self.everywhere = m.get("everywhere") or list(dict.fromkeys(settings.MUSIC_SPEAKERS.values()))
        self._sleep: Timer | None = None      # "stop the music in 30 minutes"
        self._sleep_at = 0.0
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
        if speaker_key and speaker_key != "*":
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
        if self._everywhere_now:
            return " everywhere"
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

    # ── resolution: one scored pool of artists, songs, albums, playlists ─

    def _itunes(self, term: str) -> list:
        """iTunes song results for term, most popular first; [] on failure."""
        try:
            r = requests.get(_ITUNES, params={"term": term, "entity": "song", "limit": 15,
                                              "country": self.store_country}, timeout=2.5)
            return r.json().get("results") or []
        except Exception as e:
            log.warning("Music: iTunes search failed: %s", e)
            return []

    def _dz(self, kind: str, q: str) -> list:
        """Deezer search results (names and popularity only; playback stays on
        Apple Music). [] when unreachable: scoring then leans on Apple's order."""
        try:
            r = requests.get(_DEEZER.format(kind), params={"q": q, "limit": 15}, timeout=2.5)
            return r.json().get("data") or []
        except Exception as e:
            log.warning("Music: Deezer %s search failed: %s", kind, e)
            return []

    @staticmethod
    def _hypotheses(it: MusicIntent) -> list[tuple[str, str]]:
        """(title, artist) readings of the request. Every " by " is a possible
        split, and the whole phrase may be a title: "stand by me by ben e king"
        → (stand, me by ben e king), (stand by me, ben e king), (…whole…, "")."""
        if it.action == "artist":
            return [("", it.artist)]
        if not it.artist:
            return [(it.query, "")]
        whole = f"{it.query} by {it.artist}"
        parts = re.split(r"\s+by\s+", whole, flags=re.IGNORECASE)
        hyps = [(" by ".join(parts[:i]), " by ".join(parts[i:])) for i in range(1, len(parts))]
        return hyps + [(whole, "")]

    def _gather(self, it: MusicIntent, hyps) -> tuple[list[_Cand], list]:
        """Apple candidates for every reading, plus iTunes' popularity order
        for the main title, all searched in parallel."""
        kinds = {"artist": ["artist"], "album": ["album"], "track": ["track", "album"]}.get(
            it.action, ["artist", "track", "album", "playlist"])
        searches = []
        for title, artist in hyps:
            if title and artist:
                searches += [(f"{title} {artist}", ["track", "album"]), (title, ["track", "album"])]
            elif title:
                searches.append((title, kinds))
            else:
                searches.append((artist, ["artist"]))
        searches = list(dict.fromkeys((q, tuple(t)) for q, t in searches))
        titles = [] if it.genre or it.action in ("artist", "album") else \
            list(dict.fromkeys(t for t, _ in hyps if t))[:1]
        with ThreadPoolExecutor(len(searches) + len(titles)) as ex:
            dz = [ex.submit(self._itunes, t) for t in titles]
            futures = [ex.submit(self._search, q, list(t), 10) for q, t in searches]
            results = []
            for f in futures:
                try:
                    results.append(f.result())
                except MusicError as e:
                    log.warning("Music: search failed: %s", e)
        if not results:
            raise MusicError("every search failed")
        cands: dict[str, _Cand] = {}
        for res in results:
            for plural, items in res.items():
                if not isinstance(items, list):
                    continue
                for i, item in enumerate(items):
                    if not item.get("uri") or not item.get("name"):
                        continue
                    c = cands.setdefault(item["uri"], _Cand(item, item.get("media_type") or plural[:-1]))
                    c.pos = min(c.pos, i)
        return list(cands.values()), [row for f in dz for row in f.result()]

    @staticmethod
    def _hit_rank(c: _Cand, charts: list) -> float:
        """0..1 from where iTunes puts this very song for the title: first is
        1, then it falls off fast. 0 when it isn't listed, so it only helps."""
        credited = {_norm(a) for a in _credits(c.item)}
        best = 0.0
        for i, r in enumerate(charts):
            names = {_norm(a) for a in _credits({"artists": [{"name": r.get("artistName", "")}]})}
            if credited & names and _sim(r.get("trackName", ""), c.item["name"]) >= 0.9:
                best = max(best, 0.6 ** i)
        return best

    FANS_TTL = 30 * 86400

    def _artist_pop(self, names: list[str]) -> dict[str, float]:
        """0..1 popularity per artist name from Deezer fan counts (1k → 0,
        10M → 1), cached on disk: the same artists come up again and again.
        Exact names only, so the band "Killers" doesn't borrow The Killers' fans."""
        now, out, todo = time.time(), {}, []
        for n in dict.fromkeys(names):
            hit = self._fans.get(_norm(n))
            if hit and now - hit[1] < self.FANS_TTL:
                out[n] = hit[0]
            elif _norm(n):
                todo.append(n)

        def lookup(n):
            rows = self._dz("artist", n)
            fans = max((r.get("nb_fan") or 0 for r in rows if _norm(r.get("name", "")) == _norm(n)), default=0)
            return n, min(1.0, max(0.0, (math.log10(fans + 1) - 3) / 4)), bool(rows)
        if todo:
            with ThreadPoolExecutor(len(todo)) as ex:
                for n, pop, ok in ex.map(lookup, todo):
                    out[n] = pop
                    if ok:                  # Deezer down: don't remember a 0
                        self._fans[_norm(n)] = (pop, now)
            try:
                self._fans_path.write_text(json.dumps(self._fans))
            except OSError as e:
                log.warning("Music: saving artist popularity failed: %s", e)
        return out

    def _score(self, c: _Cand, it: MusicIntent, hyps, asked: str) -> float:
        """How well c fits the request, before artist popularity (added by _resolve)."""
        name = c.item["name"]
        if c.kind == "playlist":
            words = set(_key(re.sub(r"\b(?:music|songs|some)\b", " ", it.query)).split())
            have = set(_key(name).split())
            overlap = len(words & have) / len(words) if words else 0.0
            if it.genre:
                s = max(_match(it.query, name), overlap, 0.5) + 0.35
            else:                           # "play Halo" isn't "HALO Essentials"
                s = _match(it.query, name)
                if s < 0.9:
                    return 0.0
                s -= 0.15
            s += 0.2 if (c.item.get("owner") or "").startswith("Apple Music") else 0.0
        else:
            s = 0.0
            for title, artist in hyps:
                if c.kind == "artist":
                    if title and artist:
                        continue            # "X by Y" names a song or album
                    fit = _match(title or artist, name)
                    if fit < 0.75:
                        continue
                    h = fit
                else:
                    if not title:
                        continue
                    fit = _match(title, name)
                    # A shortened title ("Key of Life"), word for word inside
                    # the real one: with the right artist, or as Apple's top hit.
                    if (fit < 0.75 and len(_key(title).split()) >= 2
                            and f" {_key(title)} " in f" {_key(name)} " and (artist or c.pos == 0)):
                        fit = 0.8 if artist else 0.76
                    if fit < 0.75:
                        continue
                    h = fit
                    if artist:
                        by = max((_match(artist, a) for a in _artist_names(c.item)), default=0.0)
                        # "Fleetwood" for Fleetwood Mac; whole words only ("me" ≠ Meg)
                        want = set(_key(artist).split())
                        if want and any(want <= set(_key(a).split()) for a in _artist_names(c.item)):
                            by = max(by, 0.9)
                        h += 0.5 * by - 0.25
                said = _norm(title or artist)
                if said.startswith("the ") and said == _norm(name):
                    h += 0.1                # "the killers": the band, not Iron Maiden's "Killers"
                s = max(s, h)
            if not s:
                return 0.0
            if it.genre:
                s -= 0.25
        # Apple's own ranking within the type. For songs and albums it's the
        # best sign of which version people mean (Dolly's "Jolene" over
        # Beyoncé's), so it falls off steeply there.
        s += 0.2 * max(0, 1 - c.pos / 10) if c.kind in ("artist", "playlist") else 0.3 * 0.65 ** c.pos
        if c.item.get("provider") == "library" or name in self._played:
            s += 0.1
        if c.kind == "album" and (c.item.get("album_type") in ("single", "ep") or self._NOT_STUDIO_NAME.search(name)):
            # "Espresso - Single": the song is the thing; and even for "the
            # album X", the full album over an EP of the same name
            s -= 0.3 if it.action != "album" else 0.2
        blob = " ".join([name, c.item.get("version") or "", *_artist_names(c.item)])
        if _NOT_ORIGINAL.search(blob) and not _NOT_ORIGINAL.search(asked):
            s -= 0.4
        elif c.kind == "track" and _LIVE_REMIX.search(blob) and not _LIVE_REMIX.search(asked):
            s -= 0.15
        s += {"track": {"track": 0.25, "album": -0.1, "artist": -0.3, "playlist": -0.3},
              "album": {"album": 0.3, "track": -0.2, "artist": -0.5, "playlist": -0.3},
              "artist": {"artist": 0.3, "album": -0.5, "track": -0.5, "playlist": -0.5},
              # a bare title means the song more often than the album
              "any": {"track": 0.1, "artist": 0.1},
              }.get(it.action, {}).get(c.kind, 0.0)
        return s

    def _resolve(self, it: MusicIntent) -> list[_Cand]:
        """Everything that could be meant, best first (score > 0)."""
        hyps = self._hypotheses(it)
        cands, charts = self._gather(it, hyps)
        asked = f"{it.raw} {it.query} {it.artist}"
        for c in cands:
            c.score = self._score(c, it, hyps, asked)
        # Popularity of whoever made it, for the contenders only: Queen over a
        # song called QUEEN, Beyoncé's "Halo" over Tiffany Day's album HALO.
        # Weighs most for artists; for songs Apple's order leads (see _score).
        top = sorted((c for c in cands if c.score > 0 and c.kind != "playlist"), key=lambda c: -c.score)[:10]
        who = {id(c): (c.item["name"] if c.kind == "artist" else (_credits(c.item) or [""])[0]) for c in top}
        pops = self._artist_pop([w for w in who.values() if w])
        for c in top:
            c.score += (0.4 if c.kind == "artist" else 0.2) * pops.get(who[id(c)], 0.0)
            if c.kind == "track":
                c.score += 0.25 * self._hit_rank(c, charts)
        pref = self._prefs.get(self._pref_key(it)) or {}
        disliked = self._disliked()
        for c in cands:
            if c.item["uri"] in disliked:
                c.score -= 0.5
            if c.score > 0 and c.item["uri"] == pref.get("want"):
                c.score += 0.6
            elif c.item["uri"] in pref.get("not", ()):
                c.score -= 0.6
        ranked = sorted((c for c in cands if c.score > 0), key=lambda c: -c.score)
        for c in ranked[:5]:
            log.info("Music: %.2f %s %s — %s", c.score, c.kind, c.item["name"], _artists(c.item))
        return ranked

    def _play_cand(self, c: _Cand, player: str, it: MusicIntent) -> str:
        self._last_played, self._last_at = c, time.time()
        if c.kind == "artist":
            return self._play_artist(c.item, player, it.shuffle)
        if c.kind == "album":
            return self._play_album(c.item, player, it.shuffle)
        if c.kind == "playlist":
            self._play(player, c.item, shuffle=True)
            return f"Playing {c.item['name']}{self._where(player)}."
        return self._play_track(c.item, player)

    # ── playing ──────────────────────────────────────────────────────────

    def _play(self, player: str, media, *, radio=False, shuffle=False):
        pid = self._player_id(player)
        uris = [m["uri"] for m in media] if isinstance(media, list) else media["uri"]
        if self.default_volume is not None and self._option == "replace":
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
        option = self._option
        if option != "replace":              # queued behind what's playing: no radio tail, no reshuffle
            radio = shuffle = False
        args = dict(queue_id=pid, media=uris, option=option, radio_mode=radio, shuffle=shuffle)
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
                self._everywhere_now = False

    def play_url(self, url: str, speaker_key: str | None = None) -> str:
        """Play a bare audio URL (a podcast episode); return " on the X" for
        the reply. Not _learn()ed: an episode title isn't a name to re-hear."""
        with self._lock:
            player = self._player_name(speaker_key)
            self._play(player, {"uri": url})
            return self._where(player)

    # ── corrections ("no, the Adele one") ─────────────────────────────────

    CORRECT_TTL = 180          # seconds after a play that "the other one" means it

    def correctable(self) -> bool:
        """Did Antigua just start something by name that a correction could mean?"""
        return self._last_played is not None and time.time() - self._last_at < self.CORRECT_TTL

    @staticmethod
    def _pref_key(it: MusicIntent) -> str:
        return f"{it.action}|{_key(it.query)}|{_key(it.artist)}"

    def _remember(self, it: MusicIntent, c: _Cand, good: bool):
        p = self._prefs.setdefault(self._pref_key(it), {"want": None, "not": []})
        uri = c.item["uri"]
        if good:
            p["want"] = uri
            p["not"] = [u for u in p["not"] if u != uri]
        else:
            if p.get("want") == uri:
                p["want"] = None
            if uri not in p["not"]:
                p["not"] = (p["not"] + [uri])[-10:]
        try:
            self._prefs_path.write_text(json.dumps(self._prefs))
        except OSError as e:
            log.warning("Music: saving preferences failed: %s", e)

    def _disliked(self) -> set:
        return set(self._prefs.get("_disliked", {}).get("not", []))

    def _dislike(self, uri: str):
        d = self._prefs.setdefault("_disliked", {"want": None, "not": []})
        if uri not in d["not"]:
            d["not"] = (d["not"] + [uri])[-500:]
        try:
            self._prefs_path.write_text(json.dumps(self._prefs))
        except OSError as e:
            log.warning("Music: saving preferences failed: %s", e)

    def correct(self, cor: Correction) -> str:
        """Replace what was just played with the version the user meant."""
        with self._lock:
            try:
                return self._correct(cor)
            except MusicError as e:
                log.warning("Music correction %s failed: %s", cor.kind, e)
                return "I couldn't reach the music server."

    def _by(self, c: _Cand, artist: str) -> float:
        names = [c.item["name"]] if c.kind == "artist" else _credits(c.item) + _artist_names(c.item)
        want = set(_key(artist).split())
        if want and any(want <= set(_key(n).split()) for n in names):
            return 1.0
        return max((_match(artist, n) for n in names), default=0.0)

    def _correct(self, cor: Correction) -> str:
        prev, it = self._last_played, self._last_it
        player = self._active or self._player_name(it.speaker)
        self._rejected.add(prev.item["uri"])
        self._remember(it, prev, good=False)
        pool = [c for c in self._last if c.item["uri"] not in self._rejected]
        title = _key(prev.item["name"])

        def same(c):              # another version of what was played
            return c.kind == prev.kind and (prev.kind in ("artist", "playlist") or _key(c.item["name"]) == title)

        def plain(c):             # not a cover, karaoke, live cut or remix
            blob = " ".join([c.item["name"], c.item.get("version") or "", *_artist_names(c.item)])
            return not _NOT_ORIGINAL.search(blob) and not _LIVE_REMIX.search(blob)

        pick = None
        if cor.kind == "other":
            pick = next((c for c in pool if same(c)), None) or next(iter(pool), None)
            if not pick:
                return "That's the only match I found."
        elif cor.kind == "original":
            pick = next((c for c in pool if same(c) and plain(c)), None)
            if not pick:
                return "I couldn't find another version."
        elif cor.kind == "type":
            pick = next((c for c in pool if c.kind == cor.type), None)
            if not pick and cor.type != "playlist":
                ranked = self._resolve(replace(it, action=cor.type,
                                               artist="" if cor.type == "artist" else it.artist))
                pick = next((c for c in ranked if c.kind == cor.type and c.item["uri"] not in self._rejected), None)
            if not pick:
                return {"track": "I couldn't find a song for that.", "album": "I couldn't find an album for that.",
                        "artist": "I couldn't find the artist.", "playlist": "I couldn't find a playlist for that."}[cor.type]
        else:                     # "the Adele one"
            hits = sorted((c for c in pool if self._by(c, cor.artist) >= 0.8),
                          key=lambda c: (not same(c), -c.score))
            pick = hits[0] if hits else None
            if not pick:
                what = prev.item["name"] if prev.kind in ("track", "album") else it.query
                ranked = self._resolve(replace(it, action="any", query=what, artist=cor.artist, raw=""))
                pick = next((c for c in ranked if self._by(c, cor.artist) >= 0.8
                             and c.item["uri"] not in self._rejected), None)
            if not pick:
                return f"I couldn't find one by {cor.artist}."
        log.info("Music: corrected %s → %s — %s", prev.item["name"], pick.item["name"], _artists(pick.item))
        if cor.kind != "other":   # "not that one" says what's wrong, not what's right
            self._remember(it, pick, good=True)
        return self._play_cand(pick, player, it)

    def _play_intent(self, it: MusicIntent) -> str:
        """A play request; "play X next" / "add X to the queue" queue it behind
        what's playing instead (and simply play it when nothing is)."""
        if not it.enqueue:
            return self._play_request(it)
        player = self._player_name(it.speaker) if it.speaker else self._control_target(None)
        q = self.ma.cmd("player_queues/get", queue_id=self._player_id(player)) or {}
        if q.get("state") not in ("playing", "paused") or not q.get("current_item"):
            return self._play_request(replace(it, enqueue="", speaker=it.speaker))
        self._option = "next" if it.enqueue == "next" else "add"
        try:
            reply = self._play_request(replace(it, speaker=it.speaker or self._speaker_of(player)))
        finally:
            self._option = "replace"
        where = self._where(player)
        if not (reply.startswith("Playing ") and reply.endswith(f"{where}.")):
            return reply                      # "I couldn't find …"
        what = reply[len("Playing "):len(reply) - len(where) - 1]
        return f"{what} is up next." if it.enqueue == "next" else f"Added {what} to the queue."

    def _speaker_of(self, player: str) -> str | None:
        return next((k for k, v in settings.MUSIC_SPEAKERS.items() if v == player), None)

    def _play_request(self, it: MusicIntent) -> str:
        player = self._player_name(it.speaker)
        a = it.action
        if it.speaker == "*":                 # "play Adele everywhere": group first, then play on the leader
            self._group(player, self.everywhere)
            self._everywhere_now = True

        if a == "resume":
            q = self.ma.cmd("player_queues/get", queue_id=self._player_id(player)) or {}
            if q.get("current_item"):
                self.ma.cmd("player_queues/resume", queue_id=self._player_id(player))
                self._active = player
                return ""
            return "What would you like to hear?"

        if a in ("favorites", "library"):
            tracks = self.ma.cmd("music/tracks/library_items", favorite=a == "favorites" or None,
                                 limit=300, order_by="random") or []
            tracks = [t for t in tracks if t.get("uri") not in self._disliked()]
            if not tracks:
                return "You don't have any favorites yet." if a == "favorites" else "Your library is empty."
            self._play(player, tracks, shuffle=True)
            what = "your favorites" if a == "favorites" else "songs from your library"
            return f"Playing {what}{self._where(player)}."

        if a == "my_playlist":
            mine = self.ma.cmd("music/playlists/library_items", search=it.query, limit=20) or []
            pl = max(mine, key=lambda p: _match(it.query, p["name"]), default=None)
            if not pl or _match(it.query, pl["name"]) < 0.75:
                found = self._search(it.query, ["playlist"], 8).get("playlists") or []
                pl = max(found, key=lambda p: _match(it.query, p["name"]), default=None)
                if not pl or _match(it.query, pl["name"]) < 0.85:
                    return f"I couldn't find a playlist called {it.query}."
            self._play(player, pl)
            return f"Playing {pl['name']}{self._where(player)}."

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

        # artist / album / track / any: one scored pool. A name from the
        # album list Antigua just read out wins outright.
        if a in ("album", "any"):
            pend = self._best(self._pending_albums(), it.query, it.artist, 0.75)
            if pend:
                return self._play_album(pend, player, it.shuffle)
        ranked = self._resolve(it)
        self._last, self._last_it, self._rejected = ranked, it, set()
        self._last_played = None
        what = it.artist if a == "artist" else it.raw or it.query
        if not ranked:
            if len(what.split()) >= 4:
                track = self._track_by_lyrics(what)
                if track:
                    return self._play_track(track, player)
            if a == "album":
                return f"I couldn't find the album {it.query}."
            return f"I couldn't find {what} on Apple Music."
        return self._play_cand(ranked[0], player, it)

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

    # ── navigation within what's playing ─────────────────────────────────

    _NAVIGATION = ("seek", "seek_to", "skip_songs", "play_track_number", "up_next", "time_left",
                   "more_like_this", "more_by_artist", "whole_album", "rest_of_album",
                   "sleep", "sleep_after_song", "sleep_after_queue", "like", "dislike")

    def _navigate(self, it: MusicIntent, player: str, pid: str, q: dict, cur: dict) -> str:
        a = it.action
        mi = cur.get("media_item") or {}
        idx, total = q.get("current_index") or 0, q.get("items") or 0
        duration = cur.get("duration") or mi.get("duration") or 0
        elapsed = q.get("elapsed_time") or 0
        self._active = player

        if a == "seek":
            self.ma.cmd("player_queues/skip", queue_id=pid, seconds=int(it.seconds))
            return ""
        if a == "seek_to":
            if duration and it.seconds >= duration:
                return f"This song is only {_spoken_time(duration)} long."
            self.ma.cmd("player_queues/seek", queue_id=pid, position=int(it.seconds))
            return ""
        if a == "skip_songs":
            target = max(0, idx + it.count)
            if total and target >= total:
                return "That's past the end of the queue."
            self.ma.cmd("player_queues/play_index", queue_id=pid, index=target)
            return ""
        if a == "play_track_number":
            if total and it.count > total:
                return f"There are only {total} songs in the queue." if total > 1 else "There's only one song in the queue."
            self.ma.cmd("player_queues/play_index", queue_id=pid, index=it.count - 1)
            return ""
        if a == "up_next":
            nxt = q.get("next_item") or {}
            if not nxt:
                return "Nothing's queued after this song."
            nmi = nxt.get("media_item") or {}
            by = _artists(nmi)
            return f"Next is {nmi.get('name') or nxt.get('name')}{f' by {by}' if by else ''}."
        if a == "time_left":
            if not duration:
                return "I can't tell how long this one is."
            left = max(0, duration - elapsed)
            return f"{mi.get('name') or 'This song'} is {_spoken_time(duration)} long, with {_spoken_left(left)} left."
        if a in ("sleep", "sleep_after_song", "sleep_after_queue"):
            return self._set_sleep(it, pid, q, duration, elapsed)
        if a == "like":
            try:
                self.ma.cmd("music/favorites/add_item", item=mi.get("uri") or cur.get("uri"))
            except MusicError as e:
                log.warning("Music: favorite failed: %s", e)
                return "I couldn't save that one."
            return "Added to your favorites."
        if a == "dislike":
            if mi.get("uri"):
                self._dislike(mi["uri"])
            self.ma.cmd("player_queues/next", queue_id=pid)
            return "Okay, I won't pick that one again."

        # The rest queue something behind the current song, which keeps
        # playing; finding it takes seconds (similar artists' top songs, an
        # album's track list), so answer now and queue it in the background.
        artist = (mi.get("artists") or [{}])[0]
        name = artist.get("name", "")
        if a in ("more_like_this", "more_by_artist") and not name:
            return "I couldn't tell who this is."
        album_name = (mi.get("album") or {}).get("name")
        if a in ("whole_album", "rest_of_album") and not album_name:
            return "I can't tell which album this is from."
        job = {"more_like_this": lambda: self._queue_similar(pid, name, mi),
               "more_by_artist": lambda: self._queue_artist(pid, name, mi),
               "whole_album": lambda: self._queue_album(pid, player, album_name, name, mi, whole=True),
               "rest_of_album": lambda: self._queue_album(pid, player, album_name, name, mi, whole=False)}[a]
        if self.play_in_background:
            Thread(target=self._quietly, args=(job, a), daemon=True, name=f"music-{a}").start()
        else:
            self._quietly(job, a)
        return {"more_like_this": "More like this after this song.",
                "more_by_artist": f"More {name} after this song.",
                "whole_album": f"Playing {album_name}{self._where(player)}.",
                "rest_of_album": f"The rest of {album_name} is up next."}[a]

    @staticmethod
    def _quietly(job, what: str):
        try:
            job()
        except Exception as e:           # the reply's already out; just log
            log.warning("Music: %s failed: %s", what, e)

    def _queue_similar(self, pid: str, artist_name: str, current: dict):
        tracks = self._similar_tracks({"name": artist_name}, current)
        if tracks:
            self._enqueue_next(pid, tracks)
        log.info("Music: more like %s — %d songs queued", current.get("name"), len(tracks))

    def _queue_artist(self, pid: str, artist_name: str, current: dict):
        full = self._find_artist(artist_name)
        if not full:
            log.warning("Music: more by %r — artist not found", artist_name)
            return
        ess = self._essentials(full)
        media = [ess] if ess else [t for t in self._top_tracks(full)
                                   if _key(t["name"]) != _key(current.get("name", ""))]
        if media:
            self._enqueue_next(pid, media)

    def _queue_album(self, pid: str, player: str, album_name: str, artist_name: str, current: dict, whole: bool):
        ranked = self._resolve(MusicIntent("play", "album", query=album_name, artist=artist_name))
        album = next((c.item for c in ranked if c.kind == "album"), None)
        if not album:
            log.warning("Music: album %r by %r not found", album_name, artist_name)
            return
        if whole:
            self._play(player, album)
            return
        item_id, provider = self._catalog_ref(album)
        tracks = self.ma.cmd("music/albums/album_tracks", item_id=item_id,
                             provider_instance_id_or_domain=provider) or []
        here = next((i for i, t in enumerate(tracks) if _key(t["name"]) == _key(current.get("name", ""))), None)
        rest = [album] if here is None else tracks[here + 1:]
        if rest:
            self._enqueue_next(pid, rest)

    def _enqueue_next(self, pid: str, media: list):
        """Replace everything after the current song with media."""
        self.ma.cmd("player_queues/play_media", queue_id=pid, media=[m["uri"] for m in media],
                    option="replace_next", timeout=30)

    def _top_tracks(self, artist: dict) -> list:
        item_id, provider = self._catalog_ref(artist)
        try:
            return self.ma.cmd("music/artists/top_tracks", item_id=item_id,
                               provider_instance_id_or_domain=provider) or []
        except MusicError as e:
            log.warning("Music: top tracks for %s failed: %s", artist.get("name"), e)
            return []

    def _similar_tracks(self, artist_ref: dict, current: dict) -> list:
        """Top songs by artists like this one (Apple's similar artists), mixed
        with a couple more by this artist. Apple's own similar-tracks list is
        too thin to use (two songs, one of them the seed)."""
        artist = self._find_artist(artist_ref.get("name", "")) if artist_ref.get("name") else None
        if not artist:
            return []
        item_id, provider = self._catalog_ref(artist)
        similar = self.ma.cmd("music/artists/similar_artists", item_id=item_id,
                              provider_instance_id_or_domain=provider, limit=6) or []
        with ThreadPoolExecutor(len(similar) + 1) as ex:
            lists = list(ex.map(self._top_tracks, [artist, *similar[:6]]))
        skip = self._disliked() | {current.get("uri")}
        own = [t for t in lists[0] if _key(t["name"]) != _key(current.get("name", ""))][:2]
        picks = own + [t for tops in lists[1:] for t in tops[:3]]
        picks = [t for t in picks if t.get("uri") not in skip]
        random.shuffle(picks)
        return picks

    def _set_sleep(self, it: MusicIntent, pid: str, q: dict, duration: float, elapsed: float) -> str:
        if it.action == "sleep":
            secs, reply = it.seconds, f"Okay, I'll stop the music in {_spoken_time(it.seconds)}."
        elif it.action == "sleep_after_song":
            if not duration:
                return "I can't tell how long this song is."
            secs, reply = duration - elapsed + 1, "Okay, I'll stop after this song."
        else:
            if q.get("radio_source") or q.get("dont_stop_the_music_enabled"):
                return "This keeps going on its own, so give me a time instead, like in 30 minutes."
            items = self.ma.cmd("player_queues/items", queue_id=pid, limit=500,
                                offset=(q.get("current_index") or 0) + 1) or []
            secs = duration - elapsed + sum(i.get("duration") or 0 for i in items) + 1
            reply = f"Okay, I'll stop in about {_spoken_time(secs, coarse=True)}, when the queue ends."
        self._cancel_sleep()
        self._sleep = Timer(max(1.0, secs), self._sleep_fire, args=(pid,))
        self._sleep.daemon = True
        self._sleep.start()
        self._sleep_at = time.time() + secs
        log.info("Music: sleep timer %.0fs on %s", secs, pid)
        return reply

    def _cancel_sleep(self):
        if self._sleep:
            self._sleep.cancel()
        self._sleep = None

    def _sleep_fire(self, pid: str):
        self._sleep = None
        try:
            self.ma.cmd("player_queues/pause", queue_id=pid)
            log.info("Music: sleep timer paused %s", pid)
        except MusicError as e:
            log.warning("Music: sleep timer pause failed: %s", e)

    # ── multi-room ───────────────────────────────────────────────────────

    def _label(self, name: str) -> str:
        return self.labels.get(name, "the kitchen speaker" if name == self.default_player else f"the {name}")

    def _group(self, leader: str, members: list[str]) -> list[str]:
        """Sync members to leader (those Music Assistant says can); returns their names."""
        lpid = self._player_id(leader)
        can = set((self.ma.cmd("players/get", player_id=lpid) or {}).get("can_group_with") or [])
        names, pids = [], []
        for n in dict.fromkeys(members):
            if n == leader:
                continue
            try:
                pid = self._player_id(n)
            except MusicError as e:
                log.warning("Music: can't group %s: %s", n, e)
                continue
            if pid in can:
                names.append(n)
                pids.append(pid)
        if pids:
            self.ma.cmd("players/cmd/group_many", target_player=lpid, child_player_ids=pids)
        log.info("Music: grouped %s under %s", names, leader)
        return names

    def _members(self, leader: str) -> list[str]:
        """Player ids synced to leader (not including it)."""
        lpid = self._player_id(leader)
        info = self.ma.cmd("players/get", player_id=lpid) or {}
        return [p for p in info.get("group_members") or [] if p != lpid]

    def _multiroom(self, it: MusicIntent) -> str:
        leader = self._control_target(None)
        q = self.ma.cmd("player_queues/get", queue_id=self._player_id(leader)) or {}
        if q.get("state") not in ("playing", "paused"):
            return "Nothing's playing right now."
        a = it.action
        if a == "group_all":
            added = self._group(leader, self.everywhere)
            return "Playing everywhere." if added else "I can't add any other speakers to this."
        target = self._player_name(it.speaker)
        label = self._label(target)
        if a == "group_add":
            if target == leader or self._player_id(target) in self._members(leader):
                return f"It's already playing on {label}."
            return f"Adding {label}." if self._group(leader, [target]) else f"I can't add {label} to this."
        if a == "group_remove":
            if target == leader:
                if not self._members(leader):
                    self.ma.cmd("player_queues/pause", queue_id=self._player_id(leader))
                    return ""
                return f"{label[0].upper()}{label[1:]} is leading the music; say pause to stop everything."
            if self._player_id(target) not in self._members(leader):
                return f"It isn't playing on {label}."
            self.ma.cmd("players/cmd/ungroup", player_id=self._player_id(target))
            return f"Okay, not on {label}."
        # group_only
        if target != leader:
            return f"Say move the music to {label.removeprefix('the ')} for that."
        others = self._members(leader)
        if others:
            self.ma.cmd("players/cmd/ungroup_many", player_ids=others)
        return f"Okay, just {label}."

    def _control(self, it: MusicIntent) -> str:
        a = it.action
        if a.startswith("group_"):
            return self._multiroom(it)
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

        if a == "sleep_cancel":
            if not self._sleep:
                return "There's no sleep timer set."
            self._cancel_sleep()
            return "Okay, the music will keep playing."
        player = self._control_target(it.speaker)
        pid = self._player_id(player)
        q = self.ma.cmd("player_queues/get", queue_id=pid) or {}
        cur = q.get("current_item")
        if a in self._NAVIGATION:
            if not cur or q.get("state") not in ("playing", "paused"):
                return "Nothing's playing right now."
            return self._navigate(it, player, pid, q, cur)

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
                if self._members(player):
                    self.ma.cmd("players/cmd/group_volume", player_id=pid, volume_level=it.level)
                else:
                    self.ma.cmd("players/cmd/volume_set", player_id=pid, volume_level=it.level)
                return f"Music volume {it.level}."
            self._unduck(player)      # step from the real level, not the ducked one
            grouped = bool(self._members(player))
            self.ma.cmd(f"players/cmd/{'group_' if grouped else ''}{a}", player_id=pid)
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
