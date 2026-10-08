"""Podcast skill: "play Apple News Today" / "play Up First" plays that day's episode.

Shows come from the `podcasts:` block in server.yaml (DEFAULT_SHOWS if absent).
Each is either
  * an RSS feed (`feed:`), like NPR's Up First, or
  * an Apple-exclusive show with no feed (`apple_id:`), like Apple News Today.
    Its Apple Podcasts page embeds the recent episodes as JSON, each with a
    plain MP3.
Music Assistant plays the episode URL on the music speakers
(MusicControl.play_url). `url_pattern` and `max_minutes` keep only the daily
news episodes: Apple News Today's feed also carries interviews and a trailer,
and Up First's carries the half-hour Sunday Story and bonus interviews.

Which episode, with `days` the show's news days:
  * today's is out                    → play it
  * a news day, not out yet (4am)     → play the latest one, no question
  * not a news day (Apple News Today on a weekend, Up First on Sunday), or a
    named day with no episode (a holiday) → offer the latest one before it
    ("Want me to play Friday's?"); the pipeline keeps the offer for a minute
    and a yes plays it.

Replies are templates — the LLM is never involved.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests

from . import settings
from .music_intents import _speaker_suffix

log = logging.getLogger("antigua_core.podcast")

CACHE_TTL = 600

DEFAULT_SHOWS = {
    "apple_news_today": {
        "title": "Apple News Today",
        "names": ["apple news today", "apple news"],
        "apple_id": "1473872585",
        "url_pattern": r"/ANT-\d{8}(?:v\d+)?\.mp3$",
        "days": ["mon", "tue", "wed", "thu", "fri"],
    },
    "up_first": {
        "title": "Up First",
        "names": ["up first", "npr up first", "npr's up first", "up first from npr"],
        "feed": "https://feeds.npr.org/510318/podcast.xml",
        "max_minutes": 20,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat"],
    },
}

_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAY = r"today|yesterday|latest|newest|last|most\s+recent|" + "|".join(_DAYS)

_YES_RE = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|go\s+ahead|do\s+it|play\s+it|"
    r"sounds\s+good|why\s+not|of\s+course)\b", re.IGNORECASE)
_NO_RE = re.compile(
    r"^(?:no|nope|nah|never\s*mind|nevermind|don'?t|not\s+now|forget\s+it|no\s+thanks?)\b",
    re.IGNORECASE)


@dataclass
class Show:
    key: str
    title: str
    names: list[str]
    days: set[int]                  # weekday numbers with a news episode
    feed: str = ""
    apple_id: str = ""
    url_pattern: str = ""
    max_minutes: float = 0


@dataclass
class PodcastRequest:
    show: str = "apple_news_today"
    day: str | None = None        # None = today; "latest", "yesterday", "friday", …
    speaker: str | None = None    # key into settings.MUSIC_SPEAKERS


@dataclass
class Episode:
    day: date
    title: str
    url: str
    show: str = ""                # the show's spoken title
    published: datetime = field(default=datetime.min.replace(tzinfo=timezone.utc), repr=False)
    minutes: float | None = None


def shows() -> dict[str, Show]:
    out = {}
    for key, c in (settings.PODCASTS or DEFAULT_SHOWS).items():
        out[key] = Show(
            key, c["title"], [n.lower() for n in c.get("names") or [c["title"]]],
            {_DAYS.index(d) for d in _day_names(c.get("days"))},
            c.get("feed", ""), str(c.get("apple_id", "")), c.get("url_pattern", ""),
            float(c.get("max_minutes", 0)))
    return out


def _day_names(days) -> list[str]:
    days = days or ["mon", "tue", "wed", "thu", "fri"]
    return [next(d for d in _DAYS if d.startswith(x.lower()[:3])) for x in days]


def _clean(text: str) -> str:
    return re.sub(r"[\s,]+", " ", text.strip(" .?!,")).replace("’", "'")


_request_re: tuple = (None, None)


def _request_pattern():
    """The request regex for the configured show names (rebuilt if they change)."""
    global _request_re
    names = {n: s.key for s in shows().values() for n in s.names}
    if _request_re[0] != names:
        alt = "|".join(re.escape(n).replace(r"\ ", r"\s+")
                       for n in sorted(names, key=len, reverse=True))
        _request_re = (names, re.compile(
            r"^(?:(?:hey|ok|okay|so|and|um|uh|alright|all\s+right)\s+)?(?:(?:alexa|antigua)\s+)?"
            r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:go\s+ahead\s+and\s+|let'?s\s+)?"
            r"(?:play|put\s+on|start|listen\s+to|i\s+(?:want|'d\s+like|would\s+like)\s+to\s+(?:hear|listen\s+to))\s+"
            r"(?:me\s+)?(?:the\s+)?"
            rf"(?:(?P<day>{_DAY})(?:'?s)?\s+)?(?:(?:episode|show)\s+of\s+)?(?:the\s+)?"
            rf"(?P<show>{alt})(?:\s+(?:podcast|episode|show))?"
            rf"(?:\s+(?:from\s+|for\s+|on\s+)?(?P<day2>{_DAY}))?"
            r"(?:\s+please)?$",
            re.IGNORECASE))
    return _request_re


def parse_podcast(text: str) -> PodcastRequest | None:
    if not text:
        return None
    body, spk = _speaker_suffix(_clean(text))
    names, pattern = _request_pattern()
    m = pattern.match(body)
    if not m:
        return None
    show = names[re.sub(r"\s+", " ", m.group("show").lower())]
    day = re.sub(r"\s+", " ", (m.group("day") or m.group("day2") or "").lower())
    if day in ("", "today"):
        day = None
    elif day in ("newest", "last", "most recent"):
        day = "latest"
    return PodcastRequest(show, day, spk)


def answer(text: str) -> bool | None:
    """Reply to "Want me to play Friday's?": True, False, or None (not an answer)."""
    t = _clean(text or "")
    if _NO_RE.match(t):
        return False
    if _YES_RE.match(t):
        return True
    return None


# ── episodes ────────────────────────────────────────────────────────────────

def _local(dt: datetime) -> datetime:
    return dt.astimezone(ZoneInfo(settings.WEATHER_HOME_TZ))


def _minutes(duration) -> float | None:
    """itunes:duration is seconds ("781") or clock time ("24:09", "1:02:03")."""
    try:
        secs = 0.0
        for part in str(duration).split(":"):
            secs = secs * 60 + float(part)
        return secs / 60
    except ValueError:
        return None


def parse_apple_page(html: str, show: Show) -> list[Episode]:
    """Episodes of an Apple-exclusive show from its page's embedded JSON."""
    m = re.search(r'<script type="application/json" id="serialized-server-data">(.*?)</script>',
                  html, re.S)
    if not m:
        return []
    found = []

    def walk(o):
        if isinstance(o, dict):
            enc = o.get("mediaEnclosures")
            if o.get("showAdamId") == show.apple_id and enc and o.get("releaseDate"):
                pub = datetime.fromisoformat(o["releaseDate"].replace("Z", "+00:00"))
                dur = enc[0].get("duration")
                found.append(Episode(_local(pub).date(), (o.get("title") or "").strip(),
                                     enc[0].get("streamUrl") or "", show.title, pub,
                                     dur / 60 if dur else None))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(json.loads(m.group(1)))
    return found


def parse_feed(xml: bytes | str, show: Show) -> list[Episode]:
    found = []
    for e in feedparser.parse(xml).entries:
        url = next((x.get("href") for x in e.get("enclosures") or [] if x.get("href")), "")
        if not (url and e.get("published_parsed")):
            continue
        pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        found.append(Episode(_local(pub).date(), (e.get("title") or "").strip(), url,
                             show.title, pub, _minutes(e.get("itunes_duration", ""))))
    return found


def daily(eps: list[Episode], show: Show) -> list[Episode]:
    """The show's daily news episodes, one per day, newest first."""
    keep = [e for e in eps if e.url and e.day.weekday() in show.days
            and (not show.url_pattern or re.search(show.url_pattern, e.url))
            and (not show.max_minutes or e.minutes is None or e.minutes <= show.max_minutes)]
    # A re-cut episode shares the day; keep the newest.
    by_day = {e.day: e for e in sorted(keep, key=lambda e: e.published)}
    return sorted(by_day.values(), key=lambda e: e.day, reverse=True)


_cache: dict[str, tuple[float, list[Episode]]] = {}


def episodes(show: Show, fresh: bool = False) -> list[Episode]:
    at, eps = _cache.get(show.key, (0.0, []))
    if fresh or time.time() - at > CACHE_TTL or not eps:
        if show.apple_id:
            r = requests.get(f"https://podcasts.apple.com/us/podcast/id{show.apple_id}",
                             timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            eps = daily(parse_apple_page(r.content.decode("utf-8", "replace"), show), show)
        else:
            r = requests.get(show.feed, timeout=10, headers={"User-Agent": "Antigua/1.0"})
            r.raise_for_status()
            eps = daily(parse_feed(r.content, show), show)
        if not eps:
            raise ValueError(f"no {show.title} episodes found")
        _cache[show.key] = (time.time(), eps)
    return eps


def today() -> date:
    return datetime.now(ZoneInfo(settings.WEATHER_HOME_TZ)).date()


def _target(day: str | None, now: date) -> date | None:
    if day is None:
        return now
    if day == "latest":
        return None
    if day == "yesterday":
        return now - timedelta(days=1)
    return now - timedelta(days=(now.weekday() - _DAYS.index(day)) % 7)


def _label(d: date, now: date) -> str:
    if d == now:
        return "today's"
    if d == now - timedelta(days=1):
        return "yesterday's"
    return d.strftime("%A") + "'s"


@dataclass
class Choice:
    episode: Episode | None
    ask: bool           # offer it and wait for a yes instead of playing
    reply: str


def choose(show: Show, eps: list[Episode], req: PodcastRequest, now: date) -> Choice:
    t = show.title
    if not eps:
        return Choice(None, False, f"I couldn't find any {t} episodes.")
    want = _target(req.day, now)
    if want is None:
        return Choice(eps[0], False, "")
    hit = next((e for e in eps if e.day == want), None)
    if hit:
        return Choice(hit, False, "")
    before = next((e for e in eps if e.day < want), None)
    if want == now and now.weekday() in show.days and before:
        # A news day, not out yet (it drops early morning): the latest, no question.
        return Choice(before, False, f"Today's {t} isn't out yet, so here's {_label(before.day, now)}.")
    if not before:
        return Choice(None, False, f"I couldn't find {_label(want, now)} {t}.")
    if want.weekday() not in show.days:
        why = (f"{t} doesn't come out on weekends." if not {5, 6} & show.days and want.weekday() >= 5
               else f"{t} doesn't have a news episode on {want.strftime('%A')}s.")
    else:
        why = f"There's no {t} for {want.strftime('%A')}."
    return Choice(before, True, f"{why} Want me to play {_label(before.day, now)}?")


def pick(req: PodcastRequest, now: date) -> tuple[Show, Choice]:
    show = shows()[req.show]
    eps = episodes(show)
    if now.weekday() in show.days and eps[0].day < now and req.day in (None, "latest"):
        eps = episodes(show, fresh=True)   # it may have dropped since the cache filled
    return show, choose(show, eps, req, now)


def playing_reply(ep: Episode, now: date, where: str = "", lead: str = "") -> str:
    head = lead or f"Here's {_label(ep.day, now)} {ep.show}{where}."
    if lead and where:
        head = lead[:-1] + where + "."
    if not ep.title:
        return head
    return f"{head} {ep.title}" + ("" if ep.title[-1] in ".?!" else ".")
