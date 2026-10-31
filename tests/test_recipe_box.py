#!/usr/bin/env python3
"""The recipe box (antigua_core.recipe_box): favorites and history, and the
/recipes routes PinedaDisplay's phone menu uses, over real HTTP on a
throwaway port.

Run: python3 tests/test_recipe_box.py   (also works under pytest)
"""

import json
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import requests  # noqa: E402

from antigua_core import settings  # noqa: E402

settings.configure({})

from antigua_core import recipe_box  # noqa: E402
from antigua_core.recipe import Recipe  # noqa: E402
from antigua_core.recipe_http import RecipeRoutes  # noqa: E402


def _recipe(n, title, source="Test Kitchen", images=None):
    return Recipe.from_dict(dict(
        title=title, source=source, url=f"https://example.test/{n}", servings="serves 4", total_min=45,
        rating=4.8, rating_count=120, ingredients=["1 cup flour", "2 eggs"],
        steps=["Mix.", "Bake for 30 minutes."], images=images or [f"https://example.test/{n}.jpg"]))


PIE, BREAD, SOUP = _recipe(1, "Classic Apple Pie"), _recipe(2, "Banana Bread"), _recipe(3, "Chicken Soup")


def test_history_and_favorites():
    box = recipe_box.RecipeBox(Path(tempfile.mkdtemp()) / "box.json")
    assert box.api() == {"favorites": [], "history": []}
    pie = box.note_opened("apple pie", PIE, "Sam")
    box.note_opened("apple pie", PIE, "Sam")                # again the same evening: one entry
    box.note_opened("banana bread", BREAD)
    assert [h["id"] for h in box.api()["history"]] == [recipe_box.recipe_id(BREAD), pie]
    box.note_cooked(PIE)
    hist = box.api()["history"]
    assert [h["cooked"] for h in hist] == [False, True] and hist[1]["by"] == "Sam"
    assert hist[1]["url"] == "https://example.test/1" and hist[1]["ingredients"] == ["1 cup flour", "2 eggs"]
    assert hist[1]["image"] == "https://example.test/1.jpg" and hist[1]["dish"] == "apple pie"
    assert box.last_opened().title == "Banana Bread"

    assert box.add_favorite(PIE, "Sam") and not box.add_favorite(PIE)
    box.add_favorite(SOUP)                                  # never opened: kept all the same
    assert [r.title for r in box.favorites()] == ["Chicken Soup", "Classic Apple Pie"]
    assert [r.title for r in box.favorites("apple pie")] == ["Classic Apple Pie"]
    assert [r.title for r in box.favorites("pie")] == ["Classic Apple Pie"]
    assert box.favorites("lasagna") == []
    assert [f["favorite"] for f in box.api()["favorites"]] == [True, True]
    assert box.api()["history"][1]["favorite"] is True
    assert [r.title for r in box.recent(4)] == ["Banana Bread", "Classic Apple Pie"]

    # Kept across a restart, and a recipe nothing points at any more is dropped.
    again = recipe_box.RecipeBox(box.path)
    assert [r.title for r in again.favorites()] == ["Chicken Soup", "Classic Apple Pie"]
    assert again.remove_favorite(SOUP) and not again.remove_favorite(SOUP)
    assert recipe_box.recipe_id(SOUP) not in json.loads(box.path.read_text())["recipes"]
    assert again.favorite_id(pie, False) and not again.favorites()
    assert not again.favorite_id("0123456789", True)        # not in the box


SHOWN = []


class Handler(RecipeRoutes, BaseHTTPRequestHandler):
    def recipe_show(self, rid):
        SHOWN.append(rid)
        return {"opened": rid, "shown": True} if recipe_box.box().get(rid) else None

    def log_message(self, *a):
        pass

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        self.route_recipes("GET") or self._json(404, {})

    def do_POST(self):
        self.route_recipes("POST") or self._json(404, {})

    def do_DELETE(self):
        self.route_recipes("DELETE") or self._json(404, {})


def test_http_routes():
    recipe_box._box = recipe_box.RecipeBox(Path(tempfile.mkdtemp()) / "box.json")
    rid = recipe_box.box().note_opened("apple pie", PIE)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        got = requests.get(f"{base}/recipes", timeout=5).json()
        assert got["favorites"] == [] and got["history"][0]["title"] == "Classic Apple Pie"
        assert requests.post(f"{base}/recipes/{rid}/favorite", timeout=5).json() == {"id": rid, "favorite": True}
        assert requests.get(f"{base}/recipes", timeout=5).json()["favorites"][0]["id"] == rid
        assert requests.delete(f"{base}/recipes/{rid}/favorite", timeout=5).status_code == 200
        assert requests.get(f"{base}/recipes", timeout=5).json()["favorites"] == []
        assert requests.post(f"{base}/recipes/{rid}/show", timeout=5).json()["opened"] == rid
        assert SHOWN == [rid]
        assert requests.post(f"{base}/recipes/0123456789/favorite", timeout=5).status_code == 404
        assert requests.post(f"{base}/recipes/0123456789/show", timeout=5).status_code == 404
        assert requests.get(f"{base}/recipes/../etc", timeout=5).status_code == 404
        assert requests.get(f"{base}/recipes/{rid}/favorite", timeout=5).status_code == 405
    finally:
        server.shutdown()


def main():
    test_history_and_favorites()
    test_http_routes()
    print("PASS — recipe box suite")


if __name__ == "__main__":
    main()
