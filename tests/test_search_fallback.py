"""SearXNG -> Parallel fallback: when SearXNG comes back empty or fails, the
same {title, content, published} rows come from Parallel's Search API (with
a key) or its public MCP endpoint (without).

Run: python3 tests/test_search_fallback.py   (also works under pytest)
"""

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
import json  # noqa: E402
import requests  # noqa: E402

from antigua_core import settings  # noqa: E402
from antigua_core.caches import SearXNGSkill  # noqa: E402


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


PARALLEL = {"results": [
    {"url": "https://example.test/toilet", "title": "How to Fix a Running Toilet",
     "publish_date": "2025-03-02T00:00:00", "excerpts": ["Check the flapper first.", "Then the fill valve."]},
    {"url": "https://example.test/blank", "title": "No excerpts", "excerpts": []},
]}


@contextmanager
def _patch(searx, parallel, key="k", keyless=True):
    """searx: a dict to answer with, or an exception to raise; parallel
    likewise. Puts the real requests functions and settings back after."""
    calls = {"get": 0, "post": []}

    def get(url, params=None, **kw):
        calls["get"] += 1
        if isinstance(searx, Exception):
            raise searx
        return _Resp(searx)

    def post(url, headers=None, json=None, **kw):
        calls["post"].append((url, headers, json))
        if isinstance(parallel, Exception):
            raise parallel
        return _Resp(parallel)

    real = requests.get, requests.post, settings.PARALLEL_API_KEY, settings.PARALLEL_KEYLESS
    requests.get, requests.post = get, post
    settings.PARALLEL_API_KEY, settings.PARALLEL_KEYLESS = key, keyless
    try:
        yield calls
    finally:
        requests.get, requests.post, settings.PARALLEL_API_KEY, settings.PARALLEL_KEYLESS = real


def test_empty_searxng_falls_back():
    with _patch({"results": []}, PARALLEL) as calls:
        answer, results = SearXNGSkill().search("how do I fix a running toilet", wiki=False)
    assert answer is None
    assert results == [{"title": "How to Fix a Running Toilet",
                        "content": "Check the flapper first. Then the fill valve.",
                        "published": "2025-03-02"}], results
    url, headers, body = calls["post"][0]
    assert url == settings.PARALLEL_URL and headers == {"x-api-key": "k"}
    assert body["search_queries"] == ["how do I fix a running toilet"]
    assert body["mode"] == settings.PARALLEL_MODE


def test_searxng_down_falls_back():
    with _patch(requests.ConnectionError("refused"), PARALLEL):
        _answer, results = SearXNGSkill().search("how do I fix a running toilet", wiki=False)
    assert results and results[0]["title"] == "How to Fix a Running Toilet"


def test_results_from_searxng_skip_parallel():
    with _patch({"results": [{"title": "Fix it", "content": "Flapper.", "url": "u"}]}, PARALLEL) as calls:
        _answer, results = SearXNGSkill().search("running toilet", wiki=False)
    assert results[0]["title"] == "Fix it" and calls["post"] == []


def test_parallel_failing_or_switched_off_is_quiet():
    with _patch({"results": []}, requests.Timeout("slow")):
        assert SearXNGSkill().search("running toilet", wiki=False) == (None, [])
    with _patch({"results": []}, PARALLEL, key="", keyless=False) as calls:
        assert SearXNGSkill().search("running toilet", wiki=False) == (None, [])
        assert calls["post"] == []
    # With the fallback off a SearXNG failure still raises, as before.
    with _patch(requests.ConnectionError("refused"), PARALLEL, key="", keyless=False):
        try:
            SearXNGSkill().search("running toilet", wiki=False)
        except requests.ConnectionError:
            pass
        else:
            raise AssertionError("expected the SearXNG error")


def test_keyless_uses_the_public_mcp_endpoint():
    mcp = {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": json.dumps(PARALLEL)}]}}
    with _patch({"results": []}, mcp, key="") as calls:
        _answer, results = SearXNGSkill().search("how do I fix a running toilet", wiki=False)
    assert results and results[0]["content"] == "Check the flapper first. Then the fill valve."
    url, headers, body = calls["post"][0]
    assert url == settings.PARALLEL_MCP_URL and "x-api-key" not in (headers or {})
    assert body["method"] == "tools/call" and body["params"]["name"] == "web_search"
    assert body["params"]["arguments"]["search_queries"] == ["how do I fix a running toilet"]
    # A JSON-RPC error is a quiet miss, not a crash.
    with _patch({"results": []}, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}}, key=""):
        assert SearXNGSkill().search("running toilet", wiki=False) == (None, [])


def main():
    settings.configure({})
    test_empty_searxng_falls_back()
    test_searxng_down_falls_back()
    test_results_from_searxng_skip_parallel()
    test_parallel_failing_or_switched_off_is_quiet()
    test_keyless_uses_the_public_mcp_endpoint()
    print("PASS — search fallback suite")


if __name__ == "__main__":
    main()
