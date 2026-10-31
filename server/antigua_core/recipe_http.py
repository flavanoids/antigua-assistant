"""The recipe box over HTTP, for PinedaDisplay's phone menu (Recipes tab).

GET /recipes lists favorites and history, each with its whole recipe and
page URL. POST/DELETE /recipes/<id>/favorite sets or clears the heart.
POST /recipes/<id>/show opens it here and puts its card on the kiosk, ready
for "Alexa, I'm ready". The primary only: the fallback doesn't keep the box.
"""

import re
from urllib.parse import urlparse

from . import recipe_box

_PATH_RE = re.compile(r"^/recipes(?:/(?P<id>[0-9a-f]{10})/(?P<what>favorite|show))?$")


class RecipeRoutes:
    """Needs `self._json(code, data)`, and `self.recipe_show(id)` (dict or None)."""

    def route_recipes(self, method: str) -> bool:
        """Handle the request if it's a /recipes one; True when it was."""
        path = urlparse(self.path).path
        if path != "/recipes" and not path.startswith("/recipes/"):
            return False
        m = _PATH_RE.match(path)
        if m is None:
            self._json(404, {"error": "not found"})
            return True
        rid, what = m.group("id"), m.group("what")
        box = recipe_box.box()
        if method == "GET" and rid is None:
            self._json(200, box.api())
        elif what == "favorite" and method in ("POST", "DELETE"):
            if box.favorite_id(rid, method == "POST"):
                self._json(200, {"id": rid, "favorite": method == "POST"})
            else:
                self._json(404, {"error": "no such recipe"})
        elif what == "show" and method == "POST":
            done = self.recipe_show(rid)
            if done is None:
                self._json(404, {"error": "no such recipe"})
            else:
                self._json(200, done)
        else:
            self._json(405, {"error": "method not allowed"})
        return True
