"""Research: questions worth more than one search. "Find the best price on a
Ninja Creami", "how have Houston's summers changed", "is the Pixel worth it".

User decision 2026-10-31: it all runs on the local 4B, and nothing but the
searches leaves the house. The 4B is too small to drive a free tool loop, and
too slow to read whole pages (the backup's 4 GB GPU prefills ~174 tok/s), so
the code does the legwork and the model makes small calls:

1. plan: what kind of question it is and 2-4 keyword queries;
2. answer from the evidence the code gathered, or name ONE more search it
   needs (one extra round at most), then answer.

The evidence, by kind:
- every kind: Parallel's search. One call takes all the queries, and its
  excerpts are cut to the objective, which keeps the prompt small. SearXNG
  only when Parallel fails: its Google engine is rate-limited per home IP,
  and the quick turns need it.
- price: retailer pages read through Parallel's web_fetch, and the dollar
  amounts in everything found, listed by store.
- climate: the nearest NOAA station's daily record (ACIS) from 1991 to last
  year, summed up by decade in code. No climate number comes from the model.

The answer is held to the evidence like any search answer: a sentence with
a name or number the evidence lacks is dropped (grounding.unsupported_claims).
The pipeline says one line, runs this in the background and speaks the answer
when it's ready (user decision 2026-10-31).
"""

import json
import logging
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urlparse

import requests

from . import settings
from .caches import parallel_mcp
from .grounding import unsupported_claims
from .intents.research import parse  # noqa: F401 — re-exported

log = logging.getLogger("antigua_core")

_MAX_RESULTS = 8                # search rows shown to the model
_EXCERPT_CHARS = 600            # of each row
_EVIDENCE_CHARS = 6000          # ~1.5k tokens: ~9 s of prefill on the fallback
_RUNS_PER_HOUR = 12             # each run is 1-3 searches; spare the search engines

# ── what asks for research ──────────────────────────────────────────────────

def ack(question: str) -> str:
    """The one line said before the work starts."""
    if re.search(r"\b(?:price|deal|cheap|buy)\b", question, re.I):
        return "I'll shop around and get back to you."
    return "I'll look into that and get back to you."


_runs: list[float] = []


def allowed() -> bool:
    """Under the hourly cap (_RUNS_PER_HOUR); counts this run when it is."""
    now = time.time()
    _runs[:] = [t for t in _runs if now - t < 3600]
    if len(_runs) >= _RUNS_PER_HOUR:
        return False
    _runs.append(now)
    return True


# ── the model's calls ───────────────────────────────────────────────────────

_PLAN = """Today is {today}. The user lives in {home}.
Plan web research for a home voice assistant. Reply with JSON only:
{{"kind": "price" | "climate" | "compare" | "general",
  "subject": "the product, place or topic in a few words",
  "place": "the city the question is about, or empty",
  "queries": [2 to 4 short keyword web queries]}}
kind is "price" for where to buy or what something costs, "climate" for \
weather patterns, trends or normals over years, "compare" for choosing \
between options. Queries: plain keywords, no quotes, keep the user's own \
names and model numbers, and no year unless the user said one.
Question: {q}"""

_ANSWER = """Today is {today}. You are Antigua, a home voice assistant in \
{home}. You researched this question: "{q}"

{evidence}

Reply with JSON only:
{{"answer": "3 to 5 short spoken sentences", "sources": ["site names you used"], \
"need": ""}}

Rules for the answer:
- Use ONLY the evidence above. Never state a price, number, date, name or \
store that is not written there.
- Lead with the direct answer, then the one or two facts that back it.
{rules}- Say where the facts come from in plain words ("Walmart lists it at..."). \
No URLs, no lists, no markdown.
- If the evidence is thin or conflicting, say so plainly.
{need_rule}"""

_NEED_RULE = ('- If one more web search would clearly answer the question better, put a short '
              'keyword query in "need" and leave "answer" empty. Otherwise "need" is "".')

_KIND_RULES = {
    "price": ("- Name the lowest price you found, the store, and the exact model it is for. "
              "Models and bundles differ (Deluxe, 2-pack, refurbished): do not compare "
              "different models as if they were the same.\n"
              "- Prices change; say \"right now\" or \"as of today\" once.\n"),
    "climate": ("- Say how things have changed: compare the early decade with the latest one, "
                "with the numbers from the climate record. Describe each change the way its "
                "numbers move: a number that went down is less, drier or cooler, never more.\n"
                "- Climate record numbers are averages of real daily measurements.\n"),
    "compare": "- End with which one fits better and for whom, from the evidence.\n",
}


def _json(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        m = re.search(r"\{.*\}", raw or "", re.S)
        try:
            data = json.loads(m.group(0)) if m else {}
        except ValueError:
            data = {}
    return data if isinstance(data, dict) else {}


def _today() -> str:
    return datetime.now().strftime("%A, %B %-d, %Y")


def _home() -> str:
    return settings.SEARCH_HOME_CITY or "the United States"


def plan(question: str, llm) -> dict:
    """{kind, subject, place, queries} for `question`; a plain search of the
    question itself when the model's plan doesn't parse."""
    p = _json(llm(_PLAN.format(today=_today(), home=_home(), q=question)))
    kind = p.get("kind") if p.get("kind") in ("price", "climate", "compare", "general") else "general"
    queries = []
    for q in p.get("queries") or []:
        q = " ".join(re.sub(r"[\"'+*|<>]", " ", str(q)).split())
        if q and q.lower() not in (x.lower() for x in queries):
            queries.append(q)
    return {"kind": kind, "subject": str(p.get("subject") or question)[:80],
            "place": str(p.get("place") or "").strip(),
            "queries": queries[:4] or [question]}


# ── the tools ───────────────────────────────────────────────────────────────

@dataclass
class Source:
    url: str
    title: str
    text: str
    published: str = ""

    @property
    def site(self) -> str:
        host = urlparse(self.url).hostname or ""
        return host.removeprefix("www.")


def web_search(objective: str, queries: list[str]) -> list[Source]:
    """Parallel's search (all queries in one call, excerpts cut to the
    objective), else SearXNG for the first query only."""
    data = parallel_mcp("web_search", {"objective": objective, "search_queries": queries},
                        timeout=settings.SEARCH_TIMEOUT + 10)
    out = []
    for r in (data or {}).get("results") or []:
        text = " ".join(r.get("excerpts") or []).strip()
        if r.get("url") and text:
            out.append(Source(r["url"], (r.get("title") or "").strip(), text,
                              (r.get("publish_date") or "")[:10]))
    if out:
        return out
    log.info("Research: Parallel search empty; SearXNG for %r", queries[0])
    try:
        resp = requests.get(f"{settings.SEARCH_URL}/search", timeout=settings.SEARCH_TIMEOUT, params={
            "q": queries[0], "format": "json", "engines": settings.SEARCH_ENGINES,
            "categories": "general", "language": "en-US"})
        resp.raise_for_status()
        rows = resp.json().get("results", [])
    except (requests.RequestException, ValueError) as e:
        log.warning("Research: SearXNG failed: %s", e)
        return []
    return [Source(r["url"], r.get("title", ""), r.get("content", ""),
                   (r.get("publishedDate") or "")[:10])
            for r in rows if r.get("url") and r.get("content")]


_RETAIL_RE = re.compile(r"(?:^|\.)(?:amazon|walmart|target|bestbuy|costco|samsclub|kohls|"
                        r"macys|homedepot|lowes|wayfair|newegg|bhphotovideo|heb|"
                        r"crateandbarrel|williams-sonoma|bedbathandbeyond|qvc|ebay)\.com$")
_PRICE_RE = re.compile(r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\$\s?\d+(?:\.\d{2})?")


def read_pages(urls: list[str], objective: str) -> dict[str, str]:
    """{url: text} for `urls` through Parallel's web_fetch, whose excerpts
    keep only what the objective asks for. Retailers refuse a plain fetch."""
    if not urls:
        return {}
    data = parallel_mcp("web_fetch", {"urls": urls, "objective": objective}, timeout=40)
    return {r["url"]: " ".join(r.get("excerpts") or [])
            for r in (data or {}).get("results") or [] if r.get("url") and r.get("excerpts")}


def price_mentions(sources: list[Source]) -> list[str]:
    """'walmart.com: $179.00 (Ninja CREAMi NC301 ...)' lines: each dollar
    amount with the words around it, which say what it is the price of."""
    lines = []
    for s in sources:
        for m in _PRICE_RE.finditer(s.text):
            ctx = s.text[max(0, m.start() - 90): m.end() + 40]
            ctx = " ".join(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", ctx).split())
            line = f"{s.site}: {m.group(0)} ({ctx})"
            if line not in lines:
                lines.append(line)
            if sum(x.startswith(s.site) for x in lines) >= 3:
                break
    return lines[:16]


def _geocode(place: str) -> tuple[float, float, str] | None:
    try:
        r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": place.split(",")[0], "count": 1}, timeout=6)
        r.raise_for_status()
        hit = (r.json().get("results") or [None])[0]
    except (requests.RequestException, ValueError) as e:
        log.warning("Research: geocode %r failed: %s", place, e)
        return None
    if not hit:
        return None
    return hit["latitude"], hit["longitude"], hit["name"]


def _is_home(place: str) -> bool:
    home = settings.SEARCH_HOME_CITY.split(",")[0].strip().lower()
    return not place or (bool(home) and home in place.lower())


_ACIS = "https://data.rcc-acis.org"


def _station(lat: float, lon: float, first: str, last: str) -> dict | None:
    """The NOAA station nearest (lat, lon) whose daily record runs from
    `first` to `last`, an airport first: {"name", "sid"}."""
    r = requests.post(f"{_ACIS}/StnMeta", timeout=10, json={
        "bbox": f"{lon - 0.4:.3f},{lat - 0.4:.3f},{lon + 0.4:.3f},{lat + 0.4:.3f}",
        "elems": "maxt,mint,pcpn", "meta": "name,sids,valid_daterange,ll"})
    r.raise_for_status()
    best = None
    for s in r.json().get("meta") or []:
        spans = s.get("valid_daterange") or []
        if not s.get("ll") or len(spans) < 3 or not all(
                sp and sp[0] <= first and sp[1] >= last for sp in spans):
            continue
        dist = (s["ll"][0] - lon) ** 2 + (s["ll"][1] - lat) ** 2
        key = (" AP" not in s["name"] and "AIRPORT" not in s["name"], dist)
        if best is None or key < best[0]:
            best = (key, {"name": s["name"].title().replace(" Ap", " Airport"),
                          "sid": s["sids"][0].split()[0]})
    return best[1] if best else None


def _num(v) -> float | None:
    """An ACIS value: "T" (trace) is 0, "M" (missing) is None."""
    if v == "T":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def climate_record(place: str) -> str:
    """Decade-by-decade figures for `place` (home when empty) from the NOAA
    station record (ACIS), 1991 to last year, or "" when it fails. Measured
    daily data: Open-Meteo's ERA5 grid was tried first and put Houston's 2021
    rain at 100 inches against the gauge's 52."""
    if _is_home(place):
        lat, lon = settings.WEATHER_HOME_LAT, settings.WEATHER_HOME_LON
    else:
        found = _geocode(place)
        if not found:
            return ""
        lat, lon, _name = found
    last = date.today().year - 1
    first_day, last_day = "1991-01-01", f"{last}-12-31"
    try:
        stn = _station(lat, lon, first_day, last_day)
        if stn is None:
            log.info("Research: no long NOAA record near %r", place)
            return ""
        r = requests.post(f"{_ACIS}/StnData", timeout=20, json={
            "sid": stn["sid"], "sdate": first_day, "edate": last_day,
            "elems": ["maxt", "mint", "pcpn"]})
        r.raise_for_status()
        rows = r.json()["data"]
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning("Research: NOAA record for %r failed: %s", place, e)
        return ""
    days = [row[0] for row in rows]
    highs, lows, rain = ([_num(row[i]) for row in rows] for i in (1, 2, 3))
    return summarize_climate(f"{stn['name']} (NOAA station records)", days, highs, lows, rain)


def summarize_climate(name: str, days, highs, lows, rain) -> str:
    """The climate record as text: per decade, summer highs, hot days, warm
    nights, freezes and rain, plus this month's normals."""
    by_decade: dict[int, dict] = {}
    month_now = date.today().month
    month_hi, month_lo, month_rain = [], [], {}
    for day, hi, lo, pr in zip(days, highs, lows, rain):
        if hi is None or lo is None:
            continue
        y, m = int(day[:4]), int(day[5:7])
        dec = by_decade.setdefault(y // 10 * 10, {"years": set(), "summer": [], "hot95": 0,
                                                   "hot100": 0, "warm_nights": 0, "freezes": 0,
                                                   "rain": 0.0, "downpours": 0})
        dec["years"].add(y)
        if m in (6, 7, 8):
            dec["summer"].append(hi)
        dec["hot95"] += hi >= 95
        dec["hot100"] += hi >= 100
        dec["warm_nights"] += lo >= 78
        dec["freezes"] += lo <= 32
        dec["rain"] += pr or 0
        dec["downpours"] += (pr or 0) >= 2
        if m == month_now:
            month_hi.append(hi)
            month_lo.append(lo)
            month_rain[y] = month_rain.get(y, 0) + (pr or 0)
    lines = [f"Climate record for {name}, measured daily {days[0][:4]} to {days[-1][:4]}, "
             "yearly averages by decade:"]
    for dec in sorted(by_decade):
        v = by_decade[dec]
        n = len(v["years"])
        span = f"{min(v['years'])}-{max(v['years'])}"
        lines.append(
            f"- {dec}s ({span}): summer average high {statistics.mean(v['summer']):.1f}°F; "
            f"{v['hot95'] / n:.0f} days a year at 95°F or hotter, {v['hot100'] / n:.0f} at 100°F "
            f"or hotter; {v['warm_nights'] / n:.0f} nights a year that stayed 78°F or warmer; "
            f"{v['freezes'] / n:.0f} freezing nights a year; {v['rain'] / n:.1f} inches of rain "
            f"a year, {v['downpours'] / n:.1f} days a year with 2 inches or more.")
    full = [d for d in sorted(by_decade) if len(by_decade[d]["years"]) >= 3]
    if len(full) >= 2:
        # Spelled out, words and all: given only the two numbers, the 4B
        # still called Houston "hotter and wetter" with rain down 11 inches.
        a, b = by_decade[full[0]], by_decade[full[-1]]
        per = lambda v, k: v[k] / len(v["years"])  # noqa: E731
        changes = []
        for label, va, vb, unit, up, down in (
                ("summer average high", statistics.mean(a["summer"]), statistics.mean(b["summer"]),
                 "°F", "hotter", "cooler"),
                ("days at 95°F or hotter", per(a, "hot95"), per(b, "hot95"), " days", "more", "fewer"),
                ("warm nights", per(a, "warm_nights"), per(b, "warm_nights"), " nights", "more", "fewer"),
                ("freezing nights", per(a, "freezes"), per(b, "freezes"), " nights", "more", "fewer"),
                ("yearly rain", per(a, "rain"), per(b, "rain"), " inches", "wetter", "drier")):
            diff = vb - va
            word = up if diff > 0 else down if diff < 0 else "no change"
            changes.append(f"{label} {diff:+.1f}{unit} ({word})")
        lines.append(f"Change from the {full[0]}s to the {full[-1]}s: " + "; ".join(changes) + ".")
    if month_hi:
        month = date(2000, month_now, 1).strftime("%B")
        lines.append(f"Typical {month}: average high {statistics.mean(month_hi):.0f}°F, average "
                     f"low {statistics.mean(month_lo):.0f}°F, "
                     f"{statistics.mean(month_rain.values()):.1f} inches of rain.")
    return "\n".join(lines)


# ── putting it together ─────────────────────────────────────────────────────

@dataclass
class Result:
    question: str
    answer: str
    kind: str = "general"
    sources: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)   # for the log
    seconds: float = 0.0


def _evidence(kind: str, sources: list[Source], extra: str) -> str:
    """The prompt's evidence block, cut to _EVIDENCE_CHARS: the computed
    part (climate record, price list) first, then the search rows."""
    parts = [extra] if extra else []
    if sources:
        rows = ["Web results:"]
        for i, s in enumerate(sources[:_MAX_RESULTS], 1):
            stamp = f" ({s.published})" if s.published else ""
            rows.append(f"{i}. [{s.site}]{stamp} {s.title}: {' '.join(s.text.split())[:_EXCERPT_CHARS]}")
        parts.append("\n".join(rows))
    return "\n\n".join(parts)[:_EVIDENCE_CHARS] or "No evidence was found."


def _price_block(sources: list[Source]) -> str:
    prices = price_mentions(sources)
    return ("Prices found (store: price (words around it)):\n" + "\n".join(prices)
            if prices else "No prices were found on the pages.")


def _objective(kind: str, question: str, subject: str) -> str:
    if kind == "price":
        return (f"Current prices for {subject} at US stores, with the store name and exact "
                f"model or bundle for each price. Question: {question}")
    return f"Facts and figures that answer: {question}"


def _dollars(price: str) -> float:
    return float(re.sub(r"[$,\s]", "", price))


def _grounded(answer: str, evidence: str) -> str:
    """`answer` without the sentences that state a name or number the
    evidence lacks (the model's memory, not the research)."""
    prices = {_dollars(m) for m in _PRICE_RE.findall(evidence)}
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", answer.strip()):
        # A sentence's first word is capitalized whatever it is ("Right now,
        # ..."); a name there still shows up again where it's checked.
        bad = unsupported_claims(sentence[:1].lower() + sentence[1:], evidence)
        # unsupported_claims matches numbers as substrings: "$99" passes on
        # "$159.99". A price has to be one of the evidence's own.
        bad += [m for m in _PRICE_RE.findall(sentence) if _dollars(m) not in prices]
        if bad:
            log.info("Research: dropped %r (unsupported %s)", sentence, bad)
        elif sentence:
            kept.append(sentence)
    return " ".join(kept)


def run(question: str, llm) -> Result:
    """Plan, gather, answer (with at most one more search). Never raises:
    a failure is an answer that says so."""
    t0 = time.time()
    steps = []
    try:
        p = plan(question, llm)
        kind, subject = p["kind"], p["subject"]
        steps.append(f"plan {kind}: {p['queries']}")
        with ThreadPoolExecutor(2) as pool:
            f_search = pool.submit(web_search, _objective(kind, question, subject), p["queries"])
            f_climate = pool.submit(climate_record, p["place"]) if kind == "climate" else None
            sources = f_search.result()
            extra = f_climate.result() if f_climate else ""
        steps.append(f"search: {len(sources)} results")
        if kind == "price":
            shops = [s.url for s in sources if _RETAIL_RE.search(urlparse(s.url).hostname or "")]
            pages = read_pages(shops[:4], f"Current price of {subject} and the exact model")
            for s in sources:
                if pages.get(s.url):
                    s.text = f"{pages[s.url]} {s.text}"
            steps.append(f"read {len(pages)} store pages")
            extra = _price_block(sources)
        evidence = _evidence(kind, sources, extra)
        for round_ in (1, 2):
            reply = _json(llm(_ANSWER.format(
                today=_today(), home=_home(), q=question, evidence=evidence,
                rules=_KIND_RULES.get(kind, ""), need_rule=_NEED_RULE if round_ == 1 else "")))
            need = " ".join(str(reply.get("need") or "").split())
            if round_ == 1 and need and not reply.get("answer"):
                more = web_search(_objective(kind, question, subject), [need])
                steps.append(f"more {need!r}: {len(more)} results")
                seen = {s.url for s in sources}
                sources = [s for s in more if s.url not in seen][:4] + sources
                if kind == "price":
                    extra = _price_block(sources)
                evidence = _evidence(kind, sources, extra)
                continue
            break
        answer = _grounded(str(reply.get("answer") or ""), evidence)
        if not answer:
            answer = (f"I looked into {subject}, but I couldn't find anything solid enough "
                      "to tell you.")
        names = reply.get("sources") or []
        names = [names] if isinstance(names, str) else [str(s) for s in names if s]
        return Result(question, answer, kind, names[:4], steps, time.time() - t0)
    except Exception:
        log.exception("Research failed for %r", question)
        return Result(question, "Sorry, my research on that fell through.", steps=steps,
                      seconds=time.time() - t0)
