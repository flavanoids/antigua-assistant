"""Drive times: "how long to get to Lowe's on Ella", "how far is Mom's house",
"how's traffic to work". Car only, always from home. Deterministic replies.

Pieces, none of them Google:
  places    server/config/places.yaml (git-ignored, chmod 600): home and the
            household's favorites — people's houses, work. Never spoken back
            as addresses, never logged, never handed to the LLM.
  search    Photon (photon.komoot.io, OpenStreetMap data) for businesses near
            home and for addresses in places.yaml. No key.
  routing   TomTom when TOMTOM_API_KEY is in the mcp env file (live traffic +
            what the trip usually takes at this hour); otherwise OSRM
            (OpenStreetMap, no traffic of its own). No Google either way.
  traffic   Houston TranStar (transtar.py): live speeds vs normal and incidents
            on the route. Preferred over TomTom for "is it unusual" wherever it
            has coverage.

Which store: a qualifier ("on Ella", "Bunker Hill", "in Montrose") picks among
a chain's locations; without one, the nearest wins when it's clearly the
nearest, and otherwise Antigua asks which one.
"""

import hashlib
import json
import logging
import math
import os
import re
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests
import yaml

from . import settings
from .intents.commute import CommuteRequest
from .transtar import TranStar, spoken_incident, spoken_segment

log = logging.getLogger("antigua_core")


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    kind: str = "poi"        # "poi" | "favorite" | "home" | "place" | "address"
    street: str = ""
    housenumber: str = ""
    district: str = ""
    locality: str = ""
    city: str = ""
    osm_value: str = ""
    label: str = ""          # how a reply names it; set by resolve()
    private: bool = False    # favorites/home: coordinates rounded before they leave the house
    note: str = ""           # said before the answer ("I couldn't find a Target on 43rd...")


@dataclass
class Reply:
    text: str
    pending: dict | None = None   # set when the reply asks "which one?"


# ── Places file ──────────────────────────────────────────────────────────────


def _norm(s: str) -> str:
    """'Niko Niko's' / 'niko nikos' -> 'nikonikos'; 'H-E-B' / 'HEB' -> 'heb'."""
    s = s.lower().replace("&", "and").replace("centre", "center")
    s = re.sub(r"^(?:the|my|our)\s+", "", s.strip())
    return re.sub(r"[^a-z0-9]", "", s)


def _favorite_keys(phrase: str) -> set[str]:
    """Every way a spoken destination might name a favorite: 'my mom's house'
    -> {'momshouse', 'moms', 'mom'}."""
    p = re.sub(r"^(?:the|my|our)\s+", "", phrase.lower().strip())
    keys = {_norm(p)}
    bare = re.sub(r"\s+(?:house|place|apartment|apt|home|condo)$", "", p)
    keys.add(_norm(bare))
    keys.add(_norm(re.sub(r"'?s$", "", bare)))
    return {k for k in keys if k}


class Places:
    """places.yaml: home + favorites, reloaded when the file changes."""

    def __init__(self, path=None, geocode=None):
        self._path = Path(path or settings.COMMUTE_PLACES_PATH)
        self._geocode = geocode           # (address) -> (lat, lon) | None
        self._mtime = None
        self._home: Place | None = None
        self._favorites: list[tuple[set, Place]] = []
        self._lock = threading.Lock()
        self._cache_path = settings.DATA_DIR / "commute_places.json"

    def _load_cache(self) -> dict:
        try:
            return json.loads(self._cache_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_cache(self, data: dict):
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
        except OSError as e:
            log.warning("commute: place cache save failed: %s", type(e).__name__)

    def _point(self, entry: dict, cache: dict) -> tuple | None:
        if entry.get("lat") is not None and entry.get("lon") is not None:
            return float(entry["lat"]), float(entry["lon"])
        addr = (entry.get("address") or "").strip()
        if not addr or self._geocode is None:
            return None
        # Keyed by a hash so the cache file doesn't hold a second copy of the address.
        k = hashlib.sha256(addr.lower().encode()).hexdigest()[:24]
        if k in cache:
            return tuple(cache[k])
        pt = self._geocode(addr)
        if pt:
            cache[k] = list(pt)
        return pt

    def _reload(self):
        try:
            st = self._path.stat()
        except OSError:
            if self._mtime is not None or self._home is None:
                log.info("commute: no %s — home from weather settings, no favorites", self._path.name)
            self._mtime, self._favorites = None, []
            self._home = Place("home", settings.WEATHER_HOME_LAT, settings.WEATHER_HOME_LON,
                               kind="home", private=True)
            return
        if st.st_mtime == self._mtime:
            return
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            log.warning("commute: %s is readable by other users — chmod 600 it", self._path)
        try:
            data = yaml.safe_load(self._path.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            log.warning("commute: %s unreadable: %s", self._path.name, type(e).__name__)
            data = {}
        cache = self._load_cache()
        before = dict(cache)
        home = self._point(data.get("home") or {}, cache)
        if home is None:
            log.warning("commute: home in %s has no lat/lon and its address didn't geocode — "
                        "using the weather home point", self._path.name)
            home = (settings.WEATHER_HOME_LAT, settings.WEATHER_HOME_LON)
        favorites = []
        for f in data.get("favorites") or []:
            name = (f.get("name") or "").strip()
            pt = self._point(f, cache) if name else None
            if pt is None:
                if name:
                    log.warning("commute: favorite %r has no location", name)
                continue
            keys = set()
            for alias in [name, *(f.get("aliases") or [])]:
                keys |= _favorite_keys(str(alias))
            favorites.append((keys, Place(f.get("say_as") or name, *pt, kind="favorite", private=True)))
        if cache != before:
            self._save_cache(cache)
        self._home = Place("home", *home, kind="home", private=True)
        self._favorites = favorites
        self._mtime = st.st_mtime
        log.info("commute: loaded home + %d favorites", len(favorites))

    def home(self) -> Place:
        with self._lock:
            self._reload()
            return self._home

    def favorite(self, phrase: str) -> Place | None:
        with self._lock:
            self._reload()
            keys = _favorite_keys(phrase)
            for fkeys, place in self._favorites:
                if keys & fkeys:
                    return place
        return None


# ── Search (Photon / OpenStreetMap) ──────────────────────────────────────────

# Same-brand records that aren't the store itself.
_SIDE_VALUES = {"fuel", "car_wash", "pharmacy", "atm", "parking", "charging_station",
                "vending_machine", "bicycle_rental", "parcel_locker", "post_box"}
_POI_KEYS = {"shop", "amenity", "leisure", "tourism", "office", "craft", "healthcare",
             "sport", "club", "historic", "aeroway", "railway"}


def _feature_to_place(f: dict) -> Place:
    p = f.get("properties", {})
    lon, lat = f["geometry"]["coordinates"][:2]
    district = (p.get("district") or "").split("/")[-1].strip()
    key = p.get("osm_key", "")
    kind = "poi" if key in _POI_KEYS else ("place" if key == "place" else "other")
    return Place(p.get("name") or "", lat, lon, kind=kind, street=p.get("street") or "",
                 housenumber=p.get("housenumber") or "", district=district,
                 locality=p.get("locality") or "", city=p.get("city") or "",
                 osm_value=p.get("osm_value") or "")


def _km(a: Place, b: Place) -> float:
    dx = (b.lon - a.lon) * 111.32 * math.cos(math.radians(a.lat))
    dy = (b.lat - a.lat) * 111.32
    return math.hypot(dx, dy)


class Photon:
    def __init__(self, url=None, timeout=None):
        self._url = (url or settings.COMMUTE_PHOTON_URL).rstrip("/")
        self._timeout = timeout or settings.COMMUTE_TIMEOUT
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "Antigua-home-assistant/1.0"
        self._cache: dict[tuple, tuple[list, float]] = {}
        self._lock = threading.Lock()

    def search(self, q: str, near: Place, *, local=True, limit=15) -> list[Place]:
        """Places matching q. local=True keeps to ~45 km around `near`; the
        bias and box use a ~10 km-rounded point, not the house."""
        lat, lon = round(near.lat, 1), round(near.lon, 1)
        key = (q.lower(), lat, lon, local, limit)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
        if hit and now - hit[1] < 86400:
            return hit[0]
        params = {"q": q, "lat": lat, "lon": lon, "limit": limit, "lang": "en"}
        if local:
            params["bbox"] = f"{lon - 0.5},{lat - 0.42},{lon + 0.5},{lat + 0.42}"
        try:
            r = self._session.get(f"{self._url}/api/", params=params, timeout=self._timeout)
            r.raise_for_status()
            places = [_feature_to_place(f) for f in r.json().get("features", [])]
        except Exception as e:
            # Never the exception text: its URL carries the query, which can be
            # a favorite's street address.
            log.warning("commute: place search failed (%s)", type(e).__name__)
            return []
        with self._lock:
            self._cache[key] = (places, now)
        return places

    def geocode(self, address: str, near: Place | None = None) -> tuple | None:
        near = near or Place("", settings.WEATHER_HOME_LAT, settings.WEATHER_HOME_LON)
        hits = self.search(address, near, local=False, limit=1)
        return (hits[0].lat, hits[0].lon) if hits else None


# ── Resolving a spoken destination ───────────────────────────────────────────

_QUALIFIER_SPLIT_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<sep>on|in|at|by|near|off(?:\s+of)?|over\s+(?:on|by|in)|close\s+to|"
    r"next\s+to|around)\s+(?:the\s+)?(?P<qual>.+)$", re.I)

# Spoken road names -> what OpenStreetMap calls them. Houston's, mostly; extend
# per house with places.yaml `road_aliases`.
_ROAD_ALIASES = {
    "beltway": "samhouston", "beltway8": "samhouston", "bw8": "samhouston",
    "thebeltway": "samhouston", "tollway": "samhouston",
    "610": "loop", "theloop": "loop",
    "290": "northwestfreeway", "us290": "northwestfreeway",
}
# Street-type words say nothing about which store: "Ella Boulevard" is "Ella".
_GENERIC_WORDS = {"road", "street", "boulevard", "drive", "avenue", "lane", "parkway",
                  "freeway", "highway", "way", "north", "south", "east", "west", "loop",
                  "the", "one", "near", "area", "side", "there", "that", "this"}
_QUAL_NEAR_KM = 1.2      # a qualifier street/area this close to a store picks it
_SIBLING_KM = 0.6        # same-brand fuel/pharmacy records count as the store's streets
_DEDUPE_KM = 0.5         # same name this close = one place (a campus and its gate)
_CLEARLY_NEAREST = 1.4   # nearest is "the" one when the next is this many times farther
_MAX_OPTIONS = 3


# Places people name by a short form: "Hobby Airport" is "William P. Hobby
# Airport"; "Minute Maid Park" is now "Daikin Park". A loose match or Photon's
# own top hit is only trusted for these kinds of places.
_LANDMARK_VALUES = {"aerodrome", "stadium", "university", "college", "mall", "hospital", "museum",
                    "arts_centre", "theatre", "zoo", "theme_park", "attraction", "park",
                    "events_venue", "conference_centre", "exhibition_centre", "station",
                    "library", "cinema", "sports_centre", "golf_course", "aquarium"}
_AREA_VALUES = {"suburb", "neighbourhood", "quarter", "city", "town", "village", "hamlet", "borough"}
_FILLER_TOKENS = {"the", "a", "an", "of", "at", "and"}


def _dedupe_km(a: Place, b: Place) -> float:
    """Two same-named records this close are one place. A campus or airport
    shows up as several points (Rice University and its memorial)."""
    return 1.5 if _LANDMARK_VALUES & {a.osm_value, b.osm_value} else _DEDUPE_KM


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower().replace("'s", "s").replace("'", "")))


def _name_matches(query: str, place: Place) -> tuple[int, int]:
    """(tier, extra words). 3 = same name, or the name plus the home city
    ('POST Houston', 'Houston Heights'); 1 = starts with it ('Lowe's Home
    Improvement') or, for a landmark, has every word of it ('William P. Hobby
    Airport'); 0 = no match. Among tier 1, fewest extra words wins."""
    qn, n = _norm(query), _norm(place.name)
    city = _norm(settings.SEARCH_HOME_CITY)
    if not n or not qn:
        return 0, 0
    if n == qn or (city and n in (qn + city, city + qn)):
        return 3, 0
    if place.osm_value in _SIDE_VALUES:
        return 0, 0
    skip = _FILLER_TOKENS | _tokens(settings.SEARCH_HOME_CITY)
    qt, nt = _tokens(query) - skip, _tokens(place.name) - _FILLER_TOKENS
    extra = len(nt - qt - skip)
    if n.startswith(qn):
        return 1, extra
    # Any word order only for landmarks and neighborhoods: a hotel named for
    # the airport isn't the airport.
    if place.osm_value in _LANDMARK_VALUES or place.osm_value in _AREA_VALUES:
        if qt and qt <= nt:
            return 1, extra
    return 0, 0


def _qual_keys(qual: str, aliases: dict) -> set[str]:
    q = _norm(qual)
    keys = {q}
    for k, v in aliases.items():
        if q == k or q.startswith(k) or q.endswith(k):
            keys.add(v)
    keys |= {_norm(w) for w in qual.split() if len(_norm(w)) >= 4 and _norm(w) not in _GENERIC_WORDS}
    return {k for k in keys if k and k not in _GENERIC_WORDS}


def _text_fields(p: Place) -> list[str]:
    return [_norm(x) for x in (p.street, p.district, p.locality, p.city, p.name,
                               p.osm_value.replace("_", " ")) if x]


def _text_match(keys: set, p: Place) -> bool:
    fields = _text_fields(p)
    return any(k in f for k in keys for f in fields)


def _mid_sentence(name: str) -> str:
    """'The Galleria' reads 'the Galleria' inside a reply; 'George Bush
    Intercontinental Airport - Houston' drops the home city."""
    name = name.split(";")[-1].strip()
    city = settings.SEARCH_HOME_CITY
    if city:
        name = re.sub(rf"\s*[-–,]\s*{re.escape(city)}$", "", name, flags=re.I)
    if re.fullmatch(r"(?:Downtown|Midtown|Uptown)", name):
        return name.lower()
    return re.sub(r"^The\s", "the ", name)


def _plural(name: str) -> str:
    name = re.sub(r"^The\s+", "", name)
    if name.lower().endswith(("s", "'s")):
        return name
    if re.search(r"[^aeiou]y$", name, re.I):
        return name[:-1] + "ies"
    return name + "s"


def _a(word: str) -> str:
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def describe(p: Place) -> str:
    """'on Montrose Boulevard' / 'on West Sam Houston Parkway North in CityCentre'."""
    parts = []
    if p.street:
        parts.append(f"on {p.street}")
    area = p.district or p.locality
    if area and _norm(area) not in _norm(p.street) and (not p.street or area != p.city):
        parts.append(f"in {area}")
    if not parts and p.city:
        parts.append(f"in {p.city}")
    return " ".join(parts)


def _sentence_list(items: list[str]) -> str:
    if len(items) <= 2:
        return " or ".join(items)
    return ", ".join(items[:-1]) + ", or " + items[-1]


class Resolver:
    def __init__(self, photon: Photon, places: Places, road_aliases=None):
        self.photon = photon
        self.places = places
        self.aliases = {**_ROAD_ALIASES, **{_norm(k): _norm(v) for k, v in (road_aliases or {}).items()}}

    def _town(self, name: str, home: Place) -> Place | None:
        for p in self.photon.search(name, home, local=False, limit=5):
            if p.kind == "place" and _norm(p.name) == _norm(name):
                return p
        return None

    def _chain(self, name: str, home: Place, results: list, qual_reading=False) -> list[Place]:
        """Places called `name` within the search radius, nearest first: one
        chain's locations, or a few different places sharing the name."""
        scored = []
        for p in results:
            usable = p.kind == "poi" or (p.kind == "place" and p.osm_value in _AREA_VALUES)
            if not usable or _km(home, p) > settings.COMMUTE_SEARCH_RADIUS_KM:
                continue
            tier, extra = _name_matches(name, p)
            if tier:
                scored.append((tier, extra, p))
        if not scored:
            return []
        if max(t for t, _, _ in scored) < 3 and not qual_reading and self._town(name, home):
            return []   # "Galveston" is the city, not "Galveston Bay/Harbor"
        best = max(t for t, _, _ in scored)
        group = [(e, p) for t, e, p in scored if t == best]
        if best == 1:
            # Fewest extra words, and a landmark over the hotels named after it
            # ("Hyatt Place Bush Intercontinental Airport").
            fewest = min(e for e, _ in group)
            group = [(e, p) for e, p in group if e == fewest]
            for keep in (_AREA_VALUES, _LANDMARK_VALUES):   # "the medical center" is the district
                if any(p.osm_value in keep for _, p in group):
                    group = [(e, p) for e, p in group if p.osm_value in keep]
                    break
        # One place per real place: the exact name before its "+ city" variant,
        # a neighborhood before a park or trail sharing its name, and two
        # matches this close together are one place under two names (The
        # Galleria, The Houston Galleria).
        group.sort(key=lambda ep: (_norm(ep[1].name) != _norm(name), ep[1].kind != "place"))
        out: list[Place] = []
        for _, p in group:
            if any(_km(o, p) < _DEDUPE_KM
                   or (_norm(o.name) == _norm(p.name)
                       and (_km(o, p) < _dedupe_km(o, p) or "place" in (o.kind, p.kind)))
                   for o in out):
                continue
            out.append(p)
        return sorted(out, key=lambda p: _km(home, p))

    def _qualified(self, chain, qual, results, home) -> list[Place]:
        """The chain's locations the qualifier points at."""
        keys = _qual_keys(qual, self.aliases)
        hits = [p for p in chain if _text_match(keys, p)]
        if hits:
            return hits
        # "H-E-B Bunker Hill": the store's address is on Katy Freeway, but its
        # gas station and car wash are listed on Bunker Hill Road.
        side = [s for s in results if s.street and _text_match(keys, s)
                and _norm(s.name).startswith(_norm(chain[0].name)[:3])]
        hits = [p for p in chain if any(_km(p, s) <= _SIBLING_KM for s in side)]
        if hits:
            return hits
        # "Lowe's on Ella": the qualifier is a street or area near the store.
        spots = [s for s in self.photon.search(qual, home) if _km(home, s) <= settings.COMMUTE_SEARCH_RADIUS_KM]
        scored = sorted((min(_km(p, s) for s in spots), i, p) for i, p in enumerate(chain)) if spots else []
        return [p for d, _, p in scored if d <= _QUAL_NEAR_KM]

    def resolve(self, dest: str) -> tuple[str, object]:
        """('found', Place) | ('ask', (name, [Place])) | ('home', None) | ('none', None)."""
        if _norm(dest) in ("home", "myhouse", "thehouse", "ourhouse", "here"):
            return "home", None
        fav = self.places.favorite(dest)
        if fav is not None:
            fav.label = fav.name
            return "found", fav
        home = self.places.home()

        # A street address: "1521 North Loop West".
        if re.match(r"^\d+\s+\w", dest):
            hits = [p for p in self.photon.search(dest, home, limit=3)
                    if _km(home, p) <= settings.COMMUTE_SEARCH_RADIUS_KM * 2]
            if hits:
                p = hits[0]
                p.kind = "address"
                p.label = f"{p.housenumber} {p.street}".strip() or dest
                return "found", p
            return "none", None

        splits = []   # (name, qualifier) readings, most literal first
        m = _QUALIFIER_SPLIT_RE.match(dest)
        if m:
            splits.append((m.group("name"), m.group("qual")))
        splits.append((dest, None))
        words = dest.split()
        if not m and 2 <= len(words) <= 5:
            splits += [(" ".join(words[:k]), " ".join(words[k:])) for k in range(len(words) - 1, 0, -1)]

        names = list(dict.fromkeys(n for n, _ in splits))
        # "The POST" is listed as "POST Houston", "the Heights" as "Houston
        # Heights": the whole phrase is also searched with the home city.
        city = settings.SEARCH_HOME_CITY
        bare = re.sub(r"^the\s+", "", dest, flags=re.I)
        with_city = f"{bare} {city}" if city and city.lower() not in dest.lower() else None
        # And without "the": Photon takes "the Houston Zoo" literally.
        extra = [q for q in (with_city, bare) if q and q not in names]
        queries = names + extra
        with ThreadPoolExecutor(max_workers=4) as ex:
            found = dict(zip(queries, ex.map(lambda n: self.photon.search(n, home, limit=25), queries)))
        for q in extra:
            found[dest] = found[dest] + found.pop(q)
        # Every record any reading turned up, for the same-brand side listings.
        pool = [p for ps in found.values() for p in ps]

        missed = None   # "Target on 43rd": there's a Target, just not on 43rd
        for name, qual in splits:
            chain = self._chain(name, home, found[name], qual_reading=bool(qual))
            if not chain:
                continue
            if qual:
                picked = self._qualified(chain, qual, pool, home)
                if not picked:
                    if m and missed is None and name == m.group("name"):
                        missed = (chain, f"I couldn't find {_a(chain[0].name)} {m.group('sep').lower()} "
                                         f"{m.group('qual')}, so here's the closest one.")
                    continue
                return self._pick(picked, home, chain)
            return self._pick(chain, home, chain)

        if missed:
            chain, note = missed
            p = chain[0]
            self._label([p], chain)
            p.note = note
            return "found", p

        # Photon's own best hit for a landmark under a name OSM doesn't use any
        # more ("Minute Maid Park" -> Daikin Park).
        top = next((p for p in found[dest] if p.kind == "poi"), None)
        words = _tokens(bare) - _FILLER_TOKENS - _tokens(settings.SEARCH_HOME_CITY)
        if (top and top.osm_value in _LANDMARK_VALUES and len(words) >= 2
                and _km(home, top) <= settings.COMMUTE_SEARCH_RADIUS_KM):
            top.label = _mid_sentence(top.name)
            return "found", top

        # A town or city ("how long to Galveston"), any distance.
        p = self._town(dest, home)
        if p:
            p.label = _mid_sentence(p.name)
            return "found", p
        return "none", None

    @staticmethod
    def _label(cands: list[Place], chain: list[Place]):
        for p in cands:
            same = sum(1 for o in chain if o.name == p.name)
            bare = re.sub(r"^The\s+", "", p.name)
            p.label = f"the {bare} {describe(p)}" if same > 1 and describe(p) else _mid_sentence(p.name)

    def _pick(self, cands: list[Place], home: Place, chain: list[Place]):
        self._label(cands, chain)
        if len(cands) == 1:
            return "found", cands[0]
        if len({p.name for p in cands}) > 1:
            # Different places that share a name (The Post, POST Houston):
            # nearest says nothing about which one was meant.
            return "ask", (None, cands[:_MAX_OPTIONS])
        d1, d2 = _km(home, cands[0]), _km(home, cands[1])
        if d2 >= d1 * _CLEARLY_NEAREST:
            return "found", cands[0]
        opts = [p for p in cands if _km(home, p) < d1 * _CLEARLY_NEAREST][:_MAX_OPTIONS]
        return "ask", (cands[0].name, opts)


def ask_text(name: str | None, opts: list[Place]) -> str:
    """name None = differently named places: "Do you mean The Post on North
    Main Street, or POST Houston on Franklin Street?"
    """
    if name is None:
        return f"Do you mean {_sentence_list([f'{p.name} {describe(p)}'.strip() for p in opts])}?"
    n = len(opts)
    count = "two" if n == 2 else "a few"
    descs = [describe(p) or p.name for p in opts]
    if n == 2:
        return f"There are two {_plural(name)} near you: one {descs[0]}, and one {descs[1]}. Which one?"
    return f"There are {count} {_plural(name)} near you: {_sentence_list(descs)}. Which one?"


_ANSWER_STOP = {"on", "in", "by", "at", "please", "i", "mean", "meant", "uh", "um", "oh", "yeah",
                "store", "location", "go", "to", "with", "over", "off", "of", "and", "is", "it"} | _GENERIC_WORDS
_NEAREST_RE = re.compile(r"\b(?:closer|closest|nearest|nearer|either|whichever|any|doesn'?t\s+matter)\b", re.I)
_CANCEL_RE = re.compile(r"^(?:never\s*mind|nevermind|cancel|forget\s+it|neither|no(?:ne)?|stop|nothing)\b", re.I)
_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "last": -1}


def pick_answer(text: str, opts: list[Place], aliases: dict) -> int | str | None:
    """Index of the option the answer names, 'cancel', or None (not an answer)."""
    t = text.strip().lower().rstrip(".!?")
    if not t:
        return None
    if _CANCEL_RE.match(t):
        return "cancel"
    for w, i in _ORDINALS.items():
        if re.search(rf"\b{w}\b", t):
            return i % len(opts)
    if _NEAREST_RE.search(t):
        return 0     # options are nearest first
    words = [w for w in re.findall(r"[a-z0-9']+", t) if w not in _ANSWER_STOP]
    if not words or len(words) > 8:
        return None
    keys = _qual_keys(" ".join(words), aliases) | {_norm(w) for w in words if len(_norm(w)) >= 3}
    keys -= _GENERIC_WORDS
    scores = [sum(1 for k in keys if any(k in f for f in _text_fields(p))) for p in opts]
    best = max(scores)
    if best and scores.count(best) == 1:
        return scores.index(best)
    return None


# ── Routing ──────────────────────────────────────────────────────────────────


@dataclass
class Route:
    seconds: float              # best estimate now, with traffic where known
    meters: float
    points: list                # [(lat, lon)]
    usual_seconds: float | None = None   # TomTom: this trip at this hour, historically
    source: str = "osrm"


def _send_point(p: Place) -> tuple:
    """Home and favorites leave the house rounded to ~100 m."""
    return (round(p.lat, 3), round(p.lon, 3)) if p.private else (p.lat, p.lon)


class Router:
    def __init__(self, tomtom_key=None, osrm_url=None, timeout=None):
        self._key = tomtom_key
        self._osrm = (osrm_url or settings.COMMUTE_OSRM_URL).rstrip("/")
        self._timeout = timeout or settings.COMMUTE_TIMEOUT
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "Antigua-home-assistant/1.0"

    @property
    def live(self) -> bool:
        return bool(self._key)

    def route(self, a: Place, b: Place) -> Route | None:
        if self._key:
            r = self._tomtom(a, b)
            if r is not None:
                return r
        return self._osrm_route(a, b)

    def _tomtom(self, a: Place, b: Place) -> Route | None:
        (alat, alon), (blat, blon) = _send_point(a), _send_point(b)
        url = f"https://api.tomtom.com/routing/1/calculateRoute/{alat},{alon}:{blat},{blon}/json"
        try:
            r = self._session.get(url, timeout=self._timeout, params={
                "key": self._key, "traffic": "true", "travelMode": "car",
                "routeType": "fastest", "computeTravelTimeFor": "all"})
            r.raise_for_status()
            return parse_tomtom(r.json())
        except Exception as e:
            # Not the message: the URL in it carries the API key.
            log.warning("commute: TomTom routing failed (%s) — trying OSRM", type(e).__name__)
            return None

    def _osrm_route(self, a: Place, b: Place) -> Route | None:
        (alat, alon), (blat, blon) = _send_point(a), _send_point(b)
        try:
            r = self._session.get(f"{self._osrm}/route/v1/driving/{alon},{alat};{blon},{blat}",
                                  params={"overview": "full", "geometries": "geojson"},
                                  timeout=self._timeout)
            r.raise_for_status()
            return parse_osrm(r.json())
        except Exception as e:
            log.warning("commute: OSRM routing failed (%s)", type(e).__name__)
            return None


def parse_tomtom(data: dict) -> Route | None:
    routes = data.get("routes") or []
    if not routes:
        return None
    s = routes[0]["summary"]
    pts = [(p["latitude"], p["longitude"]) for leg in routes[0].get("legs", [])
           for p in leg.get("points", [])]
    return Route(s["travelTimeInSeconds"], s["lengthInMeters"], pts,
                 s.get("historicTrafficTravelTimeInSeconds"), "tomtom")


def parse_osrm(data: dict) -> Route | None:
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    r = data["routes"][0]
    pts = [(la, lo) for lo, la in r["geometry"]["coordinates"]]
    return Route(r["duration"], r["distance"], pts, None, "osrm")


# ── Putting it together ──────────────────────────────────────────────────────


@dataclass
class Trip:
    seconds: float
    miles: float
    live: bool                 # traffic is counted in `seconds`
    excess_s: float            # over what this trip usually takes now
    usual_s: float
    slow: list = field(default_factory=list)       # [Segment]
    incidents: list = field(default_factory=list)  # [Incident]

    @property
    def unusual(self) -> bool:
        return self.live and self.excess_s >= max(settings.COMMUTE_UNUSUAL_MIN_MINUTES * 60,
                                                  self.usual_s * settings.COMMUTE_UNUSUAL_PCT / 100)


_TRANSTAR_TRUST_COVERAGE = 0.3   # of the route's length


def combine(route: Route, traffic) -> Trip:
    """One estimate from a route and TranStar's view of it (traffic may be None).

    TomTom: its live time is the ETA. What counts as unusual comes from TranStar
    where it covers enough of the route, else from TomTom's historical time.
    OSRM: no traffic of its own, so TranStar's delays are added on top of it.
    """
    covered = traffic.covered_m / route.meters if traffic and route.meters else 0.0
    ts_excess = traffic.excess_s if traffic else 0.0
    if route.source == "tomtom":
        seconds, live = route.seconds, True
        if covered >= _TRANSTAR_TRUST_COVERAGE or route.usual_seconds is None:
            excess = ts_excess
        else:
            excess = max(0.0, route.seconds - route.usual_seconds)
    else:
        live = covered > 0
        street_excess = sum(s.excess_s * c for s, c in traffic.segments if s.kind == "street") if traffic else 0
        seconds = route.seconds + (traffic.freeway_delay_s if traffic else 0) + street_excess
        excess = ts_excess
    return Trip(seconds, route.meters / 1609.34, live, excess, max(seconds - excess, 60),
                traffic.slowest() if traffic else [], traffic.incidents if traffic else [])


def spoken_minutes(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    if m < 60:
        return "a minute" if m == 1 else f"{m} minutes"
    h, m = divmod(m, 60)
    lead = "an hour" if h == 1 else f"{h} hours"
    if m < 3:
        return lead
    return f"{lead} and {m} minutes"


def spoken_miles(miles: float) -> str:
    if miles < 0.75:
        return "less than a mile"
    n = round(miles)
    return "a mile" if n == 1 else f"{n} miles"


def road_pattern(road: str) -> re.Pattern:
    """A spoken road -> a pattern over TranStar names: 'I-10' / 'interstate 10'
    -> IH-10, '290' -> US-290, 'the beltway' -> Sam Houston / Beltway 8."""
    r = road.lower().strip()
    r = re.sub(r"^the\s+", "", r)
    if re.search(r"\b(?:beltway|bw\s*-?\s*8|sam\s+houston|tollway)\b", r):
        return re.compile(r"Sam Houston|Beltway 8|BW-?8", re.I)
    m = re.match(r"^(?:i|ih|interstate|us|sh|state\s+highway|highway|hwy|fm|loop)?[\s-]*(\d+)$", r)
    if m:
        return re.compile(rf"\b(?:IH|US|SH|FM|I)-{m.group(1)}\b", re.I)
    words = [w for w in re.findall(r"[a-z0-9]+", r) if w not in ("freeway", "street", "road", "highway")]
    return re.compile(r"\b" + r"\s+".join(map(re.escape, words or [r])) + r"\b", re.I)


def _notable(inc) -> bool:
    """Worth a heads-up on a normal day: a crash or a blocked lane on the
    freeway, a major crash on a city street (HPD logs every fender-bender)."""
    d = inc.desc.lower()
    if inc.kind == "street":
        return "major" in d
    return "accident" in d or "collision" in d or "lane" in inc.lanes.lower()


def _worst_incident(incidents) -> object | None:
    def rank(i):
        d = i.desc.lower()
        return (("accident" in d or "collision" in d), "lane" in i.lanes.lower(), i.kind == "freeway")
    return max(incidents, key=rank) if incidents else None


def phrase_trip(trip: Trip, label: str, kind: str) -> str:
    eta = spoken_minutes(trip.seconds)
    if kind == "distance":
        lead = f"{label[0].upper()}{label[1:]} is about {spoken_miles(trip.miles)} away, about {eta} by car."
    elif kind == "traffic" and trip.live:
        state = "heavier than usual" if trip.unusual else "normal"
        lead = f"Traffic to {label} looks {state}. It's about {eta}."
    else:
        lead = f"It's about {eta} to {label}."
    worst = _worst_incident(trip.incidents)
    if not trip.live:
        if worst and _notable(worst):
            return f"{lead} I don't have live speeds for that route, but there's {spoken_incident(worst)}."
        return f"{lead} That's without live traffic."
    parts = [lead]
    if trip.unusual:
        parts.append(f"That's about {spoken_minutes(trip.excess_s)} more than usual.")
        why = [f"traffic's slow on {spoken_segment(s)}" for s in trip.slow[:1]]
        if worst:
            why.append(f"there's {spoken_incident(worst)}")
        if why:
            s = " and ".join(why)
            parts.append(s[0].upper() + s[1:] + ".")
    else:
        if kind != "traffic":
            parts.append("Traffic looks normal.")
        if worst and _notable(worst):
            parts.append(f"Heads up, there's {spoken_incident(worst)}.")
    return " ".join(parts)


class CommuteProvider:
    def __init__(self, tomtom_key=None, places=None, photon=None, router=None, transtar=None):
        self.photon = photon or Photon()
        self.places = places or Places(geocode=lambda a: self.photon.geocode(a))
        self.resolver = Resolver(self.photon, self.places, settings.COMMUTE_ROAD_ALIASES)
        self.router = router or Router(tomtom_key)
        self.transtar = transtar if transtar is not None else (
            TranStar(settings.COMMUTE_TIMEOUT) if settings.COMMUTE_TRANSTAR else None)

    def answer(self, req: CommuteRequest) -> Reply | None:
        """None = no place by that name; the caller lets the LLM have it."""
        if req.kind == "area":
            return self.area_report(req.dest)
        status, val = self.resolver.resolve(req.dest)
        if status == "home":
            return Reply("You're already home.")
        if status == "none":
            log.info("commute: no place found for %r", req.dest)
            return None
        if status == "ask":
            name, opts = val
            log.info("commute: %d %s nearby, asking which", len(opts), name or "places by that name")
            return Reply(ask_text(name, opts),
                         pending={"options": [asdict(p) for p in opts], "kind": req.kind})
        reply = self.trip_reply(val, req.kind)
        if val.note:
            reply.text = f"{val.note} {reply.text}"
        return reply

    def area_report(self, road: str) -> Reply:
        """"How's traffic" / "is there traffic on I-10": TranStar around home."""
        if self.transtar is None:
            return Reply("I don't have live traffic here.")
        home = self.places.home()
        segments, incidents = self.transtar.fetch_all()
        near_km = settings.COMMUTE_AREA_RADIUS_KM
        reach = near_km * 1.5 if road else near_km
        seg_near = [s for s in segments if _km(home, Place("", *s.points[len(s.points) // 2])) <= reach]
        if not seg_near:
            return Reply("I only have live traffic for the Houston area.")
        inc_near = [i for i in incidents if _km(home, Place("", i.lat, i.lon)) <= near_km]
        where = "around you"
        if road:
            rx = road_pattern(road)
            # The segment's own road: "Bellaire from Gessner to Beltway 8-West"
            # is Bellaire, not the Beltway.
            seg_near = [s for s in seg_near if rx.search(s.name.split(" from ")[0])]
            inc_near = [i for i in incidents if rx.search(re.split(r" (?:At|Before|After|Near|@) ", i.location)[0])
                        and _km(home, Place("", i.lat, i.lon)) <= reach]
            where = f"on {_mid_sentence(road)}"
            if not seg_near and not inc_near:
                return Reply(f"I don't see {road} in the traffic data.")
        slow = sorted((s for s in seg_near if s.excess_s >= 120 and s.pct_slower >= 25),
                      key=lambda s: -s.excess_s)
        worst = _worst_incident([i for i in inc_near if i.kind == "freeway"] or inc_near)
        parts = []
        if slow:
            parts.append(f"{spoken_segment(slow[0])} is running about "
                         f"{spoken_minutes(slow[0].excess_s)} slower than usual")
        if worst:
            parts.append(f"there's {spoken_incident(worst)}")
        log.info("commute: traffic report %s — %d slow, %d incidents", where, len(slow), len(inc_near))
        if not parts:
            return Reply(f"Traffic {where} looks normal.")
        text = " and ".join(parts)
        return Reply(text[0].upper() + text[1:] + ".")

    def choose(self, pending: dict, text: str) -> Reply | None:
        """The answer to "which one?" — None if `text` isn't one."""
        opts = [Place(**d) for d in pending["options"]]
        i = pick_answer(text, opts, self.resolver.aliases)
        if i is None:
            # A short reply right after the question is meant as the answer —
            # ask once more rather than send "the one on I-10" to the LLM.
            if pending.get("retried") or len(text.split()) > 6:
                return None
            choices = _sentence_list([f"the one {describe(p) or p.name}" for p in opts])
            return Reply(f"Sorry, which one? {choices[0].upper()}{choices[1:]}?",
                         pending={**pending, "retried": True})
        if i == "cancel":
            return Reply("Okay.")
        return self.trip_reply(opts[i], pending["kind"])

    def trip_reply(self, dest: Place, kind: str) -> Reply:
        home = self.places.home()
        route = self.router.route(home, dest)
        if route is None:
            return Reply("I couldn't get directions right now.")
        traffic = self.transtar.along(route.points) if self.transtar and route.points else None
        trip = combine(route, traffic)
        # Favorites by name only: their whereabouts stay out of the log.
        log.info("commute: %s (%s) %.0f min, %.1f mi, excess %.0f s, live=%s, %d slow, %d incidents",
                 dest.label if not dest.private else f"favorite {dest.name!r}", route.source,
                 trip.seconds / 60, trip.miles, trip.excess_s, trip.live,
                 len(trip.slow), len(trip.incidents))
        return Reply(phrase_trip(trip, dest.label or dest.name, kind))
