"""Shared caches and fetchers: weather, NWS alerts, news, SearXNG search.

Moved verbatim from antigua_server.py (Phase 2, antigua_core extraction);
config globals now read from antigua_core.settings at call time.
"""

import json
import logging
import re
import time
import urllib.request
from datetime import datetime
from itertools import zip_longest
from threading import Lock, Thread

import feedparser
import requests

from . import settings

log = logging.getLogger("antigua_core")


def pin_year(query: str) -> str:
    """Append the current year unless the query already names one."""
    if re.search(r"\b(19|20)\d{2}\b", query):
        return query
    return f"{query} {datetime.now():%Y}"

# ── Weather Cache ─────────────────────────────────────────────────────────────


class WeatherCache:
    def __init__(self, default_location=None, ttl=None):
        # Resolved at call time — settings.configure() runs after import.
        self._default = default_location or settings.DEFAULT_WEATHER_LOCATION
        self._ttl = ttl if ttl is not None else settings.WEATHER_TTL
        self._cache = {}  # location -> (data, fetched_at)
        self._lock = Lock()

    def _fetch(self, location):
        url = f"https://wttr.in/{urllib.parse.quote(str(location))}?format=j1"
        with urllib.request.urlopen(url, timeout=6) as resp:
            return json.loads(resp.read())

    def _parse(self, raw, location):
        cur = raw["current_condition"][0]
        today = raw["weather"][0]

        # Sunrise / sunset
        astro = today.get("astronomy", [{}])[0]
        sunrise = astro.get("sunrise", "")
        sunset = astro.get("sunset", "")

        # Hourly time-of-day buckets (morning / afternoon / evening)
        hourly_summary = self._parse_hourly(today["hourly"])

        day_labels = ["Today", "Tomorrow", "Day after tomorrow"]
        forecast = []
        for i, w in enumerate(raw["weather"][:3]):
            mid = w["hourly"][len(w["hourly"]) // 2]
            forecast.append(
                {
                    "day": day_labels[i],
                    "high_f": w["maxtempF"],
                    "low_f": w["mintempF"],
                    "desc": mid["weatherDesc"][0]["value"],
                    "rain_chance": mid.get("chanceofrain", "0"),
                }
            )
        return {
            "location": location,
            "desc": cur["weatherDesc"][0]["value"],
            "temp_f": cur["temp_F"],
            "feels_f": cur["FeelsLikeF"],
            "humidity": cur["humidity"],
            "wind_mph": cur["windspeedMiles"],
            "uv": cur.get("uvIndex", "0"),
            "sunrise": sunrise,
            "sunset": sunset,
            "hourly_summary": hourly_summary,
            "forecast": forecast,
        }

    @staticmethod
    def _parse_hourly(slots):
        """Bucket hourly slots into morning / afternoon / evening summaries."""

        def _bucket(subset):
            if not subset:
                return None
            temps = [int(h["tempF"]) for h in subset]
            return {
                "temp_lo": min(temps),
                "temp_hi": max(temps),
                "rain_pct": max(int(h.get("chanceofrain", 0)) for h in subset),
                "thunder_pct": max(int(h.get("chanceofthunder", 0)) for h in subset),
                "uv": max(int(h.get("uvIndex", 0)) for h in subset),
            }

        morning = [h for h in slots if int(h["time"]) in (600, 900)]
        afternoon = [h for h in slots if int(h["time"]) in (1200, 1500)]
        evening = [h for h in slots if int(h["time"]) in (1800, 2100)]
        return {
            "morning": _bucket(morning),
            "afternoon": _bucket(afternoon),
            "evening": _bucket(evening),
        }

    def get(self, location=None):
        loc = str(location or self._default)
        now = time.time()
        with self._lock:
            if loc in self._cache:
                data, fetched_at = self._cache[loc]
                if now - fetched_at >= self._ttl:
                    # Stale — refresh in background, return stale data immediately
                    Thread(target=self._refresh, args=(loc,), daemon=True).start()
                return data
        # No cached data at all — fetch synchronously (first call only)
        return self._refresh(loc)

    _FAIL_TTL = 60  # retry failed fetches after 60s instead of blocking every call

    def _refresh(self, loc):
        try:
            raw = self._fetch(loc)
            data = self._parse(raw, loc)
            with self._lock:
                self._cache[loc] = (data, time.time())
            log.info(
                "Weather fetched for %s: %s %s°F", loc, data["desc"], data["temp_f"]
            )
            return data
        except Exception as e:
            log.warning("Weather fetch failed for %s: %s", loc, e)
            with self._lock:
                if loc in self._cache:
                    return self._cache[loc][0]
                # Cache None so we don't retry every request for 6s each time
                self._cache[loc] = (None, time.time() - self._ttl + self._FAIL_TTL)
            return None

    def format_for_prompt(self, location=None):
        data = self.get(location)
        if not data:
            return "Current weather: unavailable."
        c = data
        loc_label = f"location {c['location']}"

        # Current conditions line
        sun = ""
        if c.get("sunrise") and c.get("sunset"):
            sun = f" Sunrise {c['sunrise']}, sunset {c['sunset']}."
        lines = [
            f"Current weather ({loc_label}): {c['desc']}, {c['temp_f']}°F "
            f"(feels like {c['feels_f']}°F), humidity {c['humidity']}%, "
            f"wind {c['wind_mph']} mph, UV index {c.get('uv', '?')}.{sun}"
        ]

        # Hourly time-of-day breakdown for today
        hs = c.get("hourly_summary", {})
        period_parts = []
        for label, key in [
            ("Morning (6-11am)", "morning"),
            ("Afternoon (noon-5pm)", "afternoon"),
            ("Evening (6-9pm)", "evening"),
        ]:
            b = hs.get(key)
            if not b:
                continue
            t = (
                f"{b['temp_lo']}-{b['temp_hi']}°F"
                if b["temp_lo"] != b["temp_hi"]
                else f"{b['temp_hi']}°F"
            )
            rain = f"{b['rain_pct']}% rain"
            thunder = ", thunder possible" if b["thunder_pct"] >= 30 else ""
            uv = f", UV {b['uv']}" if b["uv"] > 0 else ""
            period_parts.append(f"{label}: {t}, {rain}{thunder}{uv}")
        if period_parts:
            lines.append("Today by time: " + ". ".join(period_parts) + ".")

        # 3-day forecast
        for f in c["forecast"]:
            lines.append(
                f"{f['day']}: {f['desc']}, high {f['high_f']}°F / low {f['low_f']}°F"
                f", {f['rain_chance']}% chance of rain."
            )
        return " ".join(lines)



# ── NWS Alerts Cache ──────────────────────────────────────────────────────────


class NWSAlertsCache:
    """Fetches active NWS weather alerts for a lat/lon from api.weather.gov."""

    _SEVERITY_ORDER = {
        "Extreme": 0,
        "Severe": 1,
        "Moderate": 2,
        "Minor": 3,
        "Unknown": 4,
    }

    def __init__(self, lat=None, lon=None, ttl=None):
        # Resolved at call time — settings.configure() runs after import.
        self._lat = lat if lat is not None else settings.NWS_LAT
        self._lon = lon if lon is not None else settings.NWS_LON
        self._ttl = ttl if ttl is not None else settings.NWS_ALERTS_TTL
        self._cache = None  # (alerts_list, fetched_at)
        self._lock = Lock()

    def _fetch(self):
        url = f"https://api.weather.gov/alerts/active?point={self._lat},{self._lon}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "Antigua/1.0 (local assistant)"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        alerts = []
        for f in data.get("features", []):
            p = f["properties"]
            alerts.append(
                {
                    "event": p.get("event", ""),
                    "severity": p.get("severity", "Unknown"),
                    "headline": p.get("headline", ""),
                }
            )
        # Sort by severity so most critical comes first
        alerts.sort(key=lambda a: self._SEVERITY_ORDER.get(a["severity"], 99))
        return alerts

    def get(self):
        now = time.time()
        with self._lock:
            if self._cache is not None:
                data, fetched_at = self._cache
                if now - fetched_at < self._ttl:
                    return data
                Thread(target=self._refresh, daemon=True).start()
                return data
        return self._refresh()

    _FAIL_TTL = 60  # retry failed fetches after 60s

    def _refresh(self):
        try:
            alerts = self._fetch()
            with self._lock:
                self._cache = (alerts, time.time())
            if alerts:
                log.info("NWS alerts: %d active (%s)", len(alerts), alerts[0]["event"])
            else:
                log.debug("NWS alerts: none active")
            return alerts
        except Exception as e:
            log.warning("NWS alerts fetch failed: %s", e)
            with self._lock:
                if self._cache is not None:
                    return self._cache[0]
                self._cache = ([], time.time() - self._ttl + self._FAIL_TTL)
            return []

    def format_for_prompt(self):
        alerts = self.get()
        if not alerts:
            return ""
        lines = ["Active NWS weather alerts:"]
        for a in alerts:
            lines.append(f"- [{a['severity']}] {a['event']}: {a['headline']}")
        return "\n".join(lines)



# ── News Cache ────────────────────────────────────────────────────────────────


class NewsCache:
    SOURCE_LABELS = {
        "aljazeera": "Al Jazeera",
        "bbc": "BBC",
        "guardian": "The Guardian",
        "reuters": "Reuters",
        "ap": "AP News",
        "npr": "NPR",
        "bbc_tech": "BBC Technology",
        "bbc_science": "BBC Science",
        "bbc_business": "BBC Business",
        "reuters_business": "Reuters Business",
    }

    def __init__(self, sources=None, ttl=None, max_items=None):
        # Resolved at call time — settings.configure() runs after import.
        sources = sources if sources is not None else settings.NEWS_SOURCES
        ttl = ttl if ttl is not None else settings.NEWS_TTL
        max_items = max_items if max_items is not None else settings.NEWS_MAX_ITEMS
        # A source is either a direct RSS `url`, or a `google_query` — some
        # wire services (Reuters, AP) retired their public RSS feeds entirely,
        # so those are proxied through a Google News RSS search instead.
        self._sources = {s["name"]: s["url"] for s in sources if "url" in s}
        self._google_sources = {s["name"]: s["google_query"] for s in sources if "google_query" in s}
        self._categories = {}  # category -> [source_names]
        # Sources with no category are the general "what's in the news" pool —
        # category feeds (tech/science/business) only surface when asked for.
        self._general_names = [s["name"] for s in sources if "category" not in s]
        for s in sources:
            if "category" in s:
                self._categories.setdefault(s["category"], []).append(s["name"])
        self._ttl = ttl
        self._max = max_items
        self._cache = {}  # source_name -> (items, fetched_at)
        self._lock = Lock()

    def _fetch(self, name, url):
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[: self._max]:
            title = entry.get("title", "").strip()
            # Strip source suffixes added by aggregators: " - Reuters", "&nbsp;&nbsp;Reuters"
            title = re.sub(
                r"\s*[-–]\s*(Reuters|Al Jazeera\w*|BBC News?|The Guardian|NPR|AP News?|Associated Press)\s*$",
                "",
                title,
                flags=re.IGNORECASE,
            )
            title = re.sub(
                r"(&nbsp;)+\s*(Reuters|Al Jazeera\w*|BBC News?|The Guardian|NPR|AP News?|Associated Press)\s*$",
                "",
                title,
                flags=re.IGNORECASE,
            )
            summary = entry.get("summary", entry.get("description", "")).strip()
            # Strip HTML tags and entities from summary
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            summary = (
                summary.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
            )
            # Strip trailing source name from summary (Google News format)
            summary = re.sub(
                r"\s+(Reuters|Al Jazeera\w*|BBC News?|The Guardian|NPR|AP News?|Associated Press)\s*$",
                "",
                summary,
                flags=re.IGNORECASE,
            ).strip()
            # Strip Guardian/BBC navigation fragments embedded in descriptions
            summary = re.sub(
                r"\s*Middle East crisis\s*[-–]\s*live updates\s*",
                " ",
                summary,
                flags=re.IGNORECASE,
            ).strip()
            summary = re.sub(
                r"\s*Sign up for the [^\.]+\.\s*", " ", summary, flags=re.IGNORECASE
            ).strip()
            summary = re.sub(
                r"\s*Continue reading\.\.\.\s*", " ", summary, flags=re.IGNORECASE
            ).strip()
            summary = re.sub(r"\s+", " ", summary).strip()
            if title:
                items.append(
                    {
                        "title": title,
                        "summary": summary[:500] if summary else "",
                        "source": name,
                        "published": entry.get("published", ""),
                    }
                )
        return items

    def _fetch_google_news_topic(self, topic):
        """Fetch a Google News RSS search for a specific topic."""
        query = urllib.parse.quote(topic)
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:10]:
            title = entry.get("title", "").strip()
            # Extract source from title suffix "Headline - Source"
            m = re.search(r"\s+[-–]\s+([^-–]+)$", title)
            source = "news"
            if m:
                source_raw = m.group(1).strip()
                title = title[: m.start()].strip()
                source_clean = (
                    source_raw.lower()
                    .replace(" ", "")
                    .replace("-", "")
                    .replace(".", "")
                )
                source_map = {
                    "theguardian": "guardian",
                    "bbcnews": "bbc",
                    "bbconline": "bbc",
                    "aljazeera": "aljazeera",
                    "reuters": "reuters",
                    "associatedpress": "ap",
                    "apnews": "ap",
                }
                source = source_map.get(source_clean, source_clean)
            if title:
                items.append(
                    {
                        "title": title,
                        "summary": "",
                        "source": source,
                        "published": entry.get("published", ""),
                    }
                )
        return items

    def _fetch_google_news_query(self, name, query):
        """Fetch a Google News RSS search for a known source (e.g. "site:reuters.com")
        — used where the source retired its own public RSS feed entirely."""
        q = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[: self._max]:
            title = entry.get("title", "").strip()
            # Google News titles are "Headline - Source" — strip the suffix;
            # the source is already known here, no need to parse it out.
            title = re.sub(r"\s+[-–]\s+[^-–]+$", "", title).strip()
            if title:
                items.append(
                    {"title": title, "summary": "", "source": name,
                     "published": entry.get("published", "")}
                )
        return items

    def _refresh(self, name):
        url = self._sources.get(name)
        google_query = self._google_sources.get(name)
        if not url and not google_query:
            return []
        try:
            items = self._fetch(name, url) if url else self._fetch_google_news_query(name, google_query)
            with self._lock:
                self._cache[name] = (items, time.time())
            log.info("News fetched for %s: %d items", name, len(items))
            return items
        except Exception as e:
            log.warning("News fetch failed for %s: %s", name, e)
            with self._lock:
                if name in self._cache:
                    return self._cache[name][0]
            return []

    def get(self, source=None):
        all_names = list(self._sources) + list(self._google_sources)
        names = [source] if source and source in all_names else self._general_names
        now = time.time()
        per_source = []
        for name in names:
            with self._lock:
                if name in self._cache:
                    items, fetched_at = self._cache[name]
                    if now - fetched_at >= self._ttl:
                        Thread(target=self._refresh, args=(name,), daemon=True).start()
                    per_source.append(items)
                    continue
            # No cache yet — fetch synchronously (first call only)
            per_source.append(self._refresh(name))
        if len(per_source) <= 1:
            return per_source[0] if per_source else []
        # Interleave round-robin across sources rather than concatenating —
        # otherwise "what's in the news" always reads as just the first
        # source's top 5, since format_for_prompt truncates to 5 total.
        results = []
        for group in zip_longest(*per_source):
            results.extend(item for item in group if item is not None)
        return results

    @staticmethod
    def _headline_key(title: str) -> str:
        words = re.sub(r"[^\w\s]", "", title.lower()).split()
        return " ".join(words[:6])

    def top_items(self, source_filter=None, topic_filter=None, category_filter=None):
        """The deduped, interleaved item list format_for_prompt() would speak —
        exposed separately so a caller (the news-follow-up guard) can stash the
        real items shown, not just the formatted sentence built from them."""
        # Category routing: pull only from category-specific sources,
        # interleaved the same way get() does for the general pool — otherwise
        # a multi-source category always reads as just its first source.
        if category_filter and category_filter in self._categories:
            per_source = [self.get(src) for src in self._categories[category_filter]]
            items = []
            for group in zip_longest(*per_source):
                items.extend(item for item in group if item is not None)
        else:
            items = self.get(source_filter)

        # Topic: broaden with Google News RSS, then keyword-filter
        if topic_filter:
            topic_items = self._fetch_google_news_topic(topic_filter)
            seen_titles = {i["title"].lower() for i in items}
            for ti in topic_items:
                if ti["title"].lower() not in seen_titles:
                    items.append(ti)
                    seen_titles.add(ti["title"].lower())
            kw = topic_filter.lower()
            items = [
                i
                for i in items
                if kw in i["title"].lower() or kw in i["summary"].lower()
            ]

        # Deduplicate by first-6-word fingerprint
        seen_keys: set = set()
        deduped = []
        for item in items:
            key = self._headline_key(item["title"])
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(item)
        return deduped[:5]

    def format_for_prompt(self, source_filter=None, topic_filter=None, category_filter=None):
        items = self.top_items(source_filter, topic_filter, category_filter)

        if not items:
            if topic_filter:
                return f"No headlines found about '{topic_filter}' from your news sources."
            if category_filter:
                return f"No {category_filter} headlines currently available."
            return "No headlines currently available."

        lines = ["Current news headlines:"]
        for n, item in enumerate(items, 1):
            src = self.SOURCE_LABELS.get(item["source"], item["source"].title())
            body = f" — {item['summary']}" if item["summary"] else ""
            lines.append(f"{n}. [{src}] {item['title']}{body}")
        return "\n".join(lines)



# ── SearXNG Web Search ────────────────────────────────────────────────────────


def parallel_mcp(tool: str, arguments: dict, timeout: float = 20) -> dict | None:
    """Call a tool (web_search, web_fetch) on Parallel's free public MCP
    endpoint: no key, no SLA. Its result JSON, or None on any failure."""
    try:
        r = requests.post(
            settings.PARALLEL_MCP_URL,
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": tool, "arguments": arguments}},
            timeout=timeout,
        )
        r.raise_for_status()
        reply = r.json()
        if "error" in reply or reply.get("result", {}).get("isError"):
            raise ValueError(f"MCP error: {str(reply)[:200]}")
        text = next(c["text"] for c in reply["result"]["content"] if c.get("type") == "text")
        return json.loads(text)
    except (requests.RequestException, ValueError, KeyError, StopIteration) as e:
        log.warning("Parallel %s failed: %s", tool, e)
        return None


class SearXNGSkill:
    """Queries a local SearXNG instance and formats top snippets as LLM context."""

    # Repeated identical queries skip the network. 15 min by default: long
    # enough to cover follow-ups inside one conversation, short enough that
    # "what's the news in X" doesn't go stale within a session.
    @property
    def _CACHE_TTL(self):
        # Read at call time — settings.configure() runs after import.
        return settings.SEARCH_CACHE_TTL

    def __init__(self):
        self._cache: dict = {}  # (query, engines) -> (result_tuple, timestamp)
        self._cache_lock = Lock()

    # Strip question framing to get a bare entity string for Wikipedia lookup.
    # (?:the\s+)? is placed OUTSIDE the main group so the required inner \s+
    # doesn't conflict with the optional "the " that follows it.
    _WIKI_STRIP_RE = re.compile(
        r"^\s*(?:who\s+(?:is|was|are|were)|what\s+(?:is|are|was|were)|"
        r"how\s+(?:tall|old|big|large|far|heavy|fast|much|many)\s+(?:is|are|was|were)|"
        r"when\s+(?:was|did|is|are)|where\s+(?:is|was|are)|"
        r"tell\s+me\s+about|give\s+me\s+(?:info|information)\s+(?:about|on))\s+(?:the\s+)?",
        re.IGNORECASE,
    )
    # Strip trailing verb phrases left by "when was X built/born/founded"
    _WIKI_TRAILING_RE = re.compile(
        r"\s+(?:built|born|founded|invented|created|released|made|opened|written|published|discovered)\s*$",
        re.IGNORECASE,
    )

    def _wiki_entity(self, query: str) -> str:
        entity = self._WIKI_STRIP_RE.sub("", query).strip().rstrip("?.,!")
        entity = self._WIKI_TRAILING_RE.sub("", entity).strip()
        return entity

    # Homepage boilerplate that ranks well but answers nothing ("Official match
    # schedule of Chicago Fire FC", "Find New York news and weather on NBC 4").
    _BOILERPLATE_RE = re.compile(
        r"^(?:official\s|find\s|get\s+the\s+latest|your\s+source\s+for|visit\s|"
        r"shop\s|discover\s|stay\s+up|read\s+real[- ]time|tickets\s+are)",
        re.IGNORECASE,
    )
    _DATEISH_RE = re.compile(
        r"\b(?:\d{1,2}\s+(?:hour|minute|day)s?\s+ago|yesterday|today|"
        r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
        re.IGNORECASE,
    )

    _RANK_STOPWORDS = {"the", "and", "for", "next", "today", "news", "score", "result"}

    def _rank(self, results: list, query: str) -> list:
        """Relevance first, then dated, then non-boilerplate.

        Date alone is a bad primary key: for "Chicago Fire schedule", a dated
        "32 Fun Things to Do in Chicago" listicle outranked the ESPN schedule
        page. Term overlap keeps the topical results on top, where the LLM
        reads them.
        """
        terms = {
            t for t in re.findall(r"[a-z0-9]{3,}", query.lower())
            if t not in self._RANK_STOPWORDS
        }

        def score(r):
            text = f"{r['title']} {r['content']}".lower()
            overlap = sum(t in text for t in terms)
            has_date = bool(r.get("published")) or bool(self._DATEISH_RE.search(r["content"]))
            boiler = bool(self._BOILERPLATE_RE.search(r["content"]))
            return (-overlap, not has_date, boiler)  # more overlap sorts first

        return sorted(results, key=score)

    def search(self, query: str, engines: str | None = None, count: int = 0,
               wiki: bool = True, categories: str = "general") -> tuple:
        """Returns (direct_answer, results).

        Fires two parallel requests: Wikipedia (entity query) for an infobox
        answer, and the configured engines for ranked snippets. Pass engines
        explicitly to override settings.SEARCH_ENGINES (e.g. news engines for temporal
        queries), count to widen the snippet window, and wiki=False for live
        questions where an encyclopedia entry is never the answer.
        Results are cached for _CACHE_TTL seconds per (query, engines).
        direct_answer is a clean prose string when available, otherwise None.
        results is a list of {title, content, published} dicts used as fallback.
        """
        from concurrent.futures import ThreadPoolExecutor

        eff_engines = engines or settings.SEARCH_ENGINES
        cache_key = (query.lower().strip(), eff_engines, count, wiki, categories)
        now = time.time()
        with self._cache_lock:
            if cache_key in self._cache:
                result, ts = self._cache[cache_key]
                if now - ts < self._CACHE_TTL:
                    return result

        base = {"format": "json", "language": "en-US"}

        def _fetch(q, eng, cats="general"):
            r = requests.get(
                f"{settings.SEARCH_URL}/search",
                params={"q": q, "engines": eng, "categories": cats, **base},
                timeout=settings.SEARCH_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()

        wiki_data = {}
        try:
            if wiki:
                wiki_query = self._wiki_entity(query)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    f_wiki = pool.submit(_fetch, wiki_query, "wikipedia")
                    f_snippets = pool.submit(_fetch, query, eff_engines, categories)
                    wiki_data = f_wiki.result()
                    snippet_data = f_snippets.result()
            else:
                snippet_data = _fetch(query, eff_engines, categories)
        except requests.RequestException as e:
            if not self._parallel_on():
                raise
            log.warning("SearXNG failed (%s); trying Parallel", e)
            snippet_data = {}

        for ib in wiki_data.get("infoboxes", []):
            text = ib.get("content", "").strip()
            if text:
                result = (text, [])
                with self._cache_lock:
                    self._cache[cache_key] = (result, now)
                return result

        limit = count or settings.SEARCH_RESULT_COUNT
        results = []
        # Rank over a wider slice than we keep, so a dated headline sitting at
        # rank 8 can still displace boilerplate sitting at rank 1.
        for r in snippet_data.get("results", [])[: max(limit * 3, 10)]:
            title = r.get("title", "").strip()
            content = r.get("content", "").strip()
            if title and content:
                # publishedDate matters for "next game" / "who won" questions —
                # without it the LLM can't tell a 2025 fixture from tomorrow's.
                published = (r.get("publishedDate") or "").strip()
                results.append(
                    {"title": title, "content": content[:400], "published": published[:10]}
                )
        if not results and self._parallel_on():
            results = self._parallel(query, limit)
        results = self._rank(results, query)[:limit]
        result = (None, results)
        with self._cache_lock:
            self._cache[cache_key] = (result, now)
        return result

    @staticmethod
    def _parallel_on() -> bool:
        return bool(settings.PARALLEL_API_KEY or settings.PARALLEL_KEYLESS)

    def _parallel(self, query: str, limit: int) -> list:
        """SearXNG came back empty (its engines rate-limited or blocked):
        the same {title, content, published} rows from Parallel's Search
        API with a key, else its public MCP endpoint, whose web_search tool
        returns the same JSON as text. [] on any failure, so a search never
        dies here."""
        search = {"objective": query, "search_queries": [query]}
        try:
            if settings.PARALLEL_API_KEY:
                r = requests.post(
                    settings.PARALLEL_URL,
                    headers={"x-api-key": settings.PARALLEL_API_KEY},
                    json={**search, "mode": settings.PARALLEL_MODE, "advanced_settings": {
                        "max_results": max(limit * 3, 10),
                        "excerpt_settings": {"max_chars_per_result": 800},
                    }},
                    timeout=settings.SEARCH_TIMEOUT + 4,
                )
                r.raise_for_status()
                data = r.json()
            else:
                data = parallel_mcp("web_search", search, timeout=settings.SEARCH_TIMEOUT + 8)
                if data is None:
                    return []
            rows = data.get("results") or []
        except (requests.RequestException, ValueError) as e:
            log.warning("Parallel search failed: %s", e)
            return []
        out = []
        for row in rows:
            title = (row.get("title") or "").strip()
            content = " ".join(row.get("excerpts") or []).strip()
            if title and content:
                out.append({"title": title, "content": content[:400],
                            "published": (row.get("publish_date") or "")[:10]})
        log.info("Search: SearXNG empty for %r; Parallel gave %d results", query, len(out))
        return out

    def _thin(self, answer, results: list) -> bool:
        """No answer worth showing the LLM: nothing back, or all boilerplate."""
        if answer:
            return False
        if not results:
            return True
        return all(self._BOILERPLATE_RE.search(r["content"]) for r in results)

    def search_best(self, query: str, engines=None, count: int = 0, wiki: bool = True,
                    categories: str = "general", fresh: bool = False) -> tuple:
        """search(), with one retry when the first attempt comes back thin.

        Doing this by hand is what turned the World Cup answer from invented to
        correct: the first query returned cricket scores and a 2034 bid story,
        the year-pinned retry returned the actual final. Returns
        (direct_answer, results, query_used).
        """
        answer, results = self.search(query, engines=engines, count=count,
                                      wiki=wiki, categories=categories)
        if not self._thin(answer, results):
            return answer, results, query

        # Change something real on the retry: pin the year if it wasn't pinned,
        # otherwise swap engine pools. Repeating the same query is free but
        # pointless — the cache would just hand back the same results.
        retry_query = query if fresh else pin_year(query)
        retry_engines = settings.SEARCH_ENGINES if engines == settings.SEARCH_NEWS_ENGINES else settings.SEARCH_NEWS_ENGINES
        retry_categories = "general" if categories == "news" else "news"
        if retry_query == query and retry_engines == (engines or settings.SEARCH_ENGINES):
            return answer, results, query

        log.info("Search thin, retrying: %r -> %r (%s)", query, retry_query, retry_engines)
        try:
            r_answer, r_results = self.search(retry_query, engines=retry_engines,
                                              count=count, wiki=wiki,
                                              categories=retry_categories)
        except Exception as e:
            log.warning("Search retry failed: %s", e)
            return answer, results, query
        if self._thin(r_answer, r_results):
            return answer, results, query  # keep the first attempt's crumbs
        return r_answer, r_results, retry_query

    def format_substitution_prompt(self, ingredient: str, results: list) -> str:
        if not results:
            return ""
        lines = [f'Ingredient substitution results for "{ingredient}":']
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}: {r['content']}")
        lines.append(
            "Using only the above, give a spoken answer in 1-2 sentences. "
            "Include the best substitute, the ratio if different from 1:1, and any important "
            "texture or flavor trade-off. Do not list URLs."
        )
        return "\n".join(lines)

    def format_for_prompt(self, query: str, direct_answer: str | None, results: list,
                          live: bool = False) -> str:
        if direct_answer:
            return (
                f'Direct answer for "{query}": {direct_answer}\n'
                "Summarise the above in one natural spoken sentence."
            )
        if not results:
            return ""
        today = datetime.now().strftime("%A, %B %-d, %Y")
        lines = [f"Today is {today}.", f'Web search results for "{query}":']
        for i, r in enumerate(results, 1):
            stamp = f" ({r['published']})" if r.get("published") else ""
            lines.append(f"{i}.{stamp} {r['title']}: {r['content']}")
        # The strict wording applies to every search answer, not just live ones.
        # Asked "who won the World Cup" the model answered from weights with an
        # invented year, opponent, scorer and stadium — then said in the next
        # breath that it had no results for this year. Any question worth
        # searching for is a question whose answer must come from the results.
        lines.append(
            "Answer in 1-2 spoken sentences using ONLY the text above. "
            "Never state a name, date, year, time, venue, score or number that does "
            "not appear in the text above — not even one you believe is correct. "
            "If the text above does not contain the answer, say you couldn't find it "
            "and stop; do not fall back on what you remember. "
            "Do not list URLs or source names unless the user asked."
        )
        if live:
            lines.append(
                "Snippets for schedules and results are often landing pages rather "
                "than the fixture itself — if so, say you couldn't find the date."
            )
        return "\n".join(lines)


