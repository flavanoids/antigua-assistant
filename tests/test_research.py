"""Research (research.py): what asks for it, the evidence the code builds
(climate record, price list), the grounding of the answer, and the pipeline's
"ack now, answer later".

Run: python3 tests/test_research.py   (also works under pytest)
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core

from antigua_core import netstatus, pipeline, research, settings  # noqa: E402
from antigua_core.research import Source  # noqa: E402

from test_pipeline import make_backend  # noqa: E402


def test_parse():
    cases = {
        "research Houston's weather patterns over the last 30 years":
            "Houston's weather patterns over the last 30 years",
        "can you look into whether we should get a heat pump": "whether we should get a heat pump",
        "Antigua, dig into robot vacuums for pet hair": "robot vacuums for pet hair",
        "find the best price on a Ninja Creami": "find the best price on a Ninja Creami",
        "is the Ninja Swirl worth it?": "is the Ninja Swirl worth it",
        "compare the Pixel 11 and the iPhone 18": "compare the Pixel 11 and the iPhone 18",
        "should I get an air fryer or a toaster oven": "should I get an air fryer or a toaster oven",
        "what's the weather today": None,
        "look into the front door camera": None,
        "remind me to compare insurance quotes and car prices": None,
        "what's a Ninja Creami": None,
    }
    for text, want in cases.items():
        assert research.parse(text) == want, (text, research.parse(text))


def test_summarize_climate():
    days, highs, lows, rain = [], [], [], []
    for year, hot in ((1995, 92.0), (2023, 98.0)):
        for month in range(1, 13):
            for day in (1, 15):
                days.append(f"{year}-{month:02d}-{day:02d}")
                highs.append(hot if month in (6, 7, 8) else 70.0)
                lows.append(80.0 if month == 7 and year == 2023 else 50.0)
                rain.append(2.5 if month == 5 else 0.1)
    text = research.summarize_climate("Test Airport", days, highs, lows, rain)
    assert "1990s (1995-1995): summer average high 92.0°F" in text, text
    assert "2020s (2023-2023): summer average high 98.0°F" in text, text
    assert "6 days a year at 95°F or hotter" in text, text           # 2023's summer days
    assert "2 nights a year that stayed 78°F or warmer" in text, text
    assert "2.0 days a year with 2 inches or more" in text, text
    assert "Change from the 1990s to the 2020s" not in text, text    # one year each: too few
    years = range(1991, 2026)
    days = [f"{y}-07-01" for y in years]
    text = research.summarize_climate("T", days, [90 + (y > 2019) * 4 for y in years],
                                      [70] * len(days), [5 - (y > 2019) * 2 for y in years])
    assert "summer average high +4.0°F (hotter)" in text, text
    assert "yearly rain -2.0 inches (drier)" in text, text


def test_price_mentions():
    s = [Source("https://www.walmart.com/ip/x", "Ninja", "Ninja CREAMi NC301 now $179.00, was $199.00"),
         Source("https://deals.example.com/a", "Deals", "No price here")]
    lines = research.price_mentions(s)
    assert lines[0].startswith("walmart.com: $179.00 (Ninja CREAMi NC301"), lines
    assert len(lines) == 2


def test_grounded():
    ev = "walmart.com: $179.00 Ninja CREAMi NC301"
    got = research._grounded("Right now Walmart has it for $179.00. Target has it for $149.", ev)
    assert got == "Right now Walmart has it for $179.00.", got


def _llm(answers):
    """A stand-in for write_json: the plan, then each answer in turn."""
    replies = iter(answers)
    prompts = []

    def llm(prompt):
        prompts.append(prompt)
        return json.dumps(next(replies))
    llm.prompts = prompts
    return llm


def _stub_web(monkey):
    calls = []

    def web_search(objective, queries):
        calls.append(queries)
        if queries == ["creami costco"]:
            return [Source("https://www.costco.com/p", "Costco", "Ninja CREAMi NC301 $159.99")]
        return [Source("https://www.walmart.com/ip/x", "Walmart", "Ninja CREAMi NC301 $179.00")]
    monkey["web_search"] = research.web_search
    monkey["read_pages"] = research.read_pages
    research.web_search = web_search
    research.read_pages = lambda urls, objective: {}
    return calls


def _restore(monkey):
    for k, v in monkey.items():
        setattr(research, k, v)


def test_run_with_one_more_search():
    monkey = {}
    calls = _stub_web(monkey)
    try:
        llm = _llm([
            {"kind": "price", "subject": "Ninja Creami", "queries": ["ninja creami price"]},
            {"answer": "", "need": "creami costco"},
            {"answer": "Costco has the Ninja CREAMi NC301 for $159.99. Amazon has it for $99.",
             "sources": "costco.com"},
        ])
        res = research.run("find the best price on a Ninja Creami", llm)
    finally:
        _restore(monkey)
    assert calls == [["ninja creami price"], ["creami costco"]], calls
    assert res.answer == "Costco has the Ninja CREAMi NC301 for $159.99.", res.answer  # Amazon's dropped
    assert res.sources == ["costco.com"] and res.kind == "price"
    assert "costco.com: $159.99" in llm.prompts[2] and research._NEED_RULE not in llm.prompts[2]


def test_run_never_raises():
    def llm(prompt):
        raise RuntimeError("ollama down")
    res = research.run("research heat pumps", llm)
    assert res.answer == "Sorry, my research on that fell through."


def test_pipeline_ack_then_answer():
    monkey = {}
    _stub_web(monkey)
    published = []
    real_publish, real_online = settings.mqtt_publish, netstatus.online
    settings.mqtt_publish = lambda topic, payload: published.append((topic, payload))
    netstatus.online = lambda: True
    settings.SEARCH_ENABLED = True
    research._runs.clear()
    try:
        be = make_backend("")
        be.write_json = _llm([
            {"kind": "price", "subject": "Ninja Creami", "queries": ["ninja creami price"]},
            {"answer": "Walmart has the Ninja CREAMi NC301 for $179.00.", "need": ""},
        ])
        spoken = []
        real_synth = be.synthesize
        be.synthesize = lambda text, lang="en": spoken.append(text) or real_synth(text, lang)
        pipeline.init(be)
        r = pipeline.dispatch_text("find the best price on a Ninja Creami", conversation_id="r1")
        assert r["response"] == "I'll shop around and get back to you.", r
        assert r["action"] == "research"
        for _ in range(50):
            if not pipeline._research_running:
                break
            time.sleep(0.05)
        assert "Walmart has the Ninja CREAMi NC301 for $179.00." in spoken, spoken
        assert any(t == "antigua/play" for t, _ in published), published
        # A quiet (text) turn waits and answers directly.
        be.write_json = _llm([
            {"kind": "general", "subject": "x", "queries": ["x"]},
            {"answer": "Walmart has it for $179.00.", "need": ""},
        ])
        r = pipeline.dispatch_text("research the Ninja Creami", conversation_id="r2", quiet=True)
        assert r["response"] == "Walmart has it for $179.00.", r
    finally:
        _restore(monkey)
        settings.mqtt_publish, netstatus.online = real_publish, real_online


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
