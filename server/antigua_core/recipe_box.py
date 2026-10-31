"""The household's recipe box: favorites and history (2026-10-25).

Every recipe opened by voice goes into the history (when, who, and whether
it was cooked to the end); "favorite this recipe" keeps one for good. The
whole recipe is stored with them, so either can be shown again, read, or
cooked without the web, and its page is a tap away on the phone menu (GET
/recipes, see RecipeRoutes; PinedaDisplay's /menu Recipes tab).

data/recipe_box.json:
  {"recipes":   {id: Recipe.to_dict()},           id = hash of the page URL
   "favorites": [{"id", "added", "by"}],          newest first
   "history":   [{"id", "at", "by", "dish", "cooked"}]}   newest first
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from threading import RLock

from . import settings
from .recipe import Recipe

log = logging.getLogger("antigua_core")

HISTORY_KEEP = 100
# Opening the same recipe again within this is the same entry, not a new one.
_SAME_VISIT_S = 6 * 3600


def recipe_id(recipe) -> str:
    return hashlib.sha1((recipe.url or recipe.title).encode()).hexdigest()[:10]


class RecipeBox:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or settings.DATA_DIR / "recipe_box.json")
        self._lock = RLock()
        self._data = None

    def _load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self._data = {}
            for k, empty in (("recipes", {}), ("favorites", []), ("history", [])):
                self._data.setdefault(k, empty)
        return self._data

    def _save(self) -> None:
        d = self._load()
        used = {f["id"] for f in d["favorites"]} | {h["id"] for h in d["history"]}
        d["recipes"] = {k: v for k, v in d["recipes"].items() if k in used}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            tmp.replace(self.path)
        except OSError as e:
            log.warning("Recipe box not saved: %s", e)

    def _keep(self, recipe) -> str:
        rid = recipe_id(recipe)
        self._load()["recipes"][rid] = recipe.to_dict()
        return rid

    # ── history ──────────────────────────────────────────────────────────

    def note_opened(self, dish: str, recipe, by: str | None = None) -> str:
        """A recipe opened by voice (picked from the choices, or a favorite)."""
        if getattr(recipe, "kind", "recipe") != "recipe":
            return ""
        with self._lock:
            rid = self._keep(recipe)
            hist = self._load()["history"]
            now = time.time()
            if hist and hist[0]["id"] == rid and now - hist[0]["at"] < _SAME_VISIT_S:
                hist[0]["at"] = now
            else:
                hist.insert(0, {"id": rid, "at": now, "by": by or "", "dish": dish, "cooked": False})
                del hist[HISTORY_KEEP:]
            self._save()
            return rid

    def note_cooked(self, recipe) -> None:
        """Read to the last step: the latest history entry for it is cooked."""
        rid = recipe_id(recipe)
        with self._lock:
            for h in self._load()["history"]:
                if h["id"] == rid:
                    if not h["cooked"]:
                        h["cooked"] = True
                        self._save()
                    return

    def last_opened(self, within_s: float = 3600):
        """The recipe opened most recently, if it was within `within_s`."""
        with self._lock:
            hist = self._load()["history"]
            if hist and time.time() - hist[0]["at"] < within_s:
                return self.get(hist[0]["id"])
        return None

    # ── favorites ────────────────────────────────────────────────────────

    def is_favorite(self, rid: str) -> bool:
        return any(f["id"] == rid for f in self._load()["favorites"])

    def add_favorite(self, recipe, by: str | None = None) -> bool:
        """False when it already was one."""
        with self._lock:
            rid = self._keep(recipe)
            if self.is_favorite(rid):
                return False
            self._load()["favorites"].insert(0, {"id": rid, "added": time.time(), "by": by or ""})
            self._save()
            return True

    def favorite_id(self, rid: str, on: bool) -> bool:
        """The menu's heart, by id: False when there's no such recipe."""
        with self._lock:
            d = self._load()
            if rid not in d["recipes"]:
                return False
            if on and not self.is_favorite(rid):
                d["favorites"].insert(0, {"id": rid, "added": time.time(), "by": ""})
            elif not on:
                d["favorites"] = [f for f in d["favorites"] if f["id"] != rid]
            self._save()
            return True

    def remove_favorite(self, recipe) -> bool:
        rid = recipe_id(recipe)
        with self._lock:
            if not self.is_favorite(rid):
                return False
            return self.favorite_id(rid, False)

    def favorites(self, dish: str = "") -> list:
        """Favorite Recipes, newest first; with `dish`, only ones for it."""
        from .recipe import _words
        want = _words(dish) - {"recipe", "favorite", "favourite", "my", "the"} if dish else set()
        out = []
        with self._lock:
            for f in self._load()["favorites"]:
                r = self.get(f["id"])
                if r is None:
                    continue
                if want and not want <= _words(f"{r.title} {self._dish_of(f['id'])}"):
                    continue
                out.append(r)
        return out

    def recent(self, n: int = 4) -> list:
        """The last `n` different recipes opened."""
        out, seen = [], set()
        with self._lock:
            for h in self._load()["history"]:
                if h["id"] in seen:
                    continue
                r = self.get(h["id"])
                if r is not None:
                    seen.add(h["id"])
                    out.append(r)
                if len(out) == n:
                    break
        return out

    def _dish_of(self, rid: str) -> str:
        return next((h["dish"] for h in self._load()["history"] if h["id"] == rid), "")

    def get(self, rid: str):
        raw = self._load()["recipes"].get(rid)
        try:
            return Recipe.from_dict(raw) if raw else None
        except (TypeError, KeyError):
            return None

    # ── the phone menu ───────────────────────────────────────────────────

    def _row(self, rid: str) -> dict | None:
        r = self.get(rid)
        if r is None:
            return None
        from .pineda import display_title    # "Omelette Recipe" -> "Omelette", as on the kiosk
        return {"id": rid, "title": display_title(r.title), "source": r.source, "url": r.url,
                "dish": self._dish_of(rid), "servings": r.servings, "total_min": r.total_min,
                "rating": round(r.rating, 1) if r.rating_count else 0, "rating_count": r.rating_count,
                "image": (r.images or [""])[0], "ingredients": list(r.ingredients),
                "steps": list(r.steps), "favorite": self.is_favorite(rid)}

    def api(self) -> dict:
        """GET /recipes: favorites and history, each with its whole recipe."""
        with self._lock:
            d = self._load()
            favs = [dict(row, added=f["added"]) for f in d["favorites"] if (row := self._row(f["id"]))]
            hist = [dict(row, at=h["at"], by=h["by"], cooked=h["cooked"])
                    for h in d["history"] if (row := self._row(h["id"]))]
        return {"favorites": favs, "history": hist}


_box: RecipeBox | None = None


def box() -> RecipeBox:
    global _box
    if _box is None:
        _box = RecipeBox()
    return _box
