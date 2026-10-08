"""Knowledge requests: people, history, events — and follow-ups about them.

"Who was Frida Kahlo", "tell me about the Cuban Missile Crisis", "what was
Watergate", "history of Mexico". The subject is looked up on Wikipedia
(knowledge.py) and answered only from the article, which stays on hand so a
later "was she married?" is answered from the same article instead of the 4B
model's memory.

Deliberately narrow: role questions ("who is the CEO of Starbucks", "who was
the first president of Mexico") and anything time-sensitive stay on the search
route — they want one current fact, not an encyclopedia entry.
"""

import re

from .. import household
from .search import _FRESHNESS_RE, is_live_event_query

_KNOWLEDGE_RE = re.compile(
    r"^\s*(?:(?:hey|ok|okay|so|and)\s+)?(?:(?:can|could)\s+you\s+)?(?:please\s+)?"
    r"(?:"
    r"(?P<who>who\s+(?:is|was|are|were)|who's)|"
    r"(?P<what>what\s+(?:is|was|were)|what's)|"
    r"(?P<tell>(?:tell|teach)\s+(?:me|us)\s+(?:about|of)|"
    r"(?:give\s+me|i\s+want)\s+(?:an?\s+)?(?:overview|summary|rundown|history)\s+(?:of|on|about)|"
    r"what\s+(?:do\s+you\s+know|can\s+you\s+tell\s+me)\s+about|"
    r"explain)|"
    r"(?P<happened>what\s+happened\s+(?:at|in|on|during|to|with))|"
    r"(?P<es>qui[eé]n\s+(?:es|fue|era|son|fueron)|"
    r"(?:h[aá]blame|cu[eé]ntame|dime)\s+(?:sobre|de|acerca\s+de)|"
    r"qu[eé]\s+(?:fue|era)|qu[eé]\s+pas[oó]\s+(?:en|con))"
    r")\s+(?P<subject>.+?)\s*[?.!]*\s*$",
    re.IGNORECASE,
)
# "the history of X" is a subject in its own right — Wikipedia has the article.
_HISTORY_RE = re.compile(
    r"^\s*(?:(?:what\s+is|what's)\s+)?(?:the\s+)?(?P<subject>history\s+of\s+.+?)\s*[?.!]*\s*$",
    re.IGNORECASE,
)

# Subjects that are the assistant, the room or the conversation, not a topic.
_NOT_A_SUBJECT_RE = re.compile(
    r"^(?:you|your|yourself|me|my|myself|us|our|it|its|this|that|these|those|"
    r"there|here|he|she|him|her|his|they|them|their|someone|somebody|anyone|"
    r"everyone|something|anything|everything|nothing|today|tonight|tomorrow|"
    r"how|why|what|when|where|which|whether|if|"
    r"yesterday|now|next|up|new|wrong|going\s+on|happening|antigua|"
    r"the\s+(?:weather|news|time|date|matter|deal|point|plan|problem|difference|"
    r"best|worst|score|song|movie|show)|"
    r"ella|[eé]l|su|sus|ellos|ellas|eso|esto)\b",
    re.IGNORECASE,
)
# Role questions want the person *in* the role, not an overview of the role:
# "who is the CEO of Starbucks", "who was the first president of Mexico".
_ROLE_RE = re.compile(r"^the\s+(?:\w+\s+){0,3}?(?:of|for|in|at|on|behind|from|who|that)\b",
                      re.IGNORECASE)
# "who is Taylor Swift dating" asks one live fact about the subject.
_QUESTION_TAIL_RE = re.compile(
    r"\b(?:dating|married\s+to|playing|doing|worth|related\s+to|coaching|"
    r"singing|wearing|saying|like|born|died|famous\s+for|known\s+for|from)\s*$",
    re.IGNORECASE,
)
# "who is playing tonight" is a schedule, not a subject.
_TIME_WORD_RE = re.compile(
    r"\b(?:today|tonight|tomorrow|yesterday|now|currently|this\s+(?:week|weekend|year|season|morning|afternoon|evening))\b",
    re.IGNORECASE,
)
# "Frida Kahlo's husband": look up Frida Kahlo, answer the husband question.
_POSSESSIVE_RE = re.compile(r"^(?P<subject>.+?)['’]s\s+(?P<attr>\w.*)$")


_PAST_YEAR_RE = re.compile(r"\b(?:1\d{3}|20[01]\d)s?\b")


def _clean(subject: str) -> str:
    subject = re.sub(r"\s+", " ", subject).strip(" ,.?!'\"")
    return re.sub(r"^(?:about|of|on)\s+", "", subject, flags=re.IGNORECASE)


def parse_knowledge_request(transcript: str) -> str | None:
    """The subject of a who-was / tell-me-about question, or None."""
    if not transcript or is_live_event_query(transcript):
        return None
    # "the latest iPhone" moves; "the 1960 election" doesn't.
    if _FRESHNESS_RE.search(transcript) and not _PAST_YEAR_RE.search(transcript):
        return None
    m = _HISTORY_RE.match(transcript)
    if m:
        return _clean(m.group("subject"))
    m = _KNOWLEDGE_RE.match(transcript)
    if not m:
        return None
    subject = _clean(m.group("subject"))
    words = subject.split()
    if not words or len(words) > 8 or _NOT_A_SUBJECT_RE.match(subject) \
            or _TIME_WORD_RE.search(subject):
        return None
    if household.canonical(words[0]) and len(words) <= 2:
        return None  # "who is <household member>" — a person in this house, not on Wikipedia
    if m.group("who") and _ROLE_RE.match(subject):
        return None
    if (m.group("who") or m.group("what")) and _QUESTION_TAIL_RE.search(subject):
        return None
    if m.group("what"):
        # "what is the capital of France", "what was that noise": a definition
        # or a fact, not an entity. Named things are capitalised by Whisper
        # ("the Cuban Missile Crisis", "Watergate").
        head = words[1] if words[0].lower() in ("the", "a", "an") and len(words) > 1 else words[0]
        if not head[:1].isupper():
            return None
    return subject


_SPANISH_ASK_RE = re.compile(
    r"^\s*¿?\s*(?:qui[eé]n|qu[eé]|h[aá]blame|cu[eé]ntame|dime)\b", re.IGNORECASE)


# "Who is/was X" is a person — never a thing ("the Rock" is not rock music).
# "Who were/are the X" may be a band, a people or a movement ("the Aztecs",
# "the Black Panthers"), which carry no biography categories — but never a
# film or an album.
_WHO_PERSON_RE = re.compile(
    r"^\s*¿?\s*(?:(?:hey|ok|okay|so|and)\s+)?(?:who\s+(?:is|was)\b|who's|qui[eé]n\s+(?:es|fue|era)\b)",
    re.IGNORECASE)
_WHO_GROUP_RE = re.compile(
    r"^\s*¿?\s*(?:(?:hey|ok|okay|so|and)\s+)?(?:who\s+(?:are|were)\b|qui[eé]n(?:es)?\s+(?:son|fueron|eran)\b)",
    re.IGNORECASE)


def who_kind(transcript: str) -> str | None:
    """"person", "group" or None — what kind of article a "who" question needs."""
    if _WHO_PERSON_RE.match(transcript):
        return "person"
    if _WHO_GROUP_RE.match(transcript):
        return "group"
    return None


def knowledge_language(transcript: str, language: str = "en") -> str:
    """Which Wikipedia to read: a Spanish question gets the Spanish article,
    so the answer's names and places are in the text it is checked against."""
    return "es" if language == "es" or _SPANISH_ASK_RE.match(transcript) else "en"


def split_possessive(subject: str) -> tuple:
    """("Frida Kahlo", "husband") for "Frida Kahlo's husband", else (subject, None)."""
    m = _POSSESSIVE_RE.match(subject)
    if m and m.group("subject")[:1].isupper():
        return m.group("subject"), m.group("attr")
    return subject, None


# ── Follow-ups ───────────────────────────────────────────────────────────────

# Third-person references to whatever was just described.
_PRONOUN_RE = re.compile(
    r"\b(?:she|he|they|her|his|him|hers|their|them|theirs|it|its|"
    r"ella|[eé]l|su|sus|ellos|ellas)\b",
    re.IGNORECASE,
)
_SECOND_PERSON_RE = re.compile(r"\b(?:you|your|yours|yourself)\b", re.IGNORECASE)
_MORE_RE = re.compile(
    r"\b(?:tell\s+me\s+more|more\s+about|what\s+else|anything\s+else|go\s+on|"
    r"keep\s+going|continue|more\s+details?|cu[eé]ntame\s+m[aá]s|qu[eé]\s+m[aá]s)\b",
    re.IGNORECASE,
)
_QUESTION_START_RE = re.compile(
    r"^\s*¿?\s*(?:(?:and|so|but|okay|ok|wait)\s+)?(?:(?:can|could)\s+you\s+tell\s+me\s+)?"
    r"(?:who|whom|whose|what|what's|when|where|why|how|which|did|does|do|was|were|is|"
    r"are|had|has|have|could|would|tell|name|list|give|qui[eé]n|qu[eé]|cu[aá]ndo|"
    r"d[oó]nde|c[oó]mo|por\s+qu[eé]|cu[aá]l|cu[aá]nt\w+|tuvo|ten[ií]a|fue|era|estuvo|"
    r"estaba|naci[oó]|muri[oó]|se\s+cas[oó])\b",
    re.IGNORECASE,
)
_TITLE_STOP = {"the", "of", "and", "de", "la", "el", "los", "las", "y", "von", "van", "der"}


def title_tokens(title: str) -> set:
    """Distinctive words of an article title: "Frida Kahlo" -> {frida, kahlo}."""
    title = re.sub(r"\s*\(.*?\)", "", title)  # "Selena (singer)" -> "Selena"
    return {w for w in re.findall(r"[a-záéíóúñü']{3,}", title.lower()) if w not in _TITLE_STOP}


def is_more_request(transcript: str) -> bool:
    return bool(_MORE_RE.search(transcript))


# "Did she take her pills?" is still about the house, not the article.
_HOUSEHOLD_RE = re.compile(
    r"\b(?:take|took|taken|pills?|medicine|meds|vitamins?|feed|fed|walk(?:ed)?|"
    r"eat|ate|say|said|tell|told|remember|leave|left|pick(?:ed)?\s+up|"
    r"today|tonight|this\s+morning|yesterday|last\s+night)\b",
    re.IGNORECASE,
)


def is_household_question(transcript: str) -> bool:
    return bool(_HOUSEHOLD_RE.search(transcript))


def is_knowledge_followup(transcript: str, title: str, language: str = "en") -> bool:
    """Is this turn asking about `title`, the subject just described?

    Only consulted for turns no skill claimed (the LLM and search routes), and
    only while a subject is fresh, so "was she married?" / "where was Frida
    from?" / "tell me more" land on the article rather than the model's memory.
    """
    if not transcript:
        return False
    if _MORE_RE.search(transcript):
        return True
    words = set(re.findall(r"[a-záéíóúñü']+", transcript.lower()))
    if words & title_tokens(title):
        return True
    if not _QUESTION_START_RE.search(transcript):
        return False
    if language == "es" or transcript.lstrip().startswith("¿"):
        return True  # Spanish drops the pronoun: "¿estaba casada?", "¿dónde nació?"
    if not _PRONOUN_RE.search(transcript):
        return False
    # "how do you do it" / "can you turn it off" are about the assistant or the
    # house; "it" alone is too common to claim when the user says "you".
    only_it = {p.lower() for p in _PRONOUN_RE.findall(transcript)} <= {"it", "its"}
    return not (only_it and _SECOND_PERSON_RE.search(transcript))
