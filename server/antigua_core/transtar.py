"""Houston TranStar live traffic: speeds vs normal, and incidents, along a route.

No key and no sign-up: these are the files TranStar's own public traffic map
(traffic.houstontranstar.org/layers/) loads every minute. The documented XML
feeds (/datafeed/getdatafeed.aspx) need access granted by TranStar and 403
without it.

  speed_segments.js            freeway AVI segments: geometry, speed, travel
                               time, % slower than usual, delay vs free flow
  coh_bt_segments.js           City of Houston Bluetooth arterial segments
                               (11th St, Westheimer...): geometry, speed,
                               travel time, % slower than usual
  incidents_json.js            freeway incidents TranStar operators verified
  coh_hpd_incidents_json.js    HPD crash calls on city streets

Only the Houston area is covered; a route elsewhere simply matches nothing.
"""

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import requests

log = logging.getLogger("antigua_core")

BASE = "https://traffic.houstontranstar.org"
_FEEDS = {
    "freeway": "/data/layers/speed_segments.js",
    "street": "/data/layers/coh_bt_segments.js",
    "incidents": "/data/layers/incidents_json.js",
    "hpd": "/data/layers/coh_hpd_incidents_json.js",
}
_TTL = 60          # TranStar regenerates these once a minute
_HPD_MAX_AGE_MIN = 45   # HPD calls never "clear" in the feed; drop old ones

# Route matching: a TranStar segment is on the route when most of it runs
# alongside the route in the same direction. Freeway geometry is a handful of
# vertices per mile, so it can sit well off the real lanes on a curve; the
# heading check is what keeps the opposite carriageway out.
_TOL_M = {"freeway": 120, "street": 60}
_MAX_HEADING_DIFF = 50
_MIN_COVERAGE = 0.5
_INCIDENT_TOL_M = {"freeway": 150, "street": 80}


@dataclass
class Segment:
    kind: str            # "freeway" | "street"
    name: str            # "IH-10 Katy Eastbound from Silber to T.C. Jester"
    points: list         # [(lat, lon), ...]
    speed_mph: int
    travel_s: int
    pct_slower: float    # % longer than this segment usually takes now
    delay_s: int | None  # freeway only: seconds over free flow

    @property
    def excess_s(self) -> float:
        """Seconds over the usual travel time at this hour."""
        if self.pct_slower <= 0:
            return 0.0
        return self.travel_s * self.pct_slower / (100 + self.pct_slower)


@dataclass
class Incident:
    kind: str            # "freeway" | "street"
    location: str        # "IH-610 South Loop Westbound At Kirby Dr"
    desc: str            # "Heavy Truck, Stall" / "Major Accident"
    lanes: str           # "Right Shoulder" / "Left Lane,Center Lane" / ""
    direction: str       # "Westbound" / ""
    lat: float
    lon: float


@dataclass
class RouteTraffic:
    segments: list       # [(Segment, coverage 0..1)] on the route
    incidents: list      # [Incident] on the route, active
    covered_m: float     # route length TranStar has live speeds for

    @property
    def excess_s(self) -> float:
        return sum(s.excess_s * c for s, c in self.segments)

    @property
    def freeway_delay_s(self) -> float:
        return sum((s.delay_s or 0) * c for s, c in self.segments if s.kind == "freeway")

    def slowest(self, n=2) -> list:
        """The segments losing the most time against normal, worst first."""
        ranked = sorted(self.segments, key=lambda sc: -sc[0].excess_s * sc[1])
        return [s for s, c in ranked[:n] if s.excess_s * c >= 90]


# ── Parsing ──────────────────────────────────────────────────────────────────

_ARGS_RE = re.compile(r'"([^"]*)"|(-?\d+(?:\.\d+)?)')
_FREEWAY_RE = re.compile(r"new SpeedSegment\((.*?)\);")
_STREET_RE = re.compile(r"new btSeg\((.*?)\);")
_DURATION_RE = re.compile(r"(\d+)\s*(hour|minute|second)")
_BYPASS_LANES_RE = re.compile(r"\b(?:Managed Lanes|HOV|HOT)\b", re.I)


def _args(s: str) -> list[str]:
    return [a if a or b == "" else b for a, b in _ARGS_RE.findall(s)]


def _points(lats: str, lons: str) -> list:
    try:
        return [(float(a), float(o)) for a, o in zip(lats.split(), lons.split())]
    except ValueError:
        return []


def _num(s, default=0.0) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _duration_s(text: str) -> int:
    mult = {"hour": 3600, "minute": 60, "second": 1}
    return sum(int(n) * mult[u] for n, u in _DURATION_RE.findall(text))


def parse_freeway(js: str) -> list[Segment]:
    out = []
    for m in _FREEWAY_RE.finditer(js):
        a = _args(m.group(1))
        if len(a) < 14:
            continue
        speed = int(_num(a[5], -1))
        tt = _duration_s(a[6])
        pts = _points(a[0], a[1])
        if speed <= 0 or not tt or len(pts) < 2:
            continue    # "Not Available"
        # HOV and the Katy managed lanes (tagged ML, named "Managed Lanes") run
        # inside the mainlanes, so they'd always match alongside them.
        if a[8] != "ML" or _BYPASS_LANES_RE.search(a[11]):
            continue
        out.append(Segment("freeway", a[11], pts, speed, tt, _num(a[12]), int(_num(a[13]))))
    return out


def parse_streets(js: str) -> list[Segment]:
    out = []
    for m in _STREET_RE.finditer(js):
        a = _args(m.group(1))
        if len(a) < 14:
            continue
        speed, tt = int(_num(a[5], -1)), int(_num(a[6], -1))
        pts = _points(a[0], a[1])
        # isHist "y" = no live reading, TranStar filled in the historical speed.
        if speed <= 0 or tt <= 0 or a[12].lower() == "y" or len(pts) < 2:
            continue
        out.append(Segment("street", a[7], pts, speed, tt, _num(a[13]), None))
    return out


def parse_incidents(text: str) -> list[Incident]:
    out = []
    for r in json.loads(text).get("incidents", []):
        if (r.get("status") or "").lower() == "cleared":
            continue
        lat, lon = _num(r.get("lat"), None), _num(r.get("lng"), None)
        if lat is None or lon is None:
            continue
        out.append(Incident("freeway", r.get("location", ""), r.get("desc", ""),
                            r.get("lanes", ""), r.get("dir", ""), lat, lon))
    return out


def parse_hpd(text: str, now: datetime | None = None) -> list[Incident]:
    now = now or datetime.now()
    out = []
    for r in json.loads(text).get("incidents", []):
        if str(r.get("display", "True")).lower() != "true":
            continue
        m = re.search(r"today at (\d{1,2}):(\d{2})\s*([AP]M)", r.get("time", ""), re.I)
        if not m:
            continue
        h = int(m.group(1)) % 12 + (12 if m.group(3).upper() == "PM" else 0)
        age = (now - now.replace(hour=h, minute=int(m.group(2)), second=0)).total_seconds() / 60
        if not 0 <= age <= _HPD_MAX_AGE_MIN:
            continue
        lat, lon = _num(r.get("lat"), None), _num(r.get("lng"), None)
        if lat is None or lon is None:
            continue
        out.append(Incident("street", r.get("location", ""), r.get("desc", ""),
                            "", "", lat, lon))
    return out


# ── Geometry ─────────────────────────────────────────────────────────────────

_M_PER_DEG = 111_320


def _dist_m(a, b) -> float:
    dx = (b[1] - a[1]) * _M_PER_DEG * math.cos(math.radians(a[0]))
    dy = (b[0] - a[0]) * _M_PER_DEG
    return math.hypot(dx, dy)


def _heading(a, b) -> float:
    dx = (b[1] - a[1]) * math.cos(math.radians(a[0]))
    dy = b[0] - a[0]
    return math.degrees(math.atan2(dx, dy)) % 360


def _heading_diff(h1, h2) -> float:
    d = abs(h1 - h2) % 360
    return min(d, 360 - d)


def _densify(points, step_m):
    """Yield (lat, lon, heading) every ~step_m along a polyline."""
    for a, b in zip(points, points[1:]):
        d = _dist_m(a, b)
        if d == 0:
            continue
        h = _heading(a, b)
        n = max(1, int(d // step_m))
        for i in range(n):
            f = i / n
            yield (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, h)


_CELL = 0.001   # ~110 m; a 3x3 lookup covers the widest tolerance


class _RouteIndex:
    def __init__(self, points):
        self.grid: dict[tuple, list] = {}
        self.length_m = sum(_dist_m(a, b) for a, b in zip(points, points[1:]))
        for p in _densify(points, 20):
            self.grid.setdefault((int(p[0] // _CELL), int(p[1] // _CELL)), []).append(p)

    def near(self, lat, lon, tol_m, heading=None) -> tuple | None:
        """The closest route point within tol_m (and heading), or None."""
        ci, cj = int(lat // _CELL), int(lon // _CELL)
        best, best_d = None, tol_m
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for p in self.grid.get((ci + di, cj + dj), ()):
                    if heading is not None and _heading_diff(heading, p[2]) > _MAX_HEADING_DIFF:
                        continue
                    d = _dist_m((lat, lon), p)
                    if d <= best_d:
                        best, best_d = p, d
        return best


_DIR_HEADING = {"northbound": 0, "eastbound": 90, "southbound": 180, "westbound": 270}


def _coverage(seg: Segment, idx: _RouteIndex) -> float:
    samples = list(_densify(seg.points, 50))
    if not samples:
        return 0.0
    tol = _TOL_M[seg.kind]
    hit = sum(1 for la, lo, h in samples if idx.near(la, lo, tol, h))
    return hit / len(samples)


def match_route(points, segments, incidents) -> RouteTraffic:
    """TranStar's segments and incidents that lie along `points` [(lat, lon)]."""
    idx = _RouteIndex(points)
    on = []
    covered = 0.0
    for s in segments:
        # Cheap reject: neither end anywhere near the route.
        if not any(idx.near(la, lo, _TOL_M[s.kind] * 2) for la, lo in (s.points[0], s.points[-1],
                                                                       s.points[len(s.points) // 2])):
            continue
        c = _coverage(s, idx)
        if c >= _MIN_COVERAGE:
            on.append((s, c))
            covered += c * sum(_dist_m(a, b) for a, b in zip(s.points, s.points[1:]))
    hits = []
    for inc in incidents:
        p = idx.near(inc.lat, inc.lon, _INCIDENT_TOL_M[inc.kind])
        if p is None:
            continue
        want = _DIR_HEADING.get(inc.direction.lower())
        if want is not None and _heading_diff(want, p[2]) > 70:
            continue    # the other side of the freeway
        hits.append(inc)
    return RouteTraffic(on, hits, min(covered, idx.length_m))


# ── Spoken names ─────────────────────────────────────────────────────────────

_ROAD_WORDS = [
    (r"\bIH-610\b", "the 610"),
    (r"\bIH-(\d+)\b", r"I-\1"),
    (r"\bSH-6\b", "Highway 6"),
    (r"\bFM-(\d+)\b", r"FM \1"),
    (r"\b(?:US|SH)-(\d+)\b", r"\1"),
    (r"\bBW-?8\b", "Beltway 8"),
    (r"\bDr\b\.?", "Drive"), (r"\bSt\b\.?", "Street"), (r"\bRd\b\.?", "Road"),
    (r"\bBlvd\b\.?", "Boulevard"), (r"\bP(?:kwy|ky)\b\.?", "Parkway"), (r"\bExpy\b\.?", "Expressway"), (r"\bFwy\b\.?", "Freeway"),
    (r"\bLn\b\.?", "Lane"), (r"\bAve\b\.?", "Avenue"), (r"\bHwy\b\.?", "Highway"),
    (r"\bS\.?\s+(?=[A-Z])", "South "), (r"\bN\.?\s+(?=[A-Z])", "North "),
    (r"\bE\.?\s+(?=[A-Z])", "East "), (r"\bW\.?\s+(?=[A-Z])", "West "),
    (r"\s+N$", " North"), (r"\s+S$", " South"), (r"\s+E$", " East"), (r"\s+W$", " West"),
]


def spoken_road(text: str) -> str:
    for pat, rep in _ROAD_WORDS:
        text = re.sub(pat, rep, text)
    text = re.sub(r"\s*/\s*", " and ", text)
    text = re.sub(r"\bAt\b", "at", text)
    text = re.sub(r"\b(Before|After|Near)\b", lambda m: m.group(1).lower(), text)
    return " ".join(text.split())


def spoken_segment(seg: Segment) -> str:
    """'IH-10 Katy Eastbound from Silber to T.C. Jester' -> 'I-10 Katy
    eastbound from Silber to T.C. Jester'; arterials read as given."""
    name = re.sub(r"\b(North|South|East|West)bound\b", lambda m: m.group(0).lower(), seg.name)
    return spoken_road(name)


def spoken_incident(inc: Incident) -> str:
    """'there's a stalled truck on the 610 South Loop westbound at Kirby Drive'"""
    d = inc.desc.lower()
    if "accident" in d or "collision" in d:
        what = "a major accident" if "major" in d else "an accident"
    elif "fire" in d:
        what = "a vehicle fire"
    elif "high water" in d:
        what = "high water"
    elif "debris" in d or "lost load" in d:
        what = "debris on the road"
    elif "stall" in d:
        what = "a stalled truck" if "truck" in d else "a stalled car"
    elif "construction" in d or "closure" in d:
        what = "a lane closure"
    else:
        what = "an incident"
    where = inc.location
    if inc.kind == "street":
        # HPD: "4759 W FUQUA ST @ 14699 BUXLEY ST" -> "West Fuqua Street at Buxley Street"
        where = re.sub(r"\b\d+\s+", "", where).replace("@", "at").title()
        where = re.sub(r"\bAt\b", "at", where)
        where = re.sub(r"^(N|S|E|W)\s", lambda m: {"N": "North ", "S": "South ",
                                                   "E": "East ", "W": "West "}[m.group(1)], where)
    else:
        where = re.sub(r"\b(North|South|East|West)bound\b", lambda m: m.group(0).lower(), where)
    blocking = "lane" in inc.lanes.lower() and "shoulder" not in inc.lanes.lower()
    return f"{what} on {spoken_road(where)}" + (", blocking lanes" if blocking else "")


# ── Feed client ──────────────────────────────────────────────────────────────


class TranStar:
    def __init__(self, timeout=5, base=BASE):
        self._base = base
        self._timeout = timeout
        self._cache: dict[str, tuple[list, float]] = {}
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "Antigua-home-assistant/1.0"
        self._session.headers["Referer"] = f"{base}/layers/"

    def _get(self, name: str) -> list:
        now = time.time()
        with self._lock:
            hit = self._cache.get(name)
        if hit and now - hit[1] < _TTL:
            return hit[0]
        try:
            r = self._session.get(self._base + _FEEDS[name], params={"arg": int(now)},
                                  timeout=self._timeout)
            r.raise_for_status()
            text = r.text
            parsed = {"freeway": parse_freeway, "street": parse_streets,
                      "incidents": parse_incidents, "hpd": parse_hpd}[name](text)
        except Exception as e:
            log.warning("TranStar %s feed failed: %s", name, e)
            return hit[0] if hit and now - hit[1] < _TTL * 10 else []
        with self._lock:
            self._cache[name] = (parsed, now)
        return parsed

    def fetch_all(self) -> tuple[list, list]:
        """(segments, incidents), the four feeds fetched in parallel."""
        results = {}
        threads = [threading.Thread(target=lambda n=n: results.__setitem__(n, self._get(n)),
                                    daemon=True) for n in _FEEDS]
        for t in threads:
            t.start()
        for t in threads:
            t.join(self._timeout + 1)
        return (results.get("freeway", []) + results.get("street", []),
                results.get("incidents", []) + results.get("hpd", []))

    def along(self, points) -> RouteTraffic:
        segments, incidents = self.fetch_all()
        return match_route(points, segments, incidents)
