"""Ranking and cleanup for SearXNG image results shown on the display.

SearXNG interleaves its image engines round-robin instead of ranking them,
so result B is whatever the second engine's first hit was: for a local
landmark that was a 1937 post office in another state, and for a mall in
the 90s it was an aerial view of a New York fort (Wikimedia Commons matches
any single word). Stock sites come back watermarked, and titles carry the
site's name, emoji and "(2026 Reviews)". This module re-ranks by what the
title and page actually say, drops what would look broken on a full-screen
tile, and trims titles to something worth reading aloud.
"""

import re
from urllib.parse import urlsplit

from . import settings

LETTERS = "ABCDEFGH"

# How much each engine's ordering can be trusted when nothing else decides.
_ENGINE_PRIOR = {
    "google cse images": 1.0,
    "google images": 1.0,
    "bing images": 0.7,
    "duckduckgo images": 0.7,
    "brave.images": 0.7,
    "qwant images": 0.6,
    "flickr": 0.5,
    "unsplash": 0.4,
    "pinterest": 0.3,
    "wikicommons.images": 0.1,
}

# Watermarked across the middle of the frame.
_STOCK_HOSTS = (
    "alamy.", "shutterstock.", "gettyimages.", "istockphoto.", "dreamstime.",
    "depositphotos.", "123rf.", "stock.adobe.", "vecteezy.", "bigstockphoto.",
    "agefotostock.", "pond5.", "canstockphoto.", "colourbox.", "freepik.",
)
_STOCK_TITLE_RE = re.compile(r"\bstock (?:photos?|images?|pictures?)\b|\broyalty[- ]free\b", re.I)

# Video thumbnails and social-CDN links: a play-button frame at best, and
# often refused outright when the kiosk loads them from another origin.
_SOCIAL_RE = re.compile(r"tiktok\.com/api/img|ytimg\.com|fbcdn\.net|lookaside\.|instagram\.", re.I)

_SOURCE_NAMES = {
    "wikipedia.org": "Wikipedia", "wikimedia.org": "Wikimedia Commons",
    "pinterest.com": "Pinterest", "pinimg.com": "Pinterest",
    "reddit.com": "Reddit", "redd.it": "Reddit",
    "tiktok.com": "TikTok", "youtube.com": "YouTube", "facebook.com": "Facebook",
    "instagram.com": "Instagram", "tripadvisor.com": "Tripadvisor",
    "flickr.com": "Flickr", "yelp.com": "Yelp", "x.com": "X", "twitter.com": "X",
    "imdb.com": "IMDb", "amazon.com": "Amazon", "etsy.com": "Etsy",
}

_STOPWORDS = {
    "the", "a", "an", "in", "of", "on", "at", "to", "for", "from", "and", "with",
    "vs", "versus", "or", "some", "me", "picture", "pictures", "photo", "photos",
    "image", "images", "pic", "pics",
}

_VERSUS_RE = re.compile(r"\s+(?:vs\.?|versus|compared\s+(?:to|with))\s+", re.I)

_LOCAL_RE = re.compile(r"\b(?:downtown|midtown|uptown|nearby|in town|local)\b", re.I)
_NEAR_ME_RE = re.compile(r"\s*\b(?:near me|around here|close by)\b", re.I)

_TITLE_TAIL_RE = re.compile(r"\s+(?:[|·•]|::)\s+.*$|\s+:\s+r/\S+$")
_DASH_SPLIT_RE = re.compile(r"\s+[-–—]\s+")
_SITE_WORDS = {n.lower() for n in _SOURCE_NAMES.values()} | {"wiki", "blog", "news", "official site"}
_TITLE_NOISE_RE = re.compile(
    r"^file:\s*|\.(?:jpe?g|png|webp|gif|tiff?)$|\((?:\d{4}\s+)?reviews?\)|\s*\([^)]*$"
    r"|\s*-\s*all you should know before (?:going|you go).*$|\s*-\s*stock (?:photos?|images?)$|\s+(?:(?:the|a|an|and|of|in|for|to|at|with)\b\s*)+\.?$|\s+\.$",
    re.I,
)
# Emoji and pictographs, which TTS reads out by name and tiles render as tofu.
_SYMBOL_RE = re.compile("[←-⯿✀-➿️\U0001f000-\U0001faff]")

_TITLE_MAX = 80
_MIN_SIDE = 200
_SMALL_SIDE = 400


def localize(query: str) -> str:
    """Pin a local-sounding query to the home city: "the post in downtown"
    otherwise returns posts from every downtown in the country."""
    city = settings.SEARCH_HOME_CITY.strip()
    if not city or city.lower() in query.lower():
        return query
    if _NEAR_ME_RE.search(query):
        return f"{_NEAR_ME_RE.sub('', query).strip()} in {city}"
    if _LOCAL_RE.search(query):
        return f"{query} {city}"
    return query


def split_comparison(query: str) -> tuple[str, str] | None:
    """'fuji apples vs honeycrisp apples' -> ('fuji apples', 'honeycrisp
    apples'). A bare left side borrows the right's last word: 'fuji vs
    honeycrisp apples' compares apples, not a Fuji and some apples."""
    parts = _VERSUS_RE.split(query.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    left, right = (p.strip(" ,.?!") for p in parts)
    if not left or not right:
        return None
    if len(left.split()) == 1 and len(right.split()) > 1:
        left = f"{left} {right.split()[-1]}"
    return left, right


def label(side: str) -> str:
    return side[:1].upper() + side[1:]


def _tokens(text: str) -> set[str]:
    text = re.sub(r"(\d)'s\b", r"\1s", text.lower())
    out = set()
    for t in re.findall(r"[a-z0-9]+", text):
        if t in _STOPWORDS or len(t) < 2:
            continue
        out.add(t[:-1] if len(t) > 3 and t.endswith("s") else t)
    return out


def host_of(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def source_name(host: str) -> str:
    """'en.wikipedia.org' -> 'Wikipedia'; unknown hosts stay as they are."""
    for domain, name in _SOURCE_NAMES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _is_site(tail: str, host: str) -> bool:
    """Does a dash-separated tail name the site rather than the subject?
    'Eat Like No One Else' on eatlikenoone.com does; 'Union Square Mall'
    after 'Loews Theatres - ' doesn't."""
    label = _compact(host.split(".")[-2] if host.count(".") else host)
    tail_c = _compact(tail)
    return bool(
        tail.lower() in _SITE_WORDS
        or re.search(r"\.(?:com|org|net|edu|gov|co|io)\b", tail, re.I)
        or (label and len(label) >= 4 and (label in tail_c or (tail_c and tail_c in label)))
    )


def clean_title(title: str, query: str = "", host: str = "") -> str:
    """'The High Line | Things To Do in ...' -> 'The High Line'."""
    t = _SYMBOL_RE.sub("", title or "").replace("_", " ")
    t = re.sub(r"\s*(?:\.\.\.|…)\s*$", "", t.strip())
    # After a pipe, a bullet or ": r/" comes the site, a section or a
    # tagline; after a dash it's only the site when the words say so.
    t = _TITLE_TAIL_RE.sub("", t)
    parts = _DASH_SPLIT_RE.split(t)
    while len(parts) > 1 and _is_site(parts[-1], host):
        parts.pop()
    t = " - ".join(parts)
    for _ in range(2):
        t = _TITLE_NOISE_RE.sub("", t).strip(" -:|,")
    t = re.sub(r"\s{2,}", " ", t)
    if len(t) > _TITLE_MAX:
        t = t[:_TITLE_MAX].rsplit(" ", 1)[0].rstrip(" ,:;-")
    return t or query


def _resolution(r: dict) -> tuple[int, int] | None:
    m = re.match(r"\s*(\d+)\s*[x×]\s*(\d+)", str(r.get("resolution") or ""))
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return (w, h) if w and h else None


def image_key(url: str) -> str:
    """The file name, so the same photo at two sizes (pinimg's /originals/
    and /736x/, a CDN's ?w= variants) counts once."""
    path = urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1].lower() or url


def rank(raw: list[dict], query: str, count: int = 6, first: int = 0,
         exclude: set[str] | None = None) -> list[dict]:
    """SearXNG image rows -> up to `count` display items lettered from
    LETTERS[first] (A, B, C... by default). Images whose image_key is in
    `exclude` are skipped: a comparison's second side doesn't repeat the
    first's.

    Each item: {id, url, thumb_url, title, source, snippet, aspect_ratio}.
    """
    terms = _tokens(query)
    city = _tokens(settings.SEARCH_HOME_CITY)
    scored = []
    for pos, r in enumerate(raw):
        img = r.get("img_src") or r.get("thumbnail_src") or ""
        if not img.startswith(("http://", "https://")):
            continue
        if re.search(r"(?:spacer|blank|pixel)\.gif|\.svg(?:$|\?)", img, re.I):
            continue
        if "svg" in str(r.get("img_format") or "").lower():
            continue
        size = _resolution(r)
        if size:
            w, h = size
            if min(w, h) < _MIN_SIDE or not 0.31 <= w / h <= 3.2:
                continue
        page = r.get("url") or ""
        host = host_of(page) or str(r.get("source") or "").lower()
        title = r.get("title") or ""
        words = _tokens(f"{title} {urlsplit(page).path if page else ''}")
        overlap = len(terms & words) / len(terms) if terms else 0.0
        score = 3.0 * overlap
        score += _ENGINE_PRIOR.get(str(r.get("engine") or ""), 0.4)
        score += 0.3 * min(float(r.get("score") or 0), 1.0)
        if city and city <= words and not city <= terms:
            score += 0.5
        if any(s in host for s in _STOCK_HOSTS) or _STOCK_TITLE_RE.search(title):
            score -= 2.0
        if _SOCIAL_RE.search(img) or _SOCIAL_RE.search(page):
            score -= 1.0
        if size and min(size) < _SMALL_SIDE:
            score -= 1.0
        if terms and not overlap:
            score -= 1.0
        scored.append((score, -pos, r, img, host, size))

    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    items: list[dict] = []
    seen_images: set[str] = set(exclude or ())
    seen_titles: set[tuple[str, str]] = set()
    per_host: dict[str, int] = {}
    for _score, _pos, r, img, host, size in scored:
        title = clean_title(r.get("title") or "", query, host)
        tkey = (title.lower(), host)
        ikey = image_key(img)
        if ikey in seen_images or tkey in seen_titles or per_host.get(host, 0) >= 2:
            continue
        seen_images.add(ikey)
        seen_titles.add(tkey)
        per_host[host] = per_host.get(host, 0) + 1
        snippet = re.sub(r"\s+", " ", _SYMBOL_RE.sub("", r.get("content") or "")).strip()
        if snippet.lower() in (r.get("title") or "").lower() or snippet.lower() in title.lower():
            snippet = ""
        items.append({
            "id": LETTERS[first + len(items)],
            "url": img,
            "thumb_url": r.get("thumbnail_src") or img,
            "title": title,
            "source": source_name(host),
            "snippet": snippet,
            "aspect_ratio": round(size[0] / size[1], 3) if size else 1.0,
        })
        if len(items) >= min(count, len(LETTERS) - first):
            break
    return items
