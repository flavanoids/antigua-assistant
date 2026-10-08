"""PinedaDisplay Web — the dashboard on the airplaypi kiosk screen.

pineda-web runs on this box (noegpu01) and drives the kiosk's Chromium; the
restart/reboot endpoints reach airplaypi over SSH on their own (see
PinedaDisplay_Web docs/OPERATIONS.md#split-host-control). Plain HTTP, no
auth: pineda-web's admin endpoints are open on the LAN.

Also: the reply formatters for the display route (pure, tested offline), the
timer mirror that shows Antigua's running timers in the display's
moon-climate mini card, and the full-screen recipe card (RecipeDisplay).
"""

import json
import logging
import queue
import random
import re
import wave
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread

import requests

log = logging.getLogger("antigua_core")

TIMER_SOURCE = "antigua"


class PinedaError(RuntimeError):
    """pineda-web didn't answer, or answered with an error."""


class PinedaClient:
    def __init__(self, url: str, profile: str = "default", device: str | None = None,
                 timeout: float = 3.0):
        self.url = url.rstrip("/")
        self.profile = profile
        self.device = device
        self.timeout = timeout

    def _call(self, method: str, path: str, **kw):
        try:
            resp = requests.request(method, self.url + path, timeout=self.timeout, **kw)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise PinedaError(f"{method} {path}: {e}") from e
        return resp.json() if resp.content else None

    def themes(self) -> list[dict]:
        return self._call("GET", "/api/themes")["themes"]

    def current_theme(self) -> str:
        """The kiosk profile's theme — it overrides the server-wide one."""
        for p in self._call("GET", "/api/profiles"):
            if p["id"] == self.profile:
                return p.get("theme") or self._call("GET", "/api/themes")["current"]
        raise PinedaError(f"no profile {self.profile!r}")

    def set_theme(self, name: str) -> None:
        # The profile, not POST /api/themes/apply: a profile's own theme wins
        # over the server-wide one, and this one is saved across restarts.
        self._call("PATCH", f"/api/profiles/{self.profile}", json={"theme": name})

    def restart_display(self) -> None:
        self._call("POST", "/api/control/restart-display")

    def reboot(self) -> None:
        self._call("POST", "/api/control/restart-system")

    def now_showing(self) -> dict:
        params = {"device_id": self.device} if self.device else None
        return self._call("GET", "/api/now-showing", params=params)

    def sync_timers(self, timers: list[dict]) -> None:
        self._call("PUT", f"/api/timers/sync/{TIMER_SOURCE}", json={"timers": timers})

    def show_takeover(self, card: dict, seconds: float) -> None:
        """A full-screen card on every screen until `seconds` pass or
        clear_takeover(); a new one replaces the old."""
        self._call("PUT", "/api/takeover", json={"card": card, "seconds": seconds})

    def update_takeover(self, change: dict) -> None:
        """Change what's being read (LIVE_FIELDS) on the card that's up; does nothing
        (and never re-shows it) when none is."""
        self._call("PATCH", "/api/takeover", json=change)

    def takeover(self) -> dict | None:
        """The full-screen card that's up ({card, shown_at, expires_at}), or None."""
        return self._call("GET", "/api/takeover")

    def takeover_up(self) -> bool:
        return self.takeover() is not None

    def clear_takeover(self) -> None:
        self._call("DELETE", "/api/takeover")


# ── timers → the mini card ───────────────────────────────────────────────────


def timers_payload(active: list[dict]) -> list[dict]:
    """TimerManager.list_active() → pineda-web's sync body. Running timers
    only: alarms and reminders are clock times, and a paused timer has no
    fires_at to count down to (it reappears on resume)."""
    return [
        {
            "id": e["id"],
            "label": e.get("name") or e["label"],
            "fires_at": datetime.fromtimestamp(e["fires_at"], timezone.utc).isoformat(),
        }
        for e in active
        if e.get("kind", "timer") == "timer" and not e.get("paused")
    ]


class TimerMirror:
    """Pushes the whole running-timer list to pineda-web after every change.
    Each push reads the list fresh under one lock, so however pushes
    interleave, the last one to land carries the latest state."""

    def __init__(self, client: PinedaClient, list_active):
        self._client = client
        self._list_active = list_active
        self._lock = Lock()

    def push(self) -> None:
        Thread(target=self._push, daemon=True, name="pineda-timers").start()

    def _push(self) -> None:
        with self._lock:
            try:
                self._client.sync_timers(timers_payload(self._list_active()))
            except PinedaError as e:
                log.warning("Pineda: timer sync failed: %s", e)


# ── recipes → a full-screen card ────────────────────────────────────────────


def display_title(title: str) -> str:
    """'Butter Chicken Recipe (Indian Chicken Makhani)' -> 'Butter Chicken':
    the site's name, asides and the word "recipe" off, case kept."""
    t = re.sub(r"\s*[|:–—]\s.*$|\s+-\s.*$", "", title)
    t = re.sub(r"\s*\([^)]*\)|[®™©]", "", t)
    t = re.sub(r"\s*\b(?:recipes?)\b", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" ,")
    return t or title


# What's being read: these change from turn to turn on a card that's up
# (PATCH), the rest only with a new recipe, size or swap of recipe (PUT).
LIVE_FIELDS = ("stage", "current_step", "current_ingredients", "checked", "skipped", "swaps")


def _guide_title(title: str) -> str:
    t = re.sub(r"^\s*how\s+to\s+", "", title, flags=re.I).strip()
    return (t[:1].upper() + t[1:]) if t else title


def recipe_card(recipe, line=None, step: int | None = None, *, stage: str | None = None,
                lit=(), checked=(), skipped=(), swaps=None) -> dict:
    """A recipe.Recipe → pineda-web's RecipeTakeover. `line(i)` gives
    ingredient line i as the cook has it (CookSession.line: scaled); the
    published line when None. Lines outside every group lead, unnamed.
    `step` is the step being read (0-based), highlighted on screen; `stage`
    "ingredients" or "steps". `lit` (lines to highlight), `checked`,
    `skipped` and `swaps` ({line: what's used instead}) are recipe line
    indexes; the card counts lines in its own order."""
    line = line or (lambda i: recipe.ingredients[i])
    grouped = {i for _name, a, b in recipe.groups for i in range(a, b + 1)}
    loose = [i for i in range(len(recipe.ingredients)) if i not in grouped]
    order = list(loose)
    groups = [{"name": "", "items": [line(i) for i in loose]}] if loose else []
    for name, a, b in recipe.groups:
        idx = [i for i in range(a, b + 1) if i < len(recipe.ingredients)]
        if idx:
            order += idx
            groups.append({"name": name.strip(), "items": [line(i) for i in idx]})
    at = {i: n for n, i in enumerate(order)}

    def card_order(lines):
        return sorted(at[i] for i in set(lines) if i in at)

    return {
        "kind": getattr(recipe, "kind", "recipe"),
        "caution": getattr(recipe, "caution", ""),
        # A guide's kicker already says "How to": "Cut out drywall", not
        # "How to cut out drywall" under it.
        "title": _guide_title(recipe.title) if getattr(recipe, "kind", "") == "howto"
        else display_title(recipe.title),
        "source": recipe.source,
        "servings": recipe.servings,
        "total_min": recipe.total_min,
        "ingredients": groups,
        "steps": list(recipe.steps),
        "stage": stage,
        "current_step": step,
        "current_ingredients": card_order(lit),
        "checked": card_order(checked),
        "skipped": card_order(skipped),
        "swaps": [{"index": at[i], "use": use} for i, use in sorted((swaps or {}).items())
                  if i in at],
    }


def _live(card: dict) -> dict:
    return {k: card.get(k) for k in LIVE_FIELDS}


def _same_recipe(a: dict, b: dict) -> bool:
    """The same card but for what's being read."""
    fixed = lambda c: {k: v for k, v in c.items() if k not in LIVE_FIELDS}  # noqa: E731
    return fixed(a) == fixed(b)


class RecipeDisplay:
    """The recipe on the kiosk: up when a recipe opens (and again if it's
    scaled or swapped for another), down on "stop the display", the end of
    the recipe, or after `seconds`. The last card is kept on disk for "show
    the recipe again", after the session and across restarts."""

    def __init__(self, client: PinedaClient, path: Path, seconds: float = 120):
        self._client = client
        self._path = path
        self.seconds = seconds
        self._shown = None          # the card last pushed for the open recipe
        self._calls = queue.Queue()
        self._worker = None

    def last(self) -> dict | None:
        try:
            return json.loads(self._path.read_text())
        except (OSError, ValueError):
            return None

    def mirror(self, card: dict | None) -> None:
        """After each recipe turn: `card` for the open recipe, None once it's
        closed. A new recipe, size or swap puts it up; a new step or
        ingredient only moves the highlights on a card that's still up, so
        "next" doesn't bring back one that timed out or was dismissed."""
        if card == self._shown:
            return
        was, self._shown = self._shown, card
        if card is not None and was is not None and _same_recipe(card, was):
            change = {k: v for k, v in _live(card).items() if v != was.get(k)}
            self._remember(card)
            self._background(lambda: self._client.update_takeover(change))
        elif card is not None:
            self._remember(card)
            self._background(lambda: self._client.show_takeover(card, self.seconds))
        elif was is not None:
            self._background(self._client.clear_takeover)

    def _remember(self, card: dict) -> None:
        try:
            self._path.write_text(json.dumps(card))
        except OSError as e:
            log.warning("Pineda: couldn't keep the recipe card: %s", e)

    def show(self, card: dict | None = None) -> bool:
        """Put `card` (or the last one) up again. False when there's none."""
        card = card or self.last()
        if card is None:
            return False
        self._client.show_takeover(card, self.seconds)
        return True

    def hide(self) -> bool:
        """Take it down. False when nothing was up."""
        if not self._client.takeover_up():
            return False
        self._client.clear_takeover()
        return True

    def _background(self, call) -> None:
        """Off the reply's path, one at a time, in order: a step update must
        land after the show it updates."""
        if self._worker is None:
            self._worker = Thread(target=self._drain, daemon=True, name="pineda-recipe")
            self._worker.start()
        self._calls.put(call)

    def _drain(self) -> None:
        while True:
            call = self._calls.get()
            try:
                call()
            except PinedaError as e:
                log.warning("Pineda: recipe card: %s", e)
            finally:
                self._calls.task_done()


# ── themes ───────────────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z ]", " ", text.lower().replace("_", " ")).split())


def base_themes(themes: list[dict]) -> list[dict]:
    """Themes you'd pick by name — not the night twins every light theme
    switches to on its own at sundown."""
    twins = {t["night"] for t in themes if t.get("night")}
    return [t for t in themes if t["name"] not in twins]


def match_theme(text: str, themes: list[dict]) -> dict | None:
    """The theme named in `text`: its full label ("cocoa ember"), its id, or
    any one word of its label no other theme shares ("cocoa")."""
    said = f" {_norm(text)} "
    choices = base_themes(themes)
    words: dict[str, list[dict]] = {}
    for t in choices:
        for w in _norm(t["label"]).split():
            words.setdefault(w, []).append(t)
    best, best_len = None, 0
    for t in choices:
        names = {_norm(t["label"]), _norm(t["name"])}
        names |= {w for w in _norm(t["label"]).split() if len(w) >= 4 and len(words[w]) == 1}
        for n in names:
            if f" {n} " in said and len(n) > best_len:
                best, best_len = t, len(n)
    return best


# "change the theme" / "switch the display theme" — nothing named, so pick one.
_BARE_CHANGE_RE = re.compile(
    r"^(?:please\s+)?(?:change|switch)\s+(?:up\s+)?(?:the\s+)?(?:display\s+|screen\s+)?theme"
    r"(?:\s+on\s+the\s+(?:display|screen))?(?:\s+please)?\s*[.!?]*$",
    re.IGNORECASE,
)


def is_bare_theme_change(text: str) -> bool:
    return bool(_BARE_CHANGE_RE.match(text.strip()))


def pick_random_theme(themes: list[dict], current: str | None) -> dict:
    choices = [t for t in base_themes(themes) if t["name"] != current] or base_themes(themes)
    return random.choice(choices)


def spoken_list(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def theme_label(themes: list[dict], name: str) -> str:
    """A theme's label, a night twin by its day theme's ("Ocean, night")."""
    for t in themes:
        if t["name"] == name:
            return t["label"]
    for t in themes:
        if t.get("night") == name:
            return f"{t['label']}, night"
    return name.replace("_", " ")


# ── what's on screen ─────────────────────────────────────────────────────────


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _time_of_day(hour: int, minute: int) -> str:
    h = (hour + (1 if minute >= 30 else 0)) % 24
    if h == 0:
        return "around midnight"
    if h == 12:
        return "around noon"
    twelve = h % 12 or 12
    if h < 12:
        part = "in the morning"
    elif h < 17:
        part = "in the afternoon"
    elif h < 21:
        part = "in the evening"
    else:
        part = "at night"
    return f"around {twelve} {part}"


def photo_taken_reply(photo: dict | None) -> str:
    if not photo:
        return "I can't tell which photo is up right now."
    taken = photo.get("taken_local")
    if not taken:
        return "That one doesn't have a date."
    dt = datetime.fromisoformat(taken)
    return (f"That photo was taken on {dt:%B} {_ordinal(dt.day)}, {dt.year}, "
            f"{_time_of_day(dt.hour, dt.minute)}.")


def _by(quote: dict) -> str:
    author = quote.get("author") or "an unknown author"
    return f"{author}, in {quote['year']}" if quote.get("year") else author


def quote_who_reply(quote: dict | None) -> str:
    if not quote:
        return "I can't tell which quote is up right now."
    if not quote.get("author"):
        return "I don't know who said that one."
    return f"That's {_by(quote)}."


def quote_read_reply(quote: dict | None) -> str:
    if not quote:
        return "I can't tell which quote is up right now."
    return f"{quote['text']} That's {_by(quote)}."


def quote_context(quote: dict) -> str:
    """Grounding for "tell me more about this quote" (search results follow)."""
    lines = [f'The quote on the display: "{quote["text"]}" — {_by(quote)}.']
    lines.append("Say briefly who said it and what it means or where it comes from, "
                 "in two or three sentences. Use only the facts given here.")
    return "\n".join(lines)


def quote_search_query(quote: dict) -> str:
    words = quote["text"].split()[:10]
    return " ".join(filter(None, [quote.get("author"), " ".join(words)]))


# ── the Spanish phrase, spoken ES → EN → ES ──────────────────────────────────


def phrase_text(phrase: dict) -> tuple[str, str]:
    """(spanish, english meaning) — the parts synthesized in each voice."""
    return phrase["spanish"].strip(), f"It means: {phrase['english'].strip()}."


def concat_wavs(paths: list[str], out_path: Path, gap_s: float = 0.4) -> bool:
    """Join same-format WAVs with a short silence between. False (nothing
    written) if any is missing or they don't share a format."""
    try:
        frames, params = [], None
        for p in paths:
            with wave.open(p, "rb") as w:
                these = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                if params is None:
                    params = these
                elif these != params:
                    log.warning("Pineda: WAV formats differ, can't join: %s", paths)
                    return False
                frames.append(w.readframes(w.getnframes()))
        channels, width, rate = params
        gap = b"\x00" * int(rate * gap_s) * channels * width
        with wave.open(str(out_path), "wb") as out:
            out.setnchannels(channels)
            out.setsampwidth(width)
            out.setframerate(rate)
            out.writeframes(gap.join(frames))
        return True
    except (OSError, wave.Error, TypeError) as e:
        log.warning("Pineda: joining WAVs failed: %s", e)
        return False
