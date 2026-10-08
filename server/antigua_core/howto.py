"""Household how-tos, walked through like a recipe: "walk me through cutting
out drywall" -> supplies and numbered steps, spoken a step at a time and shown
on the kiosk.

Almost no how-to page carries schema.org HowTo data (about 1 in 10, and none
of those listed supplies; 2026-10-07), so the steps are written by the LLM
from the pages themselves. Finding them: SearXNG for the URLs (Parallel's
web_search when it's empty), a plain fetch, and Parallel's web_fetch for the
sites that refuse ours (Home Depot, Lowe's, Angi). The result is a
recipe.Recipe with kind="howto", so the cooking session runs it unchanged.

Two checks on what the LLM wrote, never on the pages (a page's sidebar
mentions everything):
- a fixed list of combinations that must never be suggested (bleach with
  vinegar or ammonia, and so on): the guide is refused;
- risk words (power tools, ladders, harsh chemicals, wiring, gas, heat,
  fumes, blades): a fixed caution line, said once and kept on the card.
"""

import hashlib
import html as _html
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from . import settings
from .caches import parallel_mcp
from .recipe import _HEADERS, Recipe

log = logging.getLogger("antigua_core")

_CACHE_DAYS = 60
_PAGES = 4                      # pages read per guide
_PAGE_CHARS = 4000              # of each page's text: 4 pages fit the 8k context
_SKIP_HOSTS = re.compile(r"youtube|youtu\.be|reddit|quora|pinterest|facebook|tiktok|instagram|"
                         r"amazon\.|ebay\.|walmart\.com/ip|homedepot\.com/p/|lowes\.com/pd/")

# ── safety ──────────────────────────────────────────────────────────────────

# Never together: each makes a toxic gas or a dangerous reaction.
_BLEACH = r"\bbleach|sodium\s+hypochlorite|\bclorox"
_NEVER = [
    (_BLEACH, r"\bvinegar|\bammonia|\bacid\b|muriatic|hydrochloric|rubbing\s+alcohol|"
              r"isopropyl|hydrogen\s+peroxide|toilet\s+bowl\s+cleaner|lemon\s+juice|\bwindex",
     "bleach"),
    (r"hydrogen\s+peroxide", r"\bvinegar", "hydrogen peroxide"),
    (r"drain\s+cleaner|\blye\b|sodium\s+hydroxide", r"drain\s+cleaner.*drain\s+cleaner|"
     r"another\s+drain\s+cleaner|different\s+drain\s+cleaner|\bacid\b|muriatic", "drain cleaner"),
]

_RISKS = [
    ("power tools", r"\b(?:power\s+tools?|circular\s+saw|jig\s*saw|reciprocating\s+saw|"
                    r"oscillating(?:\s+multi)?[\s-]tool|angle\s+grinder|drill|rotary\s+tool|"
                    r"sander|nail\s+gun|table\s+saw|heat\s+gun|chainsaw|pressure\s+washer)\b"),
    ("a ladder", r"\b(?:ladder|step\s*stool|on\s+the\s+roof|rooftop|scaffold)"),
    ("harsh chemicals", r"\b(?:bleach|ammonia|muriatic|hydrochloric|\blye\b|sodium\s+hydroxide|"
                        r"drain\s+cleaner|oven\s+cleaner|borax|acetone|mineral\s+spirits|"
                        r"paint\s+thinner|trisodium\s+phosphate|insecticide|pesticide)"),
    ("electrical work", r"\b(?:circuit\s+breaker|breaker\s+box|turn\s+off\s+(?:the\s+)?power|"
                        r"live\s+wires?|voltage\s+tester|electrical\s+box|outlet\s+box|wiring)\b"),
    ("gas", r"\b(?:gas\s+line|gas\s+valve|pilot\s+light|natural\s+gas|propane)\b"),
    ("heat or flame", r"\b(?:blowtorch|torch|open\s+flame|self[\s-]clean(?:ing)?\s+cycle|"
                      r"[45]\d\d\s*(?:°|degrees)\s*f?)"),
    ("dust or fumes", r"\b(?:respirator|dust\s+mask|n95|ventilat|fumes|asbestos|lead\s+paint|"
                      r"\bmold\b|mildew\s+remover)"),
    ("sharp blades", r"\b(?:utility\s+knife|razor\s+blade|box\s+cutter|drywall\s+saw|"
                     r"jab\s+saw|keyhole\s+saw|chisel)\b"),
]

CAUTION = ("Heads up: this involves {what}. Be careful, use your best judgement, "
           "and look it up online if you're unsure about anything.")


def never_together(text: str) -> str:
    """The first dangerous combination in `text` ("bleach"), or ""."""
    for a, b, name in _NEVER:
        if re.search(a, text, re.I) and re.search(b, text, re.I):
            return name
    return ""


def caution_for(text: str) -> str:
    """The fixed caution line naming what's risky in `text`, or ""."""
    found = [name for name, rx in _RISKS if re.search(rx, text, re.I)]
    if not found:
        return ""
    what = found[0] if len(found) == 1 else ", ".join(found[:-1]) + " and " + found[-1]
    return CAUTION.format(what=what)


# ── finding and reading the pages ───────────────────────────────────────────

def _urls(question: str) -> list:
    try:
        r = requests.get(f"{settings.SEARCH_URL}/search", timeout=settings.SEARCH_TIMEOUT, params={
            "q": question, "format": "json", "engines": settings.SEARCH_ENGINES,
            "categories": "general", "language": "en-US"})
        r.raise_for_status()
        rows = r.json().get("results", [])
    except (requests.RequestException, ValueError) as e:
        log.info("Howto: SearXNG failed: %s", e)
        rows = []
    out = []
    for row in rows:
        u = row.get("url") or ""
        if u.startswith("http") and not _SKIP_HOSTS.search(urlparse(u).hostname or "") and u not in out:
            out.append(u)
    return out[:_PAGES]


def page_text(page: str) -> str:
    """Readable text of an HTML page: the article or main element when there
    is one, without scripts, styles, navigation, headers and footers."""
    for tag in ("article", "main"):
        m = re.search(rf"<{tag}\b.*?</{tag}>", page, re.S | re.I)
        if m and len(m.group(0)) > 2000:
            page = m.group(0)
            break
    page = re.sub(r"<(script|style|nav|header|footer|aside|form|svg|noscript)\b.*?</\1>",
                  " ", page, flags=re.S | re.I)
    page = re.sub(r"<(?:br|/p|/li|/h\d|/div|/tr)\b[^>]*>", "\n", page, flags=re.I)
    text = _html.unescape(re.sub(r"<[^>]+>", " ", page))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if len(line) > 2)


def _fetch(url: str) -> str:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=6)
        if r.status_code == 200:
            text = page_text(r.content.decode("utf-8", errors="replace"))
            if len(text.split()) > 150:
                return text
        log.info("Howto: fetch %s -> HTTP %s", url, r.status_code)
    except requests.RequestException as e:
        log.info("Howto: fetch %s failed: %s", url, type(e).__name__)
    return ""


def gather(question: str) -> list:
    """[(url, text)] to write the guide from: up to _PAGES pages, blocked
    ones through Parallel's web_fetch; Parallel's search excerpts when
    SearXNG finds nothing."""
    urls = _urls(question)
    if not urls:
        data = parallel_mcp("web_search", {"objective": f"step-by-step instructions: {question}",
                                           "search_queries": [question]})
        return [(r.get("url", ""), "\n".join(r.get("excerpts") or []))
                for r in (data or {}).get("results", [])[:_PAGES] if r.get("excerpts")]
    with ThreadPoolExecutor(len(urls)) as pool:
        texts = dict(zip(urls, pool.map(_fetch, urls)))
    missing = [u for u in urls if not texts[u]]
    if missing:
        data = parallel_mcp("web_fetch", {"urls": missing, "full_content": True,
                                          "objective": f"step-by-step instructions, tools and "
                                                       f"materials: {question}"}, timeout=40)
        for r in (data or {}).get("results", []):
            if r.get("url") in texts:
                texts[r["url"]] = r.get("full_content") or "\n".join(r.get("excerpts") or [])
    return [(u, texts[u][:_PAGE_CHARS]) for u in urls if texts[u]]


# ── writing the guide ───────────────────────────────────────────────────────

_PROMPT = """You turn web pages into a short spoken how-to guide for a home \
assistant. Task: "{question}"

Write it ONLY from the pages below. Combine what they agree on; leave out \
ads, product pitches, stories and anything not about the task.

Reply with JSON only:
{{"title": "How to ... (short, under 8 words)",
  "supplies": ["each tool or material, with amounts when the pages give them"],
  "steps": ["one action per step, 1-2 short sentences, plain spoken English"],
  "source": "the site name you relied on most"}}

Rules: 3 to 12 steps. No numbering inside the steps. Never suggest mixing \
cleaning products. If the pages don't explain how to do the task, reply \
{{"steps": []}}.

{pages}"""


def _write(question: str, pages: list, llm) -> dict:
    blocks = "\n\n".join(f"PAGE {i + 1} ({urlparse(u).hostname}):\n{text}"
                         for i, (u, text) in enumerate(pages))
    raw = llm(_PROMPT.format(question=question, pages=blocks))
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        m = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    return data if isinstance(data, dict) else {}


def _clean(items, limit: int) -> list:
    out = []
    for x in items or []:
        x = re.sub(r"^\s*(?:step\s*)?\d+[.):]\s*", "", str(x), flags=re.I).strip()
        if x and x not in out:
            out.append(x)
    return out[:limit]


class HowtoError(Exception):
    """A guide that can't or mustn't be given; str() is what to say."""


def _cache_path(question: str):
    key = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:60]
    digest = hashlib.sha1(question.lower().encode()).hexdigest()[:8]
    return settings.DATA_DIR / "howtos" / f"{key}-{digest}.json"


def build(question: str, llm) -> Recipe:
    """The guide for `question` as a Recipe (kind="howto"), cached on disk.
    `llm(prompt) -> str` answers in JSON. Raises HowtoError."""
    path = _cache_path(question)
    try:
        d = json.loads(path.read_text())
        if time.time() - d["at"] < _CACHE_DAYS * 86400:
            return Recipe.from_dict(d["guide"])
    except (OSError, ValueError, KeyError):
        pass

    pages = gather(question)
    if not pages:
        raise HowtoError("I couldn't find instructions for that online.")
    data = _write(question, pages, llm)
    steps, supplies = _clean(data.get("steps"), 12), _clean(data.get("supplies"), 25)
    if len(steps) < 3:
        raise HowtoError("I couldn't find clear step-by-step instructions for that.")
    everything = " ".join(supplies + steps)
    bad = never_together(everything)
    if bad:
        log.warning("Howto: refused %r: %s combination in %r", question, bad, everything[:300])
        raise HowtoError(f"The instructions I found mix {bad} with something it reacts with, "
                         "which can make a toxic gas, so I won't walk you through them. "
                         "Never mix cleaning products.")
    title = (data.get("title") or "").strip() or question.strip().rstrip("?").capitalize()
    host = urlparse(pages[0][0]).hostname or ""
    source = re.sub(r"^(?:https?://)?www\.", "", (data.get("source") or "").strip() or host).rstrip("/")
    guide = Recipe(title=title, source=source,
                   url=pages[0][0], ingredients=supplies, steps=steps,
                   kind="howto", caution=caution_for(everything))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "question": question,
                                    "guide": guide.to_dict()}))
    except OSError as e:
        log.warning("Howto: cache write failed: %s", e)
    return guide


# ── what asks for a guide ───────────────────────────────────────────────────

_POLITE = r"(?:(?:hey |ok |okay )?(?:alexa,? )?)?(?:can you |could you |would you |please |will you )?"
_ASK = [
    re.compile(_POLITE + r"(?:walk|talk|take|guide) me through (?:how (?:to|do (?:i|you)) |how i (?:can |should )?)?(?P<q>.+)", re.I),
    re.compile(_POLITE + r"show me (?:on the (?:screen|display) )?how (?:to|do (?:i|you)) (?P<q>.+)", re.I),
    re.compile(_POLITE + r"(?:give me |i (?:want|need) )?(?:a |the )?step[- ]by[- ]step(?: guide| instructions)? "
               r"(?:on |for |to )?(?:how (?:to|do (?:i|you)) )?(?P<q>.+)", re.I),
    re.compile(_POLITE + r"(?P<q>how (?:do|can|should) (?:i|you|we) .+?),? step[- ]by[- ]step\W*$", re.I),
]
# "Walk me through it" right after a how-to question: that question.
_FOLLOW = re.compile(_POLITE + r"(?:(?:walk|talk|take|guide) me through (?:it|that|this|those steps)|"
                     r"show (?:me )?(?:it |that |the steps )?(?:on the (?:screen|display)|step[- ]by[- ]step)|"
                     r"(?:put|show) (?:it|that|the steps) on the (?:screen|display)|"
                     r"step[- ]by[- ]step(?: please)?)\W*$", re.I)
# Food goes to the recipe skill; directions and words aren't household jobs.
_NOT_HOWTO = re.compile(r"^(?:make|making|cook|cooking|bake|baking|prepare|grill|roast|brew)\b|"
                        r"^(?:get|go|drive|walk) to\b|^(?:say|spell|pronounce)\b", re.I)
# A question shaped like a how-to, remembered for "walk me through it".
HOWTO_SHAPED = re.compile(r"^\W*(?:how (?:do|can|should|would) (?:i|you|we)|how to|what(?:'s| is) the best way to)\b", re.I)


def parse_request(text: str, last_question: str | None = None) -> str | None:
    """The task a request asks to be walked through, as "how to ..."; or
    None. `last_question` answers "walk me through it"."""
    text = (text or "").strip()
    if last_question and _FOLLOW.match(text):
        q = last_question
    else:
        for rx in _ASK:
            m = rx.match(text)
            if m:
                q = m.group("q")
                break
        else:
            return None
    q = re.sub(r"[?.!]+$", "", q).strip()
    q = re.sub(r"^(?:how (?:do|can|should|would) (?:i|you|we)|how to|what(?:'s| is) the best way to)\s+",
               "", q, flags=re.I)
    q = re.sub(r",?\s*step[- ]by[- ]step$", "", q, flags=re.I).strip()
    if len(q.split()) < 2 or _NOT_HOWTO.match(q):
        return None
    # "cutting out drywall" reads fine as it is; "clean the oven" takes "how to".
    return q if re.match(r"\w+ing\b", q) else "how to " + q
