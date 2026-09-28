#!/usr/bin/env python3
"""Tests for the news skill: multi-source interleaving (caches.NewsCache) and
the no-hallucination guard (pipeline._handle_news).

No network — NewsCache is exercised with synthetic per-source items injected
directly into its cache dict, and the pipeline is driven with a stub Backend
(same pattern as test_pipeline.py).

Run: python3 tests/test_news.py   (also works under pytest)
"""

import sys
import tempfile
import time
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import pipeline, settings  # noqa: E402
from antigua_core.caches import NewsCache  # noqa: E402
from antigua_core.stores import ListStore, MemoryStore, TimerManager  # noqa: E402


def _item(source, n):
    return {"title": f"{source} headline {n}", "summary": "", "source": source, "published": ""}


def _seed(nc, source, count):
    nc._cache[source] = ([_item(source, i) for i in range(count)], time.time())


def _check_interleaving():
    failed = 0
    nc = NewsCache(sources=[
        {"name": "a"}, {"name": "b"}, {"name": "c"},
    ], ttl=1200, max_items=5)
    # Bypass the network entirely — inject already-fresh cache entries.
    nc._sources = {"a": "http://x", "b": "http://x", "c": "http://x"}
    nc._general_names = ["a", "b", "c"]
    _seed(nc, "a", 5)
    _seed(nc, "b", 5)
    _seed(nc, "c", 5)

    items = nc.get()
    sources_in_order = [i["source"] for i in items[:5]]
    if sources_in_order != ["a", "b", "c", "a", "b"]:
        failed += 1
        print(f"[FAIL] general pool should interleave a/b/c, got: {sources_in_order}")
    else:
        print(f"[ok] general pool interleaves sources: {sources_in_order}")

    # Uneven source counts — shorter sources shouldn't leave gaps.
    nc2 = NewsCache(sources=[{"name": "x"}, {"name": "y"}], ttl=1200, max_items=5)
    nc2._sources = {"x": "http://x", "y": "http://x"}
    nc2._general_names = ["x", "y"]
    _seed(nc2, "x", 1)
    _seed(nc2, "y", 5)
    items = nc2.get()
    if any(i is None for i in items) or len(items) != 6:
        failed += 1
        print(f"[FAIL] uneven interleave produced gaps/wrong length: {items}")
    else:
        print(f"[ok] uneven source counts interleave without gaps: {[i['source'] for i in items]}")

    return failed


def _check_no_headlines_message():
    failed = 0
    nc = NewsCache(sources=[{"name": "a"}], ttl=1200, max_items=5)
    nc._sources = {"a": "http://x"}
    nc._general_names = ["a"]
    _seed(nc, "a", 0)
    msg = nc.format_for_prompt()
    if msg != "No headlines currently available.":
        failed += 1
        print(f"[FAIL] empty pool message: {msg!r}")
    else:
        print(f"[ok] empty pool message: {msg!r}")
    return failed


# ── pipeline._handle_news: no-hallucination guard ───────────────────────────

class _FakeSearx:
    def search(self, query, **kw):
        return None, []

    def search_best(self, query, **kw):
        return None, [], query

    def format_for_prompt(self, *a, **kw):
        return ""


class _FakeNewsCache:
    SOURCE_LABELS = {"reuters": "Reuters"}

    def __init__(self, reply, items=None):
        self._reply = reply
        self._items = items or []

    def top_items(self, *a, **kw):
        return self._items

    def format_for_prompt(self, *a, **kw):
        return self._reply


def _make_backend(news_cache, llm_sentences=("(should not be called)",)):
    tmp = Path(tempfile.mkdtemp(prefix="antigua_news_test_"))

    def synthesize(text, lang="en"):
        p = tmp / "out.wav"
        p.write_bytes(b"RIFF")
        return str(p)

    def ask_llm_stream(*a, **kw):
        yield from llm_sentences

    return pipeline.Backend(
        transcribe=lambda path: {"text": "", "time_s": 0.0, "confidence": 1.0},
        synthesize=synthesize,
        ask_llm_stream=ask_llm_stream,
        audio_url_base=lambda: "http://test:0",
        memory_store=MemoryStore(path=tmp / "memories.json"),
        list_store=ListStore(path=tmp / "lists.json"),
        timers=TimerManager(),
        weather_cache=None,
        news_cache=news_cache,
        searxng=_FakeSearx(),
    )


def _check_hallucination_guard():
    failed = 0

    # No headlines available -> answered directly, LLM never touched.
    pipeline.init(_make_backend(_FakeNewsCache("No headlines currently available.")))
    r = pipeline.dispatch_text("what's in the news")
    if r["response"] != "No headlines currently available." or r.get("streaming"):
        failed += 1
        print(f"[FAIL] empty-news guard did not short-circuit: {r}")
    else:
        print(f"[ok] empty-news guard short-circuits: {r['response']!r}")

    reuters_item = {"title": "OpenAI IPO will not happen in 2026, Altman says",
                     "summary": "", "source": "reuters", "published": ""}

    # Real headlines -> falls through to the LLM tail (streaming response),
    # and stashes the real item(s) for a possible follow-up.
    pipeline.init(_make_backend(
        _FakeNewsCache("Current news headlines:\n1. [Reuters] Something happened.",
                       items=[reuters_item]),
        llm_sentences=("Reuters says something happened.",),
    ))
    conv_id = "news-followup-test"
    r = pipeline.dispatch_text("what's in the news", conversation_id=conv_id)
    if not r.get("streaming") or "something happened" not in r["response"].lower():
        failed += 1
        print(f"[FAIL] real headlines should reach the LLM tail: {r}")
    else:
        print(f"[ok] real headlines reach the LLM tail: {r['response']!r}")

    # Bare "sports news" -> redirected to the sports skill, no LLM, no RSS lookup.
    pipeline.init(_make_backend(_FakeNewsCache("should not be called")))
    r = pipeline.dispatch_text("sports news")
    if "sports" not in r["response"].lower() or r.get("streaming"):
        failed += 1
        print(f"[FAIL] sports-news redirect: {r}")
    else:
        print(f"[ok] sports-news redirect: {r['response']!r}")

    # Follow-up ("tell me more") re-grounds in the actual stashed item instead
    # of answering from bare conversation history — the fix for the observed
    # fabrication (fake OpenAI funding numbers, fake Iran/Saudi backstory).
    seen_context = {}

    def capturing_llm_stream(transcript, extra_context=None, **kw):
        seen_context["value"] = extra_context
        yield "That's what's in the headline, I don't have more on it."

    backend = _make_backend(_FakeNewsCache("n/a", items=[reuters_item]))
    backend.ask_llm_stream = capturing_llm_stream
    pipeline.init(backend)
    r = pipeline.dispatch_text("tell me more about the openai ipo story",
                                conversation_id=conv_id, follow_up=True)
    ctx = seen_context.get("value") or ""
    if "OpenAI IPO" not in ctx:
        failed += 1
        print(f"[FAIL] news follow-up did not re-ground in the stashed item: {ctx!r}")
    else:
        print(f"[ok] news follow-up re-grounds in the real item: {ctx!r}")

    # Explicit no-match signal: a follow-up on something not in the stashed
    # items must say so in the context, not silently hand back everything and
    # trust the LLM to notice on its own.
    r = pipeline.dispatch_text("tell me more about the iran saudi arabia story",
                                conversation_id=conv_id, follow_up=True)
    ctx = seen_context.get("value") or ""
    if "None of these headlines match" not in ctx or "don't guess" not in ctx.lower():
        failed += 1
        print(f"[FAIL] no-match case should say so explicitly: {ctx!r}")
    else:
        print(f"[ok] no-match case is explicit: {ctx!r}")

    # Follow-up scaffolding words ("story", "more", "tell", "else") must not
    # count as match signal — two items sharing no real keyword should not
    # spuriously "match" on those.
    astros_item = {"title": "Astros beat the Rays in a story of redemption",
                   "summary": "", "source": "aljazeera", "published": ""}
    backend2 = _make_backend(_FakeNewsCache("n/a", items=[reuters_item, astros_item]))
    backend2.ask_llm_stream = capturing_llm_stream
    pipeline.init(backend2)
    pipeline.dispatch_text("what's in the news", conversation_id=conv_id)
    r = pipeline.dispatch_text("tell me more about that story", conversation_id=conv_id, follow_up=True)
    ctx = seen_context.get("value") or ""
    if "None of these headlines match" not in ctx:
        failed += 1
        print(f"[FAIL] bare 'story' should not spuriously match: {ctx!r}")
    else:
        print(f"[ok] follow-up scaffolding words don't create a false match: {ctx!r}")

    return failed


def run():
    settings.configure({})
    settings.SEARCH_ENABLED = False
    failed = 0
    failed += _check_interleaving()
    failed += _check_no_headlines_message()
    failed += _check_hallucination_guard()

    if failed:
        print(f"\n{failed} check(s) failed")
        sys.exit(1)
    print("\nAll news checks passed")


if __name__ == "__main__":
    run()
