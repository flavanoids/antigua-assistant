#!/usr/bin/env python3
"""Tests for the knowledge skill (people, history, events via Wikipedia).

No network: parsing, follow-up detection, article splitting, title matching
and passage selection are pure; the pipeline test swaps in a fake provider
and records what the LLM would have been grounded in.

Run: python3 tests/test_knowledge.py   (also works under pytest)
"""

import sys
import tempfile
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

settings.configure({"household": [{"name": "Alex"}, {"name": "Katherine"}]})

from antigua_core import knowledge, pipeline  # noqa: E402
from antigua_core.intents.knowledge import (  # noqa: E402
    is_knowledge_followup,
    parse_knowledge_request,
    split_possessive,
)
from antigua_core.stores import ListStore, MemoryStore, TimerManager  # noqa: E402


PARSE_CASES = [
    ("who was Frida Kahlo", "Frida Kahlo"),
    ("Who is Bad Bunny?", "Bad Bunny"),
    ("who were the Beatles", "the Beatles"),
    ("tell me about the Cuban Missile Crisis", "the Cuban Missile Crisis"),
    ("can you tell me about Cesar Chavez", "Cesar Chavez"),
    ("what was Watergate", "Watergate"),
    ("what was the Battle of Hastings", "the Battle of Hastings"),
    ("what happened at Chernobyl", "Chernobyl"),
    ("the history of Mexico", "history of Mexico"),
    ("who was Frida Kahlo's husband", "Frida Kahlo's husband"),
    ("quién fue Frida Kahlo", "Frida Kahlo"),
    ("háblame de Selena", "Selena"),
    # Not subjects: roles, live facts, the assistant, the house, definitions.
    ("who is the CEO of Starbucks", None),
    ("who was the first president of mexico", None),
    ("who is Taylor Swift dating", None),
    ("who is playing tonight", None),
    ("what is the capital of France", None),
    ("what was that", None),
    ("tell me about yourself", None),
    ("tell me about your day", None),
    ("who is Alex", None),
    ("explain how to reset the router", None),
    ("who is the latest James Bond", None),
]

FOLLOWUP_CASES = [
    ("was she married?", True),
    ("what's her hometown", True),
    ("what were her notable achievements", True),
    ("and where was she born", True),
    ("did they have kids", True),
    ("tell me more", True),
    ("was Frida married", True),
    ("¿estaba casada?", True),
    ("can you turn it off", False),
    ("how do you do it", False),
    ("turn off the lights", False),
    ("set a timer for five minutes", False),
]


def _check_parse():
    failed = 0
    for text, want in PARSE_CASES:
        got = parse_knowledge_request(text)
        if got != want:
            failed += 1
            print(f"[FAIL] parse {text!r}: {got!r}, expected {want!r}")
    assert split_possessive("Frida Kahlo's husband") == ("Frida Kahlo", "husband")
    assert split_possessive("the Beatles") == ("the Beatles", None)
    for text, want in FOLLOWUP_CASES:
        if is_knowledge_followup(text, "Frida Kahlo") != want:
            failed += 1
            print(f"[FAIL] follow-up {text!r}: expected {want}")
    return failed


ARTICLE = """Magdalena Carmen Frida Kahlo y Calderón (Spanish pronunciation: [ˈfɾiða ˈkalo]; 6 July 1907 – 13 July 1954) was a Mexican painter known for her many portraits and self-portraits.
She is also known for painting about her experience of chronic pain.

== Biography ==

=== Early life ===
Kahlo was born on 6 July 1907 in Coyoacán, a village on the outskirts of Mexico City, at her family home.

=== Marriage ===
Kahlo and Rivera were married in a civil ceremony at the town hall of Coyoacán on 21 August 1929.

== Legacy ==
The Tate Modern considers Kahlo one of the most significant artists of the twentieth century.

== References ==
Herrera, Hayden (1983). Frida: A Biography of Frida Kahlo. Harper and Row, New York.
"""


def _topic():
    lead, passages = knowledge._split_article(ARTICLE)
    t = knowledge.Topic(title="Frida Kahlo", description="Mexican painter (1907–1954)",
                        lead=lead, passages=passages)
    t.facts = ["Spouse: Diego Rivera", "Born in: Coyoacán"]
    t.facts_ready.set()
    return t


def _check_article():
    t = _topic()
    assert "pronunciation" not in t.lead and "[" not in t.lead, t.lead
    assert "6 July 1907" in t.lead, t.lead
    sections = [p.section for p in t.passages]
    assert sections == ["Biography / Early life", "Biography / Marriage", "Legacy"], sections

    wk = knowledge.WikiKnowledge(timeout=1)
    ctx = wk.followup_context(t, "was she married?")
    assert "Spouse: Diego Rivera" in ctx and "[Biography / Marriage]" in ctx, ctx
    assert ctx.index("Marriage]") < ctx.index("Early life]") if "Early life]" in ctx else True
    ctx = wk.followup_context(_topic(), "what's her hometown")
    assert "[Biography / Early life]" in ctx, ctx
    assert "Frida Kahlo" in wk.overview_context(t)

    assert knowledge._title_fits("Frida Kahlo", "Frida Kahlo")
    assert knowledge._title_fits("Frida Callo", "Frida Kahlo")        # misheard surname
    assert knowledge._title_fits("Selena Quintanilla", "Selena", top=True)
    assert knowledge._title_fits("MLK", "Martin Luther King Jr.", redirect="MLK Jr.")
    assert not knowledge._title_fits("free the call", "Toll-free telephone number")
    assert not knowledge._title_fits("Selena Quintanilla", "Selena")  # not the top hit
    assert knowledge._title_fits("AOC", "Alexandria Ocasio-Cortez")      # initials
    assert knowledge._title_fits("AOC", "Alexandria Ocasio-Cortez", redirect="A.O.C.")
    assert knowledge._title_fits("JFK", "John F. Kennedy")
    assert knowledge._title_fits("Mamdani", "Zohran Mamdani")
    # An alias only counts when it *is* what was asked.
    assert knowledge._title_fits("the Rock", "Dwayne Johnson", redirect="The Rock (wrestler)")
    assert not knowledge._title_fits("Prince", "Charles III", redirect="Prince Charles, Prince of Wales")
    assert knowledge._title_fits("MLK", "Martin Luther King Jr.")
    # Only a plainly titled article is the primary topic for a name.
    assert knowledge._is_primary("Selena", "Selena")
    assert knowledge._is_primary("the Beatles", "The Beatles")
    assert not knowledge._is_primary("Prince", "Prince (musician)")
    assert not knowledge._is_primary("Selena", "Selena Gomez")
    assert not knowledge._is_primary("Frida", "Anni-Frid Lyngstad")
    for cat, ok in [("Category:Living people", True), ("Category:1907 births", True),
                    ("Category:English rock music groups", True), ("Category:Rock music", False),
                    ("Categoría:Nacidos en Coyoacán", True)]:
        assert bool(knowledge._PERSON_CAT_RE.match(cat)) == ok, cat
    assert knowledge._WORK_CAT_RE.match("Category:2018 superhero films")
    assert not knowledge._WORK_CAT_RE.match("Category:American film actresses")
    assert not knowledge._WORK_CAT_RE.match("Category:Anti-racist organizations in the United States")
    from antigua_core.intents.knowledge import who_kind
    assert who_kind("who was Prince") == "person" and who_kind("who's Drake") == "person"
    assert who_kind("who were the Aztecs") == "group" and who_kind("quién fue Selena") == "person"
    assert who_kind("tell me about Watergate") is None

    assert knowledge._format_time({"time": "+1907-07-06T00:00:00Z", "precision": 11}) == "July 6, 1907"
    assert knowledge._format_time({"time": "-0044-00-00T00:00:00Z", "precision": 9}) == "44 BC"
    return 0


class _FakeKnowledge(knowledge.WikiKnowledge):
    def __init__(self):
        super().__init__(timeout=1)
        self.asked = []

    def lookup(self, subject, lang="en", who=None):
        self.asked.append(subject)
        return _topic() if "frida" in subject.lower() else None


def _check_pipeline():
    tmp = Path(tempfile.mkdtemp(prefix="antigua_test_"))
    seen = {}

    def ask_llm_stream(transcript, **kw):
        seen.clear()
        seen.update(kw, transcript=transcript)
        yield "Okay."

    def synthesize(text, lang="en"):
        p = tmp / "out.wav"
        p.write_bytes(b"RIFF")
        return str(p)

    fake = _FakeKnowledge()
    pipeline.init(pipeline.Backend(
        transcribe=lambda p: {"text": "", "time_s": 0.0},
        synthesize=synthesize,
        ask_llm_stream=ask_llm_stream,
        audio_url_base=lambda: "http://test:0",
        memory_store=MemoryStore(path=tmp / "memories.json"),
        list_store=ListStore(path=tmp / "lists.json"),
        timers=TimerManager(),
        weather_cache=None,
        news_cache=None,
        knowledge=fake,
    ))
    settings.SEARCH_ENABLED = False

    def say(text, conv):
        seen.clear()
        pipeline.dispatch_text(text, conversation_id=conv, quiet=True)
        return seen

    kw = say("who was Frida Kahlo", "c1")
    assert fake.asked == ["Frida Kahlo"], fake.asked
    assert "overview of Frida Kahlo" in kw["extra_context"], kw
    assert kw["grounding_context"] == kw["extra_context"]
    assert kw["max_tokens_override"] == settings.KNOWLEDGE_MAX_TOKENS

    # A later wake word is a new conversation id — still the same subject.
    kw = say("was she married?", "c2")
    assert "follow-up about Frida Kahlo" in kw["extra_context"], kw
    assert "Spouse: Diego Rivera" in kw["extra_context"]

    # "how did she die" is claimed by memory_query; the fresh subject wins it.
    kw = say("how did she die", "c3")
    assert "follow-up about Frida Kahlo" in (kw["extra_context"] or ""), kw

    # ...but pills are about the house (the medicine skill answers it).
    kw = say("did she take her pills", "c4")
    assert "Frida" not in (kw.get("extra_context") or ""), kw

    # Possessive: look up Frida, answer the husband question directly.
    kw = say("who was Frida Kahlo's husband", "c5")
    assert fake.asked[-1] == "Frida Kahlo", fake.asked
    assert "follow-up about Frida Kahlo" in kw["extra_context"], kw

    # No article that fits: nothing invented, no stale grounding.
    kw = say("who was Zyxwv Qqq", "c6")
    assert kw.get("grounding_context") is None, kw

    # A failed lookup clears the old subject: "his albums" isn't about Frida.
    say("who was Frida Kahlo", "c8")
    say("who is Zyxwv Qqq", "c9")
    kw = say("what are his biggest albums", "c10")
    assert "Frida" not in (kw.get("extra_context") or ""), kw
    say("who was Frida Kahlo", "c11")

    # The subject expires.
    pipeline._last_knowledge["at"] -= settings.KNOWLEDGE_TOPIC_TTL + 1
    kw = say("was she married?", "c7")
    assert "Frida" not in (kw.get("extra_context") or ""), kw
    return 0


def run():
    failed = _check_parse() + _check_article() + _check_pipeline()
    if failed:
        print(f"\n{failed} check(s) failed")
        sys.exit(1)
    print("All knowledge checks passed")


def test_knowledge():
    run()


if __name__ == "__main__":
    run()
