"""Knowledge skill: people, history and events, answered from Wikipedia.

One request fetches the article's full plain text (and its Wikidata id); the
spoken overview is grounded in the lead section. Structured facts — spouse,
birthplace, dates, awards, notable works, participants, casualties — come from
Wikidata on a background thread while the overview is being spoken, and the
whole article stays with the Topic so follow-ups ("was she married?", "where
was she from?", "tell me more") are answered from the passages that actually
cover them. The model never has to remember anything about the subject.
"""

import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from threading import Event, Lock, Thread

import requests

from . import settings
from .intents.knowledge import is_more_request as _is_more

log = logging.getLogger("antigua_core")

# Wikimedia throttles clients without contact details in the User-Agent
# (429s after a handful of lookups); the project URL is the contact.
_UA = "Antigua/1.0 (self-hosted home voice assistant; https://github.com/flavanoids/antigua-assistant)"

# Wikidata properties worth speaking, by kind of subject. Items resolve to
# labels; times and quantities are formatted inline.
_FACT_PROPS = [
    ("P569", "Born on"), ("P19", "Born in"), ("P570", "Died on"), ("P20", "Died in"),
    ("P509", "Cause of death"), ("P27", "Citizenship"), ("P106", "Occupation"),
    ("P26", "Spouse"), ("P451", "Partner"), ("P40", "Children"), ("P22", "Father"),
    ("P25", "Mother"), ("P3373", "Siblings"), ("P69", "Educated at"),
    ("P39", "Positions held"), ("P102", "Political party"), ("P463", "Member of"),
    ("P136", "Genre"), ("P1303", "Instruments"), ("P264", "Record label"),
    ("P800", "Notable works"), ("P166", "Awards"), ("P1411", "Nominated for"),
    ("P135", "Movement"), ("P140", "Religion"),
    # Events and places
    ("P580", "Started"), ("P582", "Ended"), ("P585", "Date"), ("P571", "Founded"),
    ("P576", "Dissolved"), ("P112", "Founded by"), ("P276", "Location"),
    ("P17", "Country"), ("P710", "Participants"), ("P1120", "Deaths"),
    ("P1339", "Injured"), ("P361", "Part of"), ("P793", "Significant events"),
]
_MAX_VALUES = 8

# Question words -> words the passage answering them is likely to contain.
# "Was she married?" never says "spouse"; the article section is "Personal life".
_EXPANSIONS = {
    r"marr|wife|husband|spouse|partner|divorc|wedd|casad|espos|marido": (
        "married", "marriage", "wife", "husband", "spouse", "divorce", "wedding",
        "remarried", "relationship", "personal"),
    r"hometown|from|grew|born|birth|childhood|raised|early|naci|origen": (
        "born", "birth", "early", "childhood", "raised", "family", "native", "grew"),
    r"die|death|dead|kill|murder|assassin|funeral|buried|muri|muerte": (
        "died", "death", "killed", "assassinated", "funeral", "buried", "illness", "later"),
    r"achiev|accomplish|known|famous|notable|award|won|prize|honou?r|legacy|logro|premio": (
        "award", "awards", "prize", "honor", "honour", "legacy", "recognition",
        "achievement", "notable", "known", "record", "influence", "won"),
    r"kid|child|son|daughter|hij": ("children", "son", "daughter", "child"),
    r"parent|mother|father|mom|dad|famil|padre|madre": (
        "father", "mother", "parents", "family", "siblings", "sister", "brother"),
    r"school|educat|college|universit|stud|escuel": (
        "school", "education", "university", "college", "studied", "graduated"),
    r"work|career|job|song|album|film|movie|paint|book|wrote|record|obra|pel[ií]cula": (
        "career", "work", "works", "album", "film", "painting", "book", "released", "published"),
    r"start|began|begin|cause|why|origin": (
        "began", "cause", "causes", "origins", "background", "led", "started"),
    r"end|result|outcome|aftermath|won|lost|impact|effect": (
        "ended", "aftermath", "result", "outcome", "legacy", "impact", "treaty"),
    r"how\s+many|casualt|people|deaths|toll": ("killed", "deaths", "casualties", "dead", "wounded"),
    r"old|age|year|when": ("born", "died", "aged", "age", "year"),
    r"polit|party|elect|office|president|govern": (
        "elected", "election", "party", "office", "president", "political", "government"),
}
_STOP = {
    "the", "a", "an", "was", "were", "is", "are", "did", "does", "do", "she", "he",
    "they", "her", "his", "him", "their", "them", "it", "its", "what", "who", "when",
    "where", "why", "how", "which", "tell", "me", "about", "any", "and", "of", "in",
    "on", "to", "for", "with", "have", "had", "has", "that", "this", "there", "be",
    "been", "much", "many", "more", "else", "can", "you", "know", "antigua",
}


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower())
                   if not unicodedata.combining(c))


def _tidy(text: str) -> str:
    """Strip what reads badly aloud: IPA, pronunciation guides, citation debris."""
    text = re.sub(r"\s*\((?:[^()]*?;\s*)?(?:[A-Za-z ]+ )?pronunciation:?\s*\[[^\]]*\][^();]*;?\s*",
                  " (", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\(\s*[;,]?\s*\)", "", text)
    text = re.sub(r"\(\s*;?\s*", "(", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r" ([,.;:])", r"\1", text).strip()


@dataclass
class Passage:
    section: str
    text: str


@dataclass
class Topic:
    title: str
    description: str
    lead: str
    passages: list
    lang: str = "en"
    qid: str = ""
    facts: list = field(default_factory=list)
    facts_ready: Event = field(default_factory=Event)
    used: set = field(default_factory=set)  # passages already spoken

    def wait_facts(self, timeout: float) -> list:
        self.facts_ready.wait(timeout)
        return self.facts


def _split_article(extract: str) -> tuple:
    """(lead, [Passage]) from a plain-text extract with == wiki == headings."""
    lead_lines, passages, section = [], [], ""
    skip = re.compile(r"^(?:see also|notes|references|further reading|external links|"
                      r"bibliography|sources|citations|footnotes|works cited|"
                      r"filmography|discography|véase también|referencias|enlaces externos|"
                      r"bibliografía|notas)$", re.IGNORECASE)
    skipping = False
    for line in extract.splitlines():
        line = line.strip()
        if not line:
            continue
        h = re.match(r"^=+\s*(.*?)\s*=+$", line)
        if h:
            name = h.group(1)
            level = len(line) - len(line.lstrip("="))
            if level == 2:
                skipping = bool(skip.match(name))
                section = name
            elif not skipping:
                section = f"{section.split(' / ')[0]} / {name}" if section else name
            continue
        if skipping:
            continue
        text = _tidy(line)
        if len(text) < 40:
            continue
        if not section:
            lead_lines.append(text)
        else:
            passages.append(Passage(section, text))
    return "\n".join(lead_lines), passages


def _cap(text: str, limit: int) -> str:
    """Trim at a paragraph, then sentence, boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("\n", ". "):
        i = cut.rfind(sep)
        if i > limit // 2:
            return cut[: i + (1 if sep == ". " else 0)].strip()
    return cut.rsplit(" ", 1)[0]


def _qualifier_year(claim: dict, pid: str) -> str | None:
    for q in claim.get("qualifiers", {}).get(pid, []):
        m = re.match(r"([+-])(\d+)", q.get("datavalue", {}).get("value", {}).get("time", ""))
        if m:
            return f"{int(m.group(2))}{' BC' if m.group(1) == '-' else ''}"
    return None


def _span(claim: dict) -> str:
    """When a fact held: "from 2014", "2017 to 2021", "2010". Spouses, offices
    and awards carry these as qualifiers. Handing the model the date beside the
    fact is what stops it borrowing a nearby one: asked when Kamala Harris
    married, it said 2024 — a year her article mentions, for her campaign."""
    start, end = _qualifier_year(claim, "P580"), _qualifier_year(claim, "P582")
    if start and end:
        return f"{start} to {end}"
    if start:
        return f"from {start}"
    if end:
        return f"until {end}"
    return _qualifier_year(claim, "P585") or ""


def _format_time(v: dict) -> str | None:
    m = re.match(r"([+-])(\d+)-(\d\d)-(\d\d)", v.get("time", ""))
    if not m:
        return None
    sign, y, mo, d = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
    era = " BC" if sign == "-" else ""
    prec = v.get("precision", 11)
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    if prec >= 11 and mo and d:
        return f"{months[mo - 1]} {d}, {y}{era}"
    if prec == 10 and mo:
        return f"{months[mo - 1]} {y}{era}"
    if prec == 8:
        return f"the {y}s{era}"
    return f"{y}{era}"


def _title_fits(subject: str, title: str, redirect: str = "", top: bool = False) -> bool:
    """Does this article title plausibly name what was asked?

    A misheard name still returns *something* ("free the call" -> "Toll-free
    telephone number"); describing the wrong thing confidently is worse than
    saying nothing. At least half the asked words must be in the title, and
    the rest must be near-misses of a title word — Whisper's "Frida Callo" is
    still Frida Kahlo. Initials count ("AOC", "JFK"), and so does the redirect
    the search matched when it *is* what was asked — "The Rock (wrestler)" for
    "the Rock", but not "Prince Charles, Prince of Wales" for "Prince". The
    top hit may be a shorter form of what was asked ("Selena Quintanilla" ->
    Selena).
    """
    want = _words(subject)
    have = set(re.findall(r"[a-z0-9']{2,}", _fold(title).replace(".", "")))
    if not want:
        return True
    if _is_initials(want, title) or (redirect and _words(redirect) == want):
        return True
    if top and have and have <= want:
        return True
    hit = want & have
    if 2 * len(hit) < len(want):
        return False
    return all(
        any(SequenceMatcher(None, w, h).ratio() >= 0.6 for h in have) for w in want - hit
    )


# Suffixes that don't distinguish anyone: "MLK Jr." is still "MLK".
_HONORIFICS = {"jr", "sr", "ii", "iii", "iv"}


def _words(text: str) -> set:
    """Distinguishing words of a name, ignoring any "(musician)" qualifier."""
    core = re.sub(r"\(.*?\)", "", _fold(text)).replace(".", "")
    return {w for w in re.findall(r"[a-z0-9']{2,}", core)
            if w not in _STOP and w not in _HONORIFICS}


def _is_initials(want: set, title: str) -> bool:
    """"AOC" -> Alexandria Ocasio-Cortez, "JFK" -> John F. Kennedy."""
    if len(want) != 1:
        return False
    abbr = next(iter(want))
    words = re.findall(r"[a-z0-9]+", re.sub(r"\(.*?\)", "", _fold(title)).replace(".", ""))
    initials = "".join(w[0] for w in words if w not in _HONORIFICS)
    return 2 <= len(abbr) <= 5 and len(words) >= 2 and initials == abbr


def _is_primary(subject: str, title: str) -> bool:
    """The article is titled plainly what was asked — "Selena", "Madonna" —
    with no "(musician)" qualifier: Wikipedia's editors made it the primary
    topic for that name, which outranks raw popularity (Selena Gomez is read
    more than Selena, but "who was Selena" means Selena)."""
    return "(" not in title and bool(_words(subject)) and _words(title) == _words(subject)


# A biography carries birth/death or "Living people" categories; bands and
# peoples carry group categories. A "who" question must land on one of these,
# so "who is the Rock" can't be answered with an article on rock music.
_PERSON_CAT_RE = re.compile(
    r"^(?:Category|Categoría):(?:"
    r"Living people|Possibly living people|\d{1,4}s?(?: BC| BCE)? (?:births|deaths)|"
    r"Nacidos en|Fallecidos en|Personas vivas|"
    r".*\b(?:musical groups|music groups|musical duos|musical trios|bands|boy bands|girl groups|"
    r"peoples|ethnic groups|tribes|dynasties|families|organizations|organisations|"
    r"political parties|sports teams|comedy troupes|grupos de m[uú]sica|pueblos|"
    r"dinast[ií]as|familias|organizaciones)\b)",
    re.IGNORECASE,
)


# Creative works: "who were the Black Panthers" is the party, never the film.
_WORK_CAT_RE = re.compile(
    r"^(?:Category|Categoría):.*\b(?:films|film series|albums|songs|singles|"
    r"television series|TV series|video games|novels|books|comics|episodes|"
    r"soundtracks|musicals|plays|franchises|pel[ií]culas|[aá]lbumes|canciones|"
    r"series de televisi[oó]n|videojuegos|novelas)\b",
    re.IGNORECASE,
)


class WikiKnowledge:
    """Wikipedia + Wikidata lookups, cached per (subject, language)."""

    def __init__(self, timeout: float | None = None, cache_ttl: int = 3600):
        self.timeout = timeout or settings.KNOWLEDGE_TIMEOUT
        self.cache_ttl = cache_ttl
        self._session = requests.Session()
        self._session.headers["User-Agent"] = _UA
        self._cache: dict = {}
        self._lock = Lock()

    # ── Lookup ──────────────────────────────────────────────────────────────

    def lookup(self, subject: str, lang: str = "en", who: str | None = None) -> Topic | None:
        """The best-matching article for `subject`, or None when nothing fits.
        who="person" ("who is/was") accepts only people; who="group" ("who
        were/are") anything but a creative work."""
        key = (_fold(subject), lang, who)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[1] < self.cache_ttl:
                return hit[0]
        topic = self._fetch(subject, lang, who)
        if topic is not None:
            with self._lock:
                self._cache[key] = (topic, time.time())
        return topic

    def _get(self, url: str, params: dict) -> dict:
        """GET JSON, waiting out one rate limit (429) if it asks for 2s or less."""
        for attempt in (0, 1):
            r = self._session.get(url, params=params, timeout=self.timeout)
            if r.status_code == 429 and attempt == 0:
                try:
                    wait = float(r.headers.get("Retry-After", 1))
                except ValueError:
                    wait = 1.0
                if wait <= 2:
                    log.info("Knowledge: rate limited, retrying in %.1fs", wait)
                    time.sleep(wait)
                    continue
            r.raise_for_status()
            return r.json()
        return {}

    def _api(self, lang: str, **params) -> dict:
        return self._get(
            f"https://{lang}.wikipedia.org/w/api.php",
            {"action": "query", "format": "json", "formatversion": 2, **params},
        ).get("query", {})

    def _fetch(self, subject: str, lang: str, who: str | None = None) -> Topic | None:
        # Two requests: the API returns a full plain-text extract for only one
        # page per call, so search first, then fetch the article that fits.
        # Two rankings. By page views, a bare surname is the famous one
        # ("Mamdani" -> Zohran, not his father Mahmood) — but its lower hits are
        # noise ("Frida" -> George Lucas), which the title check discards. By
        # relevance is the backstop for everything the popular list misses.
        search = dict(list="search", srsearch=subject, srlimit=5, srprop="redirecttitle")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_pop = pool.submit(self._api, lang, srqiprofile="popular_inclinks_pv", **search)
            f_rel = pool.submit(self._api, lang, srinfo="suggestion", **search)
            found = f_rel.result()
            try:
                popular = f_pop.result()["search"]
            except Exception as e:
                log.warning("Knowledge: popular ranking failed for %r: %s", subject, e)
                popular = []
        hits = found["search"]
        candidates = {}  # title -> names exactly what was asked
        for ranked in (popular, hits):
            for i, h in enumerate(ranked):
                title, alias = h["title"], h.get("redirecttitle", "")
                if title not in candidates and _title_fits(subject, title, alias, top=i == 0):
                    candidates[title] = _is_primary(subject, title)
        # Whisper's spelling of a name ("Frida Callo") often has no hits at all,
        # but Wikipedia's did-you-mean knows it — one retry with that.
        suggestion = found.get("searchinfo", {}).get("suggestion")
        if not candidates and suggestion and _fold(suggestion) != _fold(subject):
            log.info("Knowledge: %r -> did you mean %r", subject, suggestion)
            return self._fetch(suggestion, lang, who)
        titles = self._rank(lang, candidates, who)
        for title in titles[:2]:
            pages = self._api(lang, titles=title, redirects=1,
                              prop="extracts|pageprops|description", explaintext=1,
                              exsectionformat="wiki",
                              ppprop="wikibase_item|disambiguation")["pages"]
            page = pages[0] if pages else {}
            props = page.get("pageprops", {})
            if "disambiguation" in props or not page.get("extract"):
                continue
            lead, passages = _split_article(page["extract"])
            topic = Topic(
                title=page["title"],
                description=page.get("description", ""),
                lead=lead or (passages[0].text if passages else ""),
                passages=passages,
                lang=lang,
                qid=props.get("wikibase_item", ""),
            )
            Thread(target=self._load_facts, args=(topic,), daemon=True,
                   name="wikidata-facts").start()
            log.info("Knowledge: %r -> %r (%s, %d passages)", subject, topic.title,
                     topic.description, len(passages))
            return topic
        log.info("Knowledge: no article matched %r (candidates: %s)", subject,
                 [h["title"] for h in hits])
        return None

    def _rank(self, lang: str, candidates: dict, who: str | None) -> list:
        """Candidates worth fetching, best first.

        Disambiguation pages go. For any "who" question so do creative works,
        and for "who is/was" anything that isn't a person. Of the rest, an article titled plainly what was asked wins
        (see _is_primary); otherwise the most-read one does, because that is
        who people mean: "Frida" is Frida Kahlo (162k views a month), not
        Anni-Frid Lyngstad, whose nickname is Frida (65k); "Kamala" is Kamala
        Harris, not the wrestler; "MJ" is Michael Jordan, not MJ Lenderman.
        """
        if not candidates:
            return []
        # Eight at most: the page-view data is only returned for about ten
        # titles per request.
        titles = list(candidates)[:8]
        pages = self._api(lang, titles="|".join(titles), prop="pageprops|categories|pageviews",
                          ppprop="disambiguation", cllimit="max", clshow="!hidden",
                          pvipdays=30, redirects=1)
        # Search hits are canonical titles already, but map any redirect back.
        canon = {r["to"]: r["from"] for r in pages.get("redirects", [])}
        info = {canon.get(p["title"], p["title"]): p for p in pages.get("pages", [])}
        keep = []
        for title in titles:
            page = info.get(title, {})
            if "disambiguation" in page.get("pageprops", {}):
                continue
            cats = [c["title"] for c in page.get("categories", [])]
            if who and any(_WORK_CAT_RE.match(c) for c in cats):
                continue
            if who == "person" and not any(_PERSON_CAT_RE.match(c) for c in cats):
                continue
            keep.append(title)
        dropped = [t for t in titles if t not in keep]
        if dropped:
            log.info("Knowledge: skipped %s (disambiguation%s)", dropped,
                     {"person": ", work or not a person", "group": " or work"}.get(who, ""))

        def views(title):
            return sum(v or 0 for v in (info.get(title, {}).get("pageviews") or {}).values())

        ranked = sorted(keep, key=lambda t: (not candidates[t], -views(t)))
        log.info("Knowledge: candidates %s",
                 [(t, views(t), "primary" if candidates[t] else "") for t in ranked])
        return ranked

    # ── Wikidata ────────────────────────────────────────────────────────────

    def _load_facts(self, topic: Topic):
        try:
            if topic.qid:
                topic.facts = self._facts(topic.qid, topic.lang)
        except Exception as e:
            log.warning("Wikidata facts for %s failed: %s", topic.title, e)
        finally:
            topic.facts_ready.set()

    def _wd(self, **params) -> dict:
        return self._get("https://www.wikidata.org/w/api.php", {"format": "json", **params})

    def _facts(self, qid: str, lang: str) -> list:
        claims = self._wd(action="wbgetentities", ids=qid, props="claims")[
            "entities"][qid].get("claims", {})
        raw, item_ids = [], []
        for pid, label in _FACT_PROPS:
            values = []
            for c in claims.get(pid, []):
                if c.get("rank") == "deprecated":
                    continue
                dv = c.get("mainsnak", {}).get("datavalue", {})
                values.append((dv.get("type"), dv.get("value"), _span(c)))
            if not values:
                continue
            if values[0][0] == "time":
                values = values[:1]  # later values are the same date, less precise
            raw.append((label, values[:_MAX_VALUES]))
            item_ids += [v["id"] for t, v, _ in values[:_MAX_VALUES] if t == "wikibase-entityid"]
        labels = {}
        ids = list(dict.fromkeys(item_ids))
        for i in range(0, len(ids), 50):
            ents = self._wd(action="wbgetentities", ids="|".join(ids[i:i + 50]),
                            props="labels", languages=f"{lang}|en")["entities"]
            for eid, ent in ents.items():
                lab = ent.get("labels", {})
                labels[eid] = (lab.get(lang) or lab.get("en") or {}).get("value")
        facts = []
        for label, values in raw:
            out = []
            for typ, v, span in values:
                if typ == "wikibase-entityid":
                    text = labels.get(v["id"])
                elif typ == "time":
                    text = _format_time(v)
                elif typ == "quantity":
                    text = v.get("amount", "").lstrip("+")
                elif typ == "string":
                    text = v
                else:
                    text = None
                if text and span:
                    text = f"{text} ({span})"
                if text and text not in out:
                    out.append(text)
            if out:
                facts.append(f"{label}: {', '.join(out)}")
        return facts

    # ── Prompt context ──────────────────────────────────────────────────────

    def overview_context(self, topic: Topic) -> str:
        lead = _cap(topic.lead, settings.KNOWLEDGE_LEAD_CHARS)
        desc = f" ({topic.description})" if topic.description else ""
        return (
            f"Encyclopedia article: {topic.title}{desc}\n{lead}\n\n"
            f"Give a short but substantive spoken overview of {topic.title} in "
            f"{settings.KNOWLEDGE_OVERVIEW_SENTENCES} sentences: who they were or what it "
            "was, when and where, what they are best known for, and one notable detail "
            "that makes them memorable. Use ONLY the article text above — never add a "
            "name, date, place or number that is not in it, and do not speculate. "
            "Leave out anything the article doesn't cover rather than remarking on it, "
            "and never mention 'the article' or 'the text'. Plain spoken sentences: no "
            "lists, no headings, no pronunciation guides, no URLs."
        )

    def followup_context(self, topic: Topic, question: str) -> str:
        facts = topic.wait_facts(settings.KNOWLEDGE_FACTS_WAIT)
        more = _is_more(question)
        picked = self._select(topic, question, more)
        lines = [f"Encyclopedia article: {topic.title}"
                 + (f" ({topic.description})" if topic.description else "")]
        if facts:
            lines.append("Facts:")
            lines += [f"- {f}" for f in facts]
        if picked:
            lines.append("Passages:")
            lines += [f"[{p.section}] {p.text}" for p in picked]
        else:
            lines.append(_cap(topic.lead, settings.KNOWLEDGE_LEAD_CHARS))
        if more:
            lines.append(
                f"\nThe user wants to hear more about {topic.title}. In 2-3 spoken "
                "sentences, share something from the passages above that the overview "
                "did not already cover. Use ONLY the text above."
            )
        else:
            lines.append(
                f"\nThe user is asking a follow-up about {topic.title} — 'she', 'he', "
                f"'they', 'it', 'her', 'his' and 'their' mean {topic.title}. Answer in 1-2 "
                "spoken sentences using ONLY the facts and passages above. Never add a "
                "name, date, place or number that is not in them, and do not guess. If "
                f"they don't contain the answer, say you don't have that detail about "
                f"{topic.title}."
            )
        return "\n".join(lines)

    def _select(self, topic: Topic, question: str, more: bool) -> list:
        """The passages most likely to answer `question`."""
        q = _fold(question)
        terms = {w for w in re.findall(r"[a-z0-9']{3,}", q) if w not in _STOP}
        for pattern, words in _EXPANSIONS.items():
            if re.search(pattern, q):
                terms.update(words)
        stems = {t[:5] for t in terms}

        def score(i_p):
            i, p = i_p
            body = _fold(p.text)
            head = _fold(p.section)
            tokens = set(re.findall(r"[a-z0-9']{3,}", body))
            hits = sum(1 for s in stems if any(t.startswith(s) for t in tokens))
            hits += 2 * sum(1 for s in stems if s in head)
            return hits

        ranked = sorted(enumerate(topic.passages), key=score, reverse=True)
        if more:
            fresh = [(i, p) for i, p in enumerate(topic.passages) if i not in topic.used]
            ranked = [x for x in ranked if x[0] not in topic.used] if terms else fresh
        picked, total = [], 0
        for i, p in ranked:
            if not more and score((i, p)) == 0:
                break
            text = _cap(p.text, settings.KNOWLEDGE_PASSAGE_CHARS)
            if total + len(text) > settings.KNOWLEDGE_CONTEXT_CHARS:
                break
            picked.append(Passage(p.section, text))
            topic.used.add(i)
            total += len(text)
            if len(picked) >= 4:
                break
        return picked

