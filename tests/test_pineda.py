#!/usr/bin/env python3
"""PinedaDisplay (airplaypi kiosk) skill: parsing, replies, timer mirror, and
the pipeline route end to end against a fake pineda-web.

Run: python3 tests/test_pineda.py   (also works under pytest)
"""

import sys
import tempfile
import time
import wave
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import pineda, pipeline, settings  # noqa: E402
from antigua_core import recipe_session  # noqa: E402
from antigua_core.intents.pineda import (  # noqa: E402
    parse_clock_mode, parse_pineda_request, parse_recipe_display,  # noqa: E402
    parse_screen_turn)  # noqa: E402
from antigua_core.recipe import Recipe  # noqa: E402
from antigua_core.recipe_subs import step_ingredients  # noqa: E402
from antigua_core.stores import ListStore, MemoryStore, TimerManager, TimerSpec  # noqa: E402

THEMES = [
    {"name": "quiet_canvas", "label": "Quiet Canvas", "night": None},
    {"name": "ocean", "label": "Ocean", "night": "ocean_night"},
    {"name": "ocean_night", "label": "Ocean Night", "night": None},
    {"name": "cocoa_ember", "label": "Cocoa Ember", "night": None},
    {"name": "groovy", "label": "Groovy", "night": "groovy_night"},
    {"name": "groovy_night", "label": "Groovy Night", "night": None},
]


def test_parser():
    cases = {
        "reboot the pi": "reboot",
        "restart airplaypi": "reboot",
        "reboot the raspberry pie": "reboot",
        "restart the display": "restart_display",
        "refresh the screen": "restart_display",
        "change the theme to ocean": "theme",
        "switch the display to groovy": "theme",
        "change the theme": "theme",
        "random theme": "theme_random",
        "surprise me with a new theme": "theme_random",
        "what themes are there": "theme_list",
        "what theme is this": "theme_current",
        "the groovy theme please": "theme_named",
        "who said this quote": "quote_who",
        "tell me more about this quote": "quote_more",
        "what does the quote mean": "quote_more",
        "read the quote": "quote_read",
        "say the spanish phrase": "phrase",
        "how do you say the spanish phrase": "phrase",
        "what's the word of the day": "phrase",
        "say the phrase": "phrase",
        "when was this photo taken": "photo_when",
        "when was this taken": "photo_when",
        "what year is this picture from": "photo_when",
        "show me pictures of strawberries": "image_search",
        "search for photos of vintage cars": "image_search",
        "pictures of red pandas": "image_search",
        "can you show me photos of cats": "image_search",
        "pictures of puppies please": "image_search",
        "tell me more about picture B": "image_detail",
        "tell me more about picture B please": "image_detail",
        "can you tell me more about picture B?": "image_detail",
        "what is in picture B": "image_detail",
        "what is photo C": "image_detail",
        "tell me about picture 2": "image_detail",
        "tell me more about picture two": "image_detail",
        "what is the second picture": "image_detail",
        "what is picture A": "image_detail",
        "tell me about image C": "image_detail",
        "describe picture D": "image_detail",
        "what is B": "image_detail",
        "tell me more about B": "image_detail",
        "first picture please": "image_detail",
        "show me tile 3": "image_detail",
    }
    for text, action in cases.items():
        got = parse_pineda_request(text)
        assert got and got[0] == action, (text, got)

    assert parse_pineda_request("tell me more about picture two") == ("image_detail", "B")
    assert parse_pineda_request("what is the second picture") == ("image_detail", "B")
    assert parse_pineda_request("first picture please") == ("image_detail", "A")
    assert parse_pineda_request("show me tile 3") == ("image_detail", "C")

    for text in [
        "give me a quote", "give me an inspirational quote",
        "what does the phrase break a leg mean", "what's the theme of macbeth",
        "play the star wars theme song", "set the theme for the party",
        "when was the first photo taken", "what time is it", "restart the music",
        "turn on the hallway lights", "play the jeopardy theme", "put on the jeopardy theme",
    ]:
        assert parse_pineda_request(text) is None, text


def test_themes():
    assert [t["name"] for t in pineda.base_themes(THEMES)] == [
        "quiet_canvas", "ocean", "cocoa_ember", "groovy"]
    m = pineda.match_theme
    assert m("change the theme to ocean", THEMES)["name"] == "ocean"
    assert m("switch to cocoa ember", THEMES)["name"] == "cocoa_ember"
    assert m("use the cocoa theme", THEMES)["name"] == "cocoa_ember"      # a unique word
    assert m("quiet canvas please", THEMES)["name"] == "quiet_canvas"
    assert m("change the theme to purple", THEMES) is None
    assert m("change the theme", THEMES) is None
    assert pineda.is_bare_theme_change("change the theme")
    assert pineda.is_bare_theme_change("Switch the display theme.")
    assert not pineda.is_bare_theme_change("change the theme to purple")
    for _ in range(20):   # never the current one, never a night twin
        pick = pineda.pick_random_theme(THEMES, "ocean")["name"]
        assert pick in ("quiet_canvas", "cocoa_ember", "groovy"), pick
    assert pineda.spoken_list(["A", "B", "C"]) == "A, B, and C"


def test_replies():
    r = pineda.photo_taken_reply
    assert r({"taken_local": "2019-06-14T19:02:11"}) == \
        "That photo was taken on June 14th, 2019, around 7 in the evening."
    assert r({"taken_local": "2021-03-02T11:40:00"}) == \
        "That photo was taken on March 2nd, 2021, around noon."
    assert r({"taken_local": "2020-12-23T00:10:00"}).endswith("around midnight.")
    assert r({"taken_local": "2020-12-23T14:10:00"}).endswith("around 2 in the afternoon.")
    assert r({"taken_local": "2020-12-23T22:10:00"}).endswith("around 10 at night.")
    assert r({"name": "x.png", "taken_local": None}) == "That one doesn't have a date."
    assert r(None) == "I can't tell which photo is up right now."

    q = {"text": "When they go low, we go high.", "author": "Michelle Obama", "year": 2016}
    assert pineda.quote_who_reply(q) == "That's Michelle Obama, in 2016."
    assert pineda.quote_who_reply({**q, "author": None}) == "I don't know who said that one."
    assert pineda.quote_read_reply(q) == \
        "When they go low, we go high. That's Michelle Obama, in 2016."
    assert pineda.quote_search_query(q) == "Michelle Obama When they go low, we go high."
    assert "When they go low" in pineda.quote_context(q)


def test_timers_payload_and_hook():
    active = [
        {"id": "a1", "label": "pasta timer", "name": "pasta", "kind": "timer", "fires_at": 0.0},
        {"id": "b2", "label": "Timer", "name": None, "kind": "timer", "fires_at": 60.0},
        {"id": "c3", "label": "Alarm", "name": None, "kind": "alarm", "fires_at": 90.0},
        {"id": "d4", "label": "gym alarm", "name": "gym", "kind": "alarm", "fires_at": 120.0},
        {"id": "e5", "label": "call mom", "name": None, "kind": "reminder",
         "message": "call mom", "fires_at": 150.0},
        {"id": "f6", "label": "Timer", "name": None, "kind": "timer", "fires_at": 9.0,
         "paused": True},
    ]
    assert pineda.timers_payload(active) == [
        {"id": "a1", "label": "pasta", "kind": "timer", "name": "pasta", "fires_at": "1970-01-01T00:00:00+00:00"},
        {"id": "b2", "label": "Timer", "kind": "timer", "name": None, "fires_at": "1970-01-01T00:01:00+00:00"},
        {"id": "c3", "label": "Alarm", "kind": "alarm", "name": None, "fires_at": "1970-01-01T00:01:30+00:00"},
        {"id": "d4", "label": "gym", "kind": "alarm", "name": "gym", "fires_at": "1970-01-01T00:02:00+00:00"},
        {"id": "e5", "label": "call mom", "kind": "reminder", "name": "call mom",
         "fires_at": "1970-01-01T00:02:30+00:00"},
    ]

    changes = []
    tm = TimerManager(on_change=lambda: changes.append(len(tm.list_active())))
    tid = tm.set(TimerSpec(seconds=300, label="tea timer", name="tea"))
    tm.add_time_id(tid, 60)
    tm.cancel_all()
    assert changes == [1, 1, 0], changes


def _wav(path: Path, frames: int, rate: int = 24000) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * frames)
    return str(path)


def test_concat_wavs():
    tmp = Path(tempfile.mkdtemp(prefix="antigua_pineda_"))
    a, b = _wav(tmp / "a.wav", 100), _wav(tmp / "b.wav", 50)
    out = tmp / "out.wav"
    assert pineda.concat_wavs([a, b, a], out, gap_s=0.01)
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() == 100 + 50 + 100 + 2 * 240, w.getnframes()
    odd = _wav(tmp / "odd.wav", 10, rate=16000)
    assert not pineda.concat_wavs([a, odd], tmp / "no.wav")
    assert not pineda.concat_wavs([a, str(tmp / "missing.wav")], tmp / "no.wav")


class _FakeMcp:
    """An HttpMcpServer that answers from a table: tool -> structured value."""

    urls = ["http://pineda:8090/mcp/"]

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def call(self, tool, arguments=None, timeout=None):
        from antigua_core.mcp_client import McpResult
        self.calls.append((tool, arguments))
        value = self.answers.get(tool)
        if isinstance(value, Exception):
            return McpResult(ok=False, text=str(value), data={"error": "x"})
        return McpResult(ok=True, data=value if isinstance(value, dict) else {"result": value})


def test_client_calls_the_display_tools():
    mcp = _FakeMcp({"theme_set": {"theme": "ocean"}, "now_showing": {"photo": None},
                    "timers_sync": {"count": 0}, "clock_mode": {"changed": 2},
                    "display_status": {"theme": "ocean", "takeover": None},
                    "takeover_get": None, "camera_show": {"camera": "front_door"},
                    "display_reboot": {"accepted": True}})
    c = pineda.PinedaClient(mcp, device="kiosk1")
    c.set_theme("ocean")
    assert c.now_showing() == {"photo": None}
    c.sync_timers([])
    assert c.hold_clock(True) == 2
    assert c.status()["theme"] == "ocean" and not c.takeover_up() and c.takeover() is None
    c.show_camera("front_door", 30)
    c.reboot()
    assert mcp.calls == [
        ("theme_set", {"name": "ocean"}), ("now_showing", {"device_id": "kiosk1"}),
        ("timers_sync", {"source": "antigua", "timers": []}), ("clock_mode", {"on": True}),
        ("display_status", {}), ("display_status", {}), ("takeover_get", {}),
        ("camera_show", {"camera": "front_door", "seconds": 30}),
        ("display_reboot", {"confirm": True}),
    ]
    down = pineda.PinedaClient(_FakeMcp({"clock_mode": RuntimeError("No screen")}))
    try:
        down.hold_clock(True)
        raise AssertionError("a failed tool should raise")
    except pineda.PinedaError as e:
        assert "No screen" in str(e)


# ── the route end to end ─────────────────────────────────────────────────────


class FakePineda:
    def __init__(self):
        self.theme = "groovy"
        self.calls = []
        self.showing = {
            "photo": {"name": "p.jpg", "taken_local": "2019-06-14T19:02:11"},
            "quote": {"text": "When they go low, we go high.", "author": "Michelle Obama",
                      "year": 2016},
            "phrase": {"english": "Honestly, I don't know", "spanish": "La neta, no sé",
                       "pron": "", "note": ""},
        }

    def themes(self):
        return THEMES

    def current_theme(self):
        return self.theme

    def status(self):
        label = next((t["label"] for t in THEMES if t["name"] == self.theme), self.theme)
        return {"theme": self.theme, "theme_label": label, "takeover": None}

    def set_theme(self, name):
        self.calls.append(("theme", name))
        self.theme = name

    def restart_display(self):
        self.calls.append(("restart_display",))

    def reboot(self):
        self.calls.append(("reboot",))

    def now_showing(self):
        return self.showing

    takeover = None

    def show_takeover(self, card, seconds):
        self.calls.append(("show", card.get("title") or card.get("query"), seconds))
        self.takeover = card

    def show_image_collage(self, query, images, seconds=120, groups=None):
        card = {"kind": "image_search", "query": query, "images": images, "highlight": None}
        if groups:
            card["groups"] = groups
        self.show_takeover(card, seconds)

    def highlight_image(self, identifier, seconds=None):
        change = {"highlight": identifier.upper()}
        if seconds:
            change["seconds"] = seconds
        self.update_takeover(change)

    def update_takeover(self, change):
        self.calls.append(("update", change))
        if self.takeover is not None:      # like pineda-web: never re-shows; only what's sent
            self.takeover = {**self.takeover, **change}

    def takeover_up(self):
        return self.takeover is not None

    def clear_takeover(self):
        self.calls.append(("clear",))
        self.takeover = None

    held = False

    def hold_clock(self, on):
        self.calls.append(("hold", on))
        changed, self.held = int(self.held != on), on
        return changed

    def turn_screen(self, to):
        self.calls.append(("turn", to))
        now = getattr(self, "rotation", "normal")
        tall = now in ("90", "270")
        then = {"tall": now if tall else "90", "wide": "normal" if tall else now,
                "flip": {"normal": "180", "180": "normal", "90": "270", "270": "90"}[now],
                "turn": "normal" if tall else "90"}[to]
        self.rotation = then
        return {"rotation": then, "changed": then != now}


class DownPineda(FakePineda):
    def now_showing(self):
        raise pineda.PinedaError("connection refused")


def make_backend(fake, llm_seen):
    tmp = Path(tempfile.mkdtemp(prefix="antigua_pineda_"))
    n = [0]

    def synthesize(text, lang="en"):
        n[0] += 1
        return _wav(tmp / f"tts_{n[0]}_{lang}.wav", 10)

    def ask_llm_stream(transcript, **kw):
        llm_seen.append(kw)
        yield "She said it at the 2016 convention."

    class _Searx:
        def search_best(self, query, **kw):
            llm_seen.append({"query": query})
            return None, [], query

        def search_image_comparison(self, left, right, per_side=3):
            items = [
                {"id": "A", "url": "https://example.com/f1.jpg", "title": "Fuji in a bowl", "source": "a.example", "snippet": ""},
                {"id": "B", "url": "https://example.com/f2.jpg", "title": "Fuji close up", "source": "b.example", "snippet": ""},
                {"id": "C", "url": "https://example.com/h1.jpg", "title": "Honeycrisp", "source": "c.example", "snippet": ""},
            ]
            return items, [{"label": left[:1].upper() + left[1:], "ids": ["A", "B"]},
                           {"label": right[:1].upper() + right[1:], "ids": ["C"]}]

        def search_images(self, query, count=6):
            return [
                {"id": "A", "url": "https://example.com/a.jpg", "title": "Ripe Strawberry", "source": "wikimedia.org", "snippet": "Fresh red berries."},
                {"id": "B", "url": "https://example.com/b.jpg", "title": "Strawberry Tart", "source": "unsplash.com", "snippet": "Baked dessert."},
            ]

    return pipeline.Backend(
        transcribe=lambda p: {"text": "", "time_s": 0.0, "confidence": 1.0},
        synthesize=synthesize,
        ask_llm_stream=ask_llm_stream,
        audio_url_base=lambda: "http://test:0",
        memory_store=MemoryStore(path=tmp / "memories.json"),
        list_store=ListStore(path=tmp / "lists.json"),
        timers=TimerManager(),
        weather_cache=None,
        news_cache=None,
        searxng=_Searx(),
        pineda=fake,
    )


def test_route():
    settings.PINEDA_REBOOT_DELAY_S = 0
    settings.SEARCH_ENABLED = True
    fake, llm_seen = FakePineda(), []
    pipeline.init(make_backend(fake, llm_seen))

    def say(text, conv="c1"):
        return pipeline.dispatch_text(text, conversation_id=conv, quiet=True)

    assert say("change the theme to ocean")["response"] == "Switching the display to Ocean."
    assert fake.calls[-1] == ("theme", "ocean")
    assert say("switch to the ocean theme")["response"] == "The display is already on Ocean."
    assert say("change the theme to purple")["response"].startswith("I don't know that theme.")
    r = say("surprise me with a random theme")
    assert r["response"].startswith("Switching the display to") and fake.theme != "ocean", r
    assert say("what themes are there")["response"] == \
        "The display has Quiet Canvas, Ocean, Cocoa Ember, and Groovy."

    assert say("restart the display")["response"] == "Restarting the display."
    assert fake.calls[-1] == ("restart_display",)

    # Reboot asks first; "no" leaves the Pi alone, "yes" reboots it.
    assert "Say yes to reboot" in say("reboot the pi", "c2")["response"]
    assert say("no", "c2")["response"] == "Okay, I won't."
    say("reboot airplaypi", "c3")
    assert say("yes", "c3")["response"].startswith("Rebooting airplaypi")
    for _ in range(50):
        if ("reboot",) in fake.calls:
            break
        time.sleep(0.02)
    assert fake.calls.count(("reboot",)) == 1, fake.calls
    # A "yes" with nothing pending isn't a reboot.
    say("yes", "c4")
    assert fake.calls.count(("reboot",)) == 1

    assert say("when was this photo taken")["response"] == \
        "That photo was taken on June 14th, 2019, around 7 in the evening."
    assert say("who said this quote")["response"] == "That's Michelle Obama, in 2016."

    r = say("tell me more about this quote")
    assert r.get("streaming"), r
    assert llm_seen[0] == {"query": "Michelle Obama When they go low, we go high."}, llm_seen
    assert "When they go low" in llm_seen[1]["grounding_context"], llm_seen[1]

    r = say("say the spanish phrase")
    assert r["response"] == \
        "La neta, no sé. It means: Honestly, I don't know. La neta, no sé.", r
    assert Path(r["audio_file"]).name.startswith("phrase_"), r
    with wave.open(r["audio_file"], "rb") as w:
        assert w.getnframes() > 30   # three clips and two gaps

    pipeline.init(make_backend(DownPineda(), []))
    assert say("when was this photo taken")["response"] == "I can't reach the display right now."

    pipeline.init(make_backend(None, []))
    assert say("restart the display")["response"] == "I can't control the display from here."


# ── the full-screen recipe card ──────────────────────────────────────────────

COLADA = Recipe.from_dict(dict(   # from_dict parses the amounts, for scaling
    title="The Best Piña Coladas Recipe | Food Network", source="Food Network",
    url="https://example.test/colada", servings="makes 2 drinks", total_min=5,
    ingredients=["2 ounces pineapple juice", "1 ounce white rum", "1 cup ice",
                 "1 pineapple wedge"],
    groups=[["For the drink", 0, 2]],
    steps=["Blend the juice, rum and ice until smooth.", "Pour and garnish."],
))


def test_recipe_parser():
    cases = {
        "can you show the recipe again": "show",
        "show me the ingredients": "show",
        "Alexa, show the recipe on the screen": "show",
        "show me the recipe for lasagna": None,     # a new recipe, not this one
        "show me a recipe for tacos": None,
        "stop the display": "hide",
        "have the display go back": "hide",
        "make the screen go back to normal": "hide",
        "hide the recipe": "hide",
        "take the recipe off the screen": "hide",
        "close the recipe": None,                   # ends the recipe itself
        "go back": None,                            # the previous step
        "restart the display": None,
        "stop the music": None,
    }
    for text, want in cases.items():
        assert parse_recipe_display(text) == want, (text, parse_recipe_display(text))
    assert parse_pineda_request("stop the display") is None


def test_recipe_card():
    card = pineda.recipe_card(COLADA)
    assert card["title"] == "The Best Piña Coladas", card["title"]
    assert pineda.display_title("Butter Chicken Recipe (Indian Chicken Makhani)") == "Butter Chicken"
    assert pineda.display_title("Pad Thai - Simply Recipes") == "Pad Thai"
    # The line no group covers leads, unnamed.
    assert card["ingredients"] == [
        {"name": "", "items": ["1 pineapple wedge"]},
        {"name": "For the drink", "items": COLADA.ingredients[:3]},
    ], card["ingredients"]
    assert card["steps"] == COLADA.steps and card["servings"] == "makes 2 drinks"
    assert card["current_step"] is None
    assert pineda.recipe_card(COLADA, step=1)["current_step"] == 1
    doubled = pineda.recipe_card(COLADA, lambda i: f"x2 {COLADA.ingredients[i]}")
    assert doubled["ingredients"][1]["items"][0] == "x2 2 ounces pineapple juice"


def test_recipe_card_highlights():
    # Recipe line indexes in, the card's own order out: the loose wedge
    # (line 3) leads, so the drink's lines 0-2 are 1-3 on the card.
    card = pineda.recipe_card(COLADA, step=0, stage="steps", lit=[0, 2], checked=[3],
                              skipped=[1], swaps={2: "crushed ice"})
    assert card["stage"] == "steps"
    assert card["current_ingredients"] == [1, 3]
    assert card["checked"] == [0] and card["skipped"] == [2]
    assert card["swaps"] == [{"index": 3, "use": "crushed ice"}]
    plain = pineda.recipe_card(COLADA)
    assert plain["current_ingredients"] == [] and plain["swaps"] == [] and plain["stage"] is None
    assert pineda._same_recipe(card, plain)
    assert not pineda._same_recipe(card, pineda.recipe_card(COLADA, lambda i: "x2"))


def test_step_ingredients():
    ing = ["1 1/2 lb boneless chicken thighs, cut into bite-size pieces",
           "1 cup plain whole-milk yogurt", "1 tablespoon lemon juice",
           "2 teaspoons garam masala", "1 teaspoon kosher salt",
           "3 tablespoons butter", "2 teaspoons garam masala", "1/2 teaspoon chili powder",
           "1 cup heavy cream", "1/2 teaspoon kosher salt", "1 batch lemon yogurt sauce"]
    groups = [("For the marinade", 0, 4), ("For the sauce", 5, 10)]
    steps = [
        "Stir together the yogurt, lemon juice, garam masala and salt.",   # whole names
        "Add the chicken and toss to coat.",                               # a cut's first word
        "Line a 13-inch baking sheet with foil.",                          # not the ginger's "inch"
        "Melt the butter, then add the garam masala and chili powder.",    # the unused masala
        "Stir in the cream, the salt and any juices from the pan.",        # not the lemon juice
        "Taste and add more salt if it needs it.",                         # the salt used last
        "Combine the sauce ingredients.",                                  # the whole group
        "Spoon over the yogurt sauce.",                                    # how a name ends
        "Add the remaining ingredients.",                                  # none left unused
    ]
    got = step_ingredients(steps, ing, groups)
    assert got[0] == [1, 2, 3, 4], got[0]
    assert got[1] == [0], got[1]
    assert got[2] == [], got[2]
    assert got[3] == [5, 6, 7], got[3]
    assert got[4] == [8, 9], got[4]
    assert got[5] == [9], got[5]
    assert got[6] == list(range(5, 11)), got[6]
    assert got[7] == [10], got[7]
    assert got[8] == [], got[8]
    assert step_ingredients(["Blend the juice and ice."], ["2 ounces pineapple juice", "1 cup ice"]) \
        == [[0, 1]]
    assert step_ingredients(["Gather all ingredients."], ["salt", "eggs"]) == [[0, 1]]
    assert step_ingredients(["Serve the chicken tikka masala."], ["chicken thighs", "garam masala"],
                            title="Chicken Tikka Masala (Restaurant Style)") == [[]]
    assert step_ingredients(["Add the red chilli powder."],
                            ["1 tsp Kashmiri red chili powder", "1 green chilli"]) == [[0]]


def _settle(fake, n):
    for _ in range(100):
        if len(fake.calls) >= n:
            return
        time.sleep(0.02)


def test_recipe_route():
    settings.RECIPE_ENABLED = settings.SEARCH_ENABLED = True
    settings.PINEDA_RECIPE_SECONDS = 120
    recipe_session.clear()
    fake = FakePineda()

    class Finder:
        def cached(self, dish):
            return True

        def find(self, dish):
            return [COLADA]

    backend = make_backend(fake, [])
    backend.recipes = Finder()
    pipeline.init(backend)
    (settings.DATA_DIR / "pineda_recipe_card.json").unlink(missing_ok=True)

    def say(text):
        return pipeline.dispatch_text(text, conversation_id="r1", quiet=True)["response"]

    assert say("show the recipe again") == "I haven't shown a recipe yet."
    assert say("stop the display") == "The display's already back to normal."

    # The first ask always shows the choices, even for one recipe
    # (user decision 2026-10-25), for 30 seconds.
    assert say("give me a recipe for pina colada") == (
        "I found one recipe for pina colada, from Food Network. It's on Pineda Display now. "
        "Want to make it?")
    _settle(fake, 1)
    assert fake.calls == [("show", None, 30)] and fake.takeover["kind"] == "recipe_choices", fake.calls
    assert recipe_session.current() is None
    fake.calls.clear()
    # Picking it opens it, up for PINEDA_RECIPE_SECONDS.
    say("yes")
    _settle(fake, 1)
    assert fake.calls == [("show", "The Best Piña Coladas", 120)], fake.calls

    # A turn that doesn't change it leaves the display alone.
    say("yes")
    time.sleep(0.1)
    assert len(fake.calls) == 1, fake.calls

    # Reading a step highlights it on the card that's up.
    say("next")
    _settle(fake, 2)
    assert fake.calls[-1][0] == "update" and fake.takeover["current_step"] == 0, fake.calls
    assert fake.takeover["stage"] == "steps"
    assert fake.calls[-1][1]["current_ingredients"] == fake.takeover["current_ingredients"]

    # Rescaling sends the new amounts, still on the same step.
    say("double it")
    _settle(fake, 3)
    assert fake.calls[-1][0] == "show" and "4 ounces" in str(fake.takeover["ingredients"]), fake.takeover
    assert fake.takeover["current_step"] == 0

    assert say("stop the display") == "Okay."
    assert fake.calls[-1] == ("clear",)
    say("next")                       # reads the new amounts (asked after "double it")
    say("next")                       # dismissed stays dismissed: only the highlight moves
    _settle(fake, 5)
    assert fake.calls[-1][0] == "update" and fake.calls[-1][1]["current_step"] == 1, fake.calls
    assert fake.takeover is None

    assert say("can you show the recipe again") == "It's on the display."
    assert "4 ounces" in str(fake.takeover["ingredients"])
    assert fake.takeover["current_step"] == 1      # back up on the step being read

    # Ending the recipe takes it down; "show" still brings the last one back.
    n = len(fake.calls)
    say("stop the recipe")
    _settle(fake, n + 1)
    assert fake.calls[-1] == ("clear",) and recipe_session.current() is None, fake.calls
    assert say("show the recipe") == "It's on the display."
    assert fake.takeover["title"] == "The Best Piña Coladas"

    pipeline.init(make_backend(None, []))
    assert say("stop the display") == "I can't control the display from here."
    recipe_session.clear()


def test_recipe_choices_route():
    """"Show me recipes for X": the best three go up with their photos, and
    the answer ("B", "the one from Allrecipes", "never mind") decides."""
    import copy
    settings.RECIPE_ENABLED = settings.SEARCH_ENABLED = True
    settings.PINEDA_RECIPE_SECONDS = 120
    recipe_session.clear()
    fake = FakePineda()

    def colada(n, source, images):
        r = copy.deepcopy(COLADA)
        r.title, r.source, r.url, r.images = f"Piña Colada {n}", source, f"https://example.test/{n}", images
        return r

    recipes = [colada(1, "Food Network", ["https://example.test/1a.jpg", "https://example.test/1b.jpg"]),
               colada(2, "Allrecipes", None),            # cached before photos: looked up
               colada(3, "Natasha's Kitchen", []),       # none on its page: left out
               colada(4, "Simply Recipes", ["https://example.test/4a.jpg"])]
    looked = []

    class Finder:
        def cached(self, dish):
            return True

        def find(self, dish):
            return recipes

        def with_photos(self, dish, rs, want=3, look=6):
            for r in rs:
                if r.images is None:
                    looked.append(r.url)
                    r.images = ["https://example.test/2a.jpg"]
            return [r for r in rs if r.images][:want]

    backend = make_backend(fake, [])
    backend.recipes = Finder()
    pipeline.init(backend)

    def say(text):
        return pipeline.dispatch_text(text, conversation_id="rc", quiet=True)

    r = say("show me recipes for mojito")
    assert r["response"] == ("I've pulled up recipes for mojito. They're on Pineda Display now. "
                             "Which one would you like? A, B, or C?"), r["response"]
    assert not r.get("expects_reply") and recipe_session.current() is None   # wake word to answer
    assert looked == ["https://example.test/2"]
    _settle(fake, 1)
    card = fake.takeover
    assert card["kind"] == "recipe_choices" and card["dish"] == "mojito", card
    assert [c["id"] for c in card["choices"]] == ["A", "B", "C"]
    assert [c["source"] for c in card["choices"]] == ["Food Network", "Allrecipes", "Simply Recipes"]
    assert card["choices"][0]["images"] == ["A1", "A2"] and card["choices"][2]["images"] == ["C1"]
    assert [i["id"] for i in card["images"]] == ["A1", "A2", "B1", "C1"]
    assert card["choices"][1]["title"] == "Piña Colada 2"

    # Something else doesn't answer it ...
    assert "Allrecipes" not in say("what time is it")["response"]
    # ... "the one from Allrecipes" does: that recipe opens, and goes up.
    n = len(fake.calls)
    r = say("the one from Allrecipes")
    assert r["response"].startswith("Good choice. I found a recipe for piña colada 2 from Allrecipes."), r
    assert recipe_session.current().recipe.url == "https://example.test/2"
    assert [x.url for x in recipe_session.current().recipes][:2] == [
        "https://example.test/2", "https://example.test/1"]     # "another recipe" still has the rest
    _settle(fake, n + 1)
    assert fake.takeover.get("title") == "Piña Colada 2", fake.takeover
    assert say("B")["response"] != r["response"]                # answered once

    # "Never mind" takes the choices down.
    recipe_session.clear()
    say("show me some mojito recipes")
    _settle(fake, len(fake.calls))
    time.sleep(0.1)
    assert fake.takeover["kind"] == "recipe_choices"
    assert say("never mind")["response"] == "Okay."
    assert fake.takeover is None and recipe_session.current() is None

    # A letter: "C"
    say("show me recipes for mojito")
    time.sleep(0.1)
    assert say("C.")["response"].startswith("Good choice. I found a recipe for piña colada 4")
    # A lone "A" as STT hears it: "Hey." (one word, which would otherwise be dropped as noise)
    recipe_session.clear()
    say("show me recipes for mojito")
    time.sleep(0.1)
    assert say("Hey.")["response"].startswith("Good choice. I found a recipe for piña colada 1")

    # A recipe open from before is closed by new choices: its "yes" and
    # "next" mustn't answer for them.
    recipe_session.start("old dish", [COLADA])
    say("show me recipes for mojito")
    assert recipe_session.current() is None
    say("never mind")

    # STT's stray lead-in ("What's up?") doesn't hide the request.
    recipe_session.clear()
    r = say("What's up? Show me recipes for mojito.")
    assert r["response"].startswith("I've pulled up recipes for mojito."), r["response"]
    say("never mind")

    # No dish named: asked which, and the answer is the dish.
    r = say("What's up? Show me recipes")
    assert r["response"] == "Sure. Recipes for what?" and not r.get("expects_reply"), r
    r = say("Mojito.")
    assert r["response"].startswith("I've pulled up recipes for mojito."), r["response"]
    say("never mind")
    assert say("show me some recipes")["response"] == "Sure. Recipes for what?"
    assert say("never mind")["response"] == "Okay."
    assert recipe_session.current() is None

    # The cookbook keeps fewer than three: the web tops the choices up.
    searched = []

    class Thin(Finder):
        def find(self, dish):
            return recipes[:1]

        def searched(self, dish):
            return False

        def search(self, dish):
            searched.append(dish)
            return recipes

    backend.recipes = Thin()
    r = say("Can you show me a recipe for a mojito?")
    assert searched == ["mojito"] and r["response"].startswith("I've pulled up recipes for mojito"), r
    _settle(fake, len(fake.calls) + 1)
    time.sleep(0.1)
    assert [c["source"] for c in fake.takeover["choices"]][0] == "Food Network"
    say("never mind")
    backend.recipes = Finder()

    # Few photos: still the choices, photos first, the rest without.
    recipe_session.clear()
    for r in recipes[1:]:
        r.images = []
    r = say("show me recipes for mojito")
    assert r["response"].endswith("Which one would you like? A, B, or C?"), r["response"]
    time.sleep(0.1)
    assert [c["images"] for c in fake.takeover["choices"]] == [["A1", "A2"], [], []], fake.takeover
    say("never mind")
    recipe_session.clear()


def test_recipe_box_route():
    """Favorites and history by voice: "favorite this recipe", "show my
    favorite recipes", "make my favorite X", "show my recent recipes"."""
    import copy
    from antigua_core import recipe_box
    settings.RECIPE_ENABLED = settings.SEARCH_ENABLED = True
    recipe_session.clear()
    recipe_box._box = recipe_box.RecipeBox(Path(tempfile.mkdtemp()) / "box.json")
    fake = FakePineda()

    def dish(n, title):
        r = copy.deepcopy(COLADA)
        r.title, r.url, r.images = title, f"https://example.test/box{n}", [f"https://example.test/box{n}.jpg"]
        return r

    menu = {"flan": [dish(1, "Classic Flan"), dish(2, "Coconut Flan")],
            "brownies": [dish(3, "Fudgy Brownies"), dish(4, "Cakey Brownies")],
            "pancakes": [dish(5, "Fluffy Pancakes")], "tacos": [dish(6, "Fish Tacos")],
            "lasagna": [dish(7, "Classic Lasagna")]}

    class Finder:
        def cached(self, d):
            return True

        def find(self, d):
            return menu[d]

        def with_photos(self, d, rs, want=3, look=6):
            return [r for r in rs if r.images][:want]

    backend = make_backend(fake, [])
    backend.recipes = Finder()
    pipeline.init(backend)

    def say(text):
        return pipeline.dispatch_text(text, conversation_id="rb", quiet=True)["response"]

    box = recipe_box.box()
    assert say("favorite this recipe").startswith("There's no recipe open to save.")
    assert "don't have any favorite recipes yet" in say("show my favorite recipes")

    # Picking from the choices is history; "favorite this recipe" keeps it.
    say("show me recipes for flan")
    say("B")
    assert [h["title"] for h in box.api()["history"]] == ["Coconut Flan"]
    assert say("favorite this recipe") == "Saved the coconut flan from Food Network to your favorites."
    assert say("save this recipe to my favorites") == "That one's already a favorite."
    say("stop the recipe")
    for d in ("brownies", "pancakes", "tacos", "lasagna"):
        say(f"show me recipes for {d}")
        say("A")
        say("favorite this recipe")               # the open one
    say("stop the recipe")
    assert [r.title for r in box.favorites()] == ["Classic Lasagna", "Fish Tacos", "Fluffy Pancakes",
                                                  "Fudgy Brownies", "Coconut Flan"]

    # "Show my favorite recipes": the newest four as choices, D picks.
    r = say("show my favorite recipes")
    assert r == ("Here are your favorite recipes. They're on Pineda Display now. Which one would you "
                 "like? A, B, C, or D? There's 1 more in the Recipes tab on the menu."), r
    time.sleep(0.1)
    assert [c["title"] for c in fake.takeover["choices"]] == ["Classic Lasagna", "Fish Tacos",
                                                             "Fluffy Pancakes", "Fudgy Brownies"]
    assert say("D").startswith("Good choice. I found a recipe for fudgy brownies")
    assert recipe_session.current().dish == "brownies"      # the dish it was opened for

    # A favorite by name opens straight away; one not kept says so.
    assert say("make my favorite flan").startswith("Here's your favorite. I found a recipe for coconut flan")
    assert recipe_session.current().recipe.title == "Coconut Flan"
    assert say("make my favorite lemon bars").startswith("You don't have a favorite lemon bars recipe yet.")

    # Out of the favorites again.
    assert say("remove this from my favorites") == \
        "Okay, I took the coconut flan from Food Network out of your favorites."
    assert "Coconut Flan" not in [r.title for r in box.favorites()]

    # Recent: the last four opened, newest first.
    r = say("show my recent recipes")
    assert r.startswith("Here are the recipes you've opened lately."), r
    time.sleep(0.1)
    assert [c["title"] for c in fake.takeover["choices"]] == ["Coconut Flan", "Fudgy Brownies",
                                                             "Classic Lasagna", "Fish Tacos"]
    say("never mind")

    # Read to the last step: cooked.
    say("make my favorite tacos")
    for _ in range(4):
        say("next")
    assert box.api()["history"][0]["title"] == "Fish Tacos" and box.api()["history"][0]["cooked"]

    # The phone menu's "Show on display": opened, its card up.
    rid = recipe_box.recipe_id(menu["lasagna"][0])
    assert pipeline.open_from_menu(rid) == {"opened": rid, "shown": True}
    _settle(fake, len(fake.calls))
    time.sleep(0.1)
    assert fake.takeover["title"] == "Classic Lasagna" and recipe_session.current().dish == "lasagna"
    assert pipeline.open_from_menu("0123456789") is None
    recipe_session.clear()


def test_clock_mode_parser():
    cases = {
        "clock mode": "clock",
        "Alexa, clock mode.": "clock",
        "switch to clock mode": "clock",
        "switch the display to the clock": "clock",
        "put the screen on clock only": "clock",
        "show me the clock": "clock",
        "go back to the clock": "clock",
        "turn on clock mode": "clock",
        "just the clock please": "clock",
        "home": "home",
        "go home": "home",
        "go back home": "home",
        "home screen": "home",
        "switch to home": "home",
        "go back to the dashboard": "home",
        "switch the display back to the home screen": "home",
        "show me the dashboard": "home",
        "exit clock mode": "home",
        "turn off the clock": "home",
        "clock mode off": "home",
        "have the display go back home": "home",
        "what time is it": None,
        "set an alarm clock for 7": None,
        "set the alarm for 7": None,
        "i'm going home": None,
        "when do you get home": None,
        "who is home": None,
        "clockwork orange": None,
        "turn on the home lights": None,
        "set the clock forward an hour": None,
        "stop the display": None,
    }
    for text, want in cases.items():
        assert parse_clock_mode(text) == want, (text, parse_clock_mode(text))


def test_clock_mode_route():
    fake = FakePineda()
    pipeline.init(make_backend(fake, []))

    def say(text):
        return pipeline.dispatch_text(text, conversation_id="clock", quiet=True)["response"]

    assert say("clock mode") == "Okay, clock mode." and fake.calls[-1] == ("hold", True)
    assert say("show me the clock") == "The clock's already up."
    assert say("go back to the dashboard") == "Okay, back to the dashboard."
    assert fake.calls[-1] == ("hold", False)
    assert say("home") == "The display's already on the dashboard."
    fake.takeover = {"title": "Tacos"}
    assert say("switch to clock mode") == "Okay, clock mode."
    assert fake.calls[-2:] == [("clear",), ("hold", True)] and fake.takeover is None
    pipeline.init(make_backend(DownPineda(), []))
    pipeline.B.pineda.hold_clock = lambda on: (_ for _ in ()).throw(pineda.PinedaError("down"))
    assert say("home") == "I can't reach the display right now."
    pipeline.init(make_backend(None, []))
    assert say("clock mode") == "I can't control the display from here."


def test_screen_turn_parser():
    cases = {
        "turn the screen tall": "tall",
        "Alexa, put the display in portrait mode": "tall",
        "make the screen vertical": "tall",
        "turn the display back to landscape": "wide",
        "make the screen wide again": "wide",
        "flip the screen over": "flip",
        "turn the screen upside down": "flip",
        "the screen is upside down": "flip",
        "flip the display": "flip",
        "rotate the screen": "turn",
        "rotate the display please": "turn",
        "turn the screen sideways": "turn",
        # not a turn
        "switch the display to groovy": None,
        "switch the display to clock mode": None,
        "turn the screen off": None,
        "restart the display": None,
        "turn the display": None,
        "flip a coin": None,
        "what's on the screen": None,
    }
    for text, want in cases.items():
        assert parse_screen_turn(text) == want, (text, parse_screen_turn(text))
    assert parse_clock_mode("turn the screen tall") is None
    assert parse_recipe_display("turn the screen tall") is None


def test_screen_turn_route():
    fake = FakePineda()
    pipeline.init(make_backend(fake, []))

    def say(text):
        return pipeline.dispatch_text(text, conversation_id="turn", quiet=True)["response"]

    assert say("turn the screen tall") == "Okay, the screen's tall now."
    assert fake.calls[-1] == ("turn", "tall") and fake.rotation == "90"
    assert say("make the display portrait") == "The screen's already tall."
    assert say("flip the screen over") == "Okay, I flipped it over." and fake.rotation == "270"
    assert say("rotate the screen") == "Okay, I turned the screen." and fake.rotation == "normal"
    pipeline.B.pineda.turn_screen = lambda to: (_ for _ in ()).throw(pineda.PinedaError("down"))
    assert say("turn the screen tall") == "I can't reach the display right now."
    pipeline.init(make_backend(None, []))
    assert say("rotate the screen") == "I can't control the display from here."


def test_timer_mirror_backs_off_a_dead_display():
    class Client:
        def __init__(self, url, up=True):
            self.url, self.up, self.calls = url, up, 0

        def sync_timers(self, body):
            self.calls += 1
            if not self.up:
                raise pineda.PinedaError("timed out")

    now = [1000.0]
    primary, standby = Client("primary"), Client("standby", up=False)
    m = pineda.TimerMirror([primary, standby], lambda: [], clock=lambda: now[0])
    m._push()                                # a timer change: both tried
    for _ in range(120):                     # ten minutes of keep_fresh ticks
        now[0] += 5
        m._push(force=False)
    # standby: 1 + retries after 5 s, 30 s, then every 300 s (at 335 s);
    # primary: the empty list again only every RESEND_EMPTY_S
    assert standby.calls == 4 and primary.calls == 3, (primary.calls, standby.calls)
    standby.up = True
    now[0] += 300
    m._push(force=False)
    m._push()
    assert standby.calls == 6 and primary.calls == 5     # back: every forced push lands


def test_timer_mirror_rides_out_a_restart():
    class Client:
        url, up, calls = "primary", True, 0

        def sync_timers(self, body):
            self.calls += 1
            if not self.up:
                raise pineda.PinedaError("Connection refused")

    now = [1000.0]
    running = [[]]
    c = Client()
    m = pineda.TimerMirror([c], lambda: running[0], clock=lambda: now[0])
    m._push()
    c.up = False
    m._push()                                # mid-restart
    c.up = True
    now[0] += 5
    running[0] = [{"id": "t1", "label": "Pasta", "fires_at": now[0] + 600}]
    m._push(force=False)                     # first tick after the backoff
    assert c.calls == 3
    for _ in range(12):                      # a running timer: re-sent every RESEND_S
        now[0] += 5
        m._push(force=False)
    assert c.calls == 4, c.calls


def test_image_search_and_detail_route():
    settings.SEARCH_ENABLED = True
    fake, llm_seen = FakePineda(), []
    pipeline.init(make_backend(fake, llm_seen))

    def say(text, conv="img_test"):
        return pipeline.dispatch_text(text, conversation_id=conv, quiet=True)

    # 1. Search for pictures
    r1 = say("show me pictures of strawberries")
    assert r1["response"] == "Here are two pictures of strawberries. Ask about A or B.", r1
    assert fake.calls[-1] == ("show", "strawberries", 30)
    assert fake.takeover["kind"] == "image_search"
    assert fake.takeover["highlight"] is None

    # 2. Ask about picture B (letter)
    r2 = say("tell me more about picture B")
    assert "Picture B is Strawberry Tart from unsplash.com. Baked dessert." == r2["response"]
    assert fake.calls[-1] == ("update", {"highlight": "B", "seconds": 30})
    assert fake.takeover["highlight"] == "B"

    # 3. Ask about picture 1 (digit/number word)
    r3 = say("what is the first picture")
    assert "Picture A is Ripe Strawberry from wikimedia.org. Fresh red berries." == r3["response"]
    assert fake.calls[-1] == ("update", {"highlight": "A", "seconds": 30})
    assert fake.takeover["highlight"] == "A"

    # 4. Ask about an invalid picture (not in this search's results)
    r4 = say("what is picture C")
    assert "I don't see picture C. You can ask about A, B." in r4["response"]

    # 5. Screen times out / takeover cleared, but user asks follow up within session window
    fake.takeover = None
    r5 = say("tell me about picture two")
    assert "Picture B is Strawberry Tart" in r5["response"]
    assert fake.calls[-1] == ("show", "strawberries", 30)
    assert fake.takeover["highlight"] == "B"

    # 6. Stop the display dismisses takeover and clears session
    r6 = say("stop the display")
    assert r6["response"] == "Okay."
    assert fake.takeover is None

    # 7. Asking now returns no active image search
    r7 = say("tell me about picture A")
    assert "I don't have an active image search on the screen right now." in r7["response"]


def test_image_nav_steps_through_the_collage():
    settings.SEARCH_ENABLED = True
    fake, llm_seen = FakePineda(), []
    pipeline.init(make_backend(fake, llm_seen))

    def say(text):
        return pipeline.dispatch_text(text, conversation_id="img_nav", quiet=True)

    # Nothing up: "next" isn't the display's, it goes on to the rest of routing.
    say("next")
    assert not any(c[0] == "update" for c in fake.calls), fake.calls

    say("show me pictures of strawberries")
    r = say("next")
    assert r["response"].startswith("Picture A is Ripe Strawberry"), r
    assert fake.takeover["highlight"] == "A"
    assert say("next picture")["response"].startswith("Picture B is")
    assert say("next")["response"].startswith("Picture A is")    # wraps around
    assert say("previous")["response"].startswith("Picture B is")
    r = say("go back")
    assert r["response"] == "Here they all are.", r
    assert fake.calls[-1] == ("update", {"highlight": None, "seconds": 30})
    assert fake.takeover["highlight"] is None
    assert say("back")["response"] == "That's all of them."
    assert say("previous")["response"].startswith("Picture B is")   # from the end
    say("no, go back")

    # "Show me A" / "make A bigger" (as STT writes it, too) opens a picture
    # full screen, even after a stray lead-in sentence.
    for phrase, letter in (("show me B", "B"), ("Show me a.", "A"), ("Show me number 2.", "B"),
                           ("What's up? Make A bigger.", "A"), ("Okay. Show me bee.", "B"),
                           ("make B bigger", "B"), ("Make a bigger.", "A"),
                           ("blow up picture 2", "B"), ("zoom in on bee", "B")):
        r = say(phrase)
        assert r["response"].startswith(f"Picture {letter} is"), (phrase, r)
        assert fake.calls[-1] == ("update", {"highlight": letter, "seconds": 30}), phrase
    assert say("make it bigger")["response"] == "That's as big as it goes."
    assert say("smaller")["response"] == "Here they all are."
    assert say("bigger")["response"] == "Which one? Say a letter, like show me A."

    # Timed out: naming a picture brings the collage back with it open.
    fake.takeover = None
    say("show me B")
    assert fake.takeover["highlight"] == "B"
    assert fake.calls[-1] == ("show", "strawberries", 30)
    say("stop the display")
    assert fake.takeover is None
    # Nothing up and no recent search: the words go on to the rest of routing.
    assert not str(say("make it bigger").get("action", "")).startswith("pineda_image")


def test_image_comparison_route():
    settings.SEARCH_ENABLED = True
    fake, llm_seen = FakePineda(), []
    pipeline.init(make_backend(fake, llm_seen))
    r = pipeline.dispatch_text("show me pictures of fuji vs honeycrisp apples",
                               conversation_id="img_cmp", quiet=True)
    assert r["response"] == ("Here are Fuji apples, A and B, and Honeycrisp apples, C."), r
    assert fake.takeover["groups"] == [{"label": "Fuji apples", "ids": ["A", "B"]},
                                       {"label": "Honeycrisp apples", "ids": ["C"]}]
    # Timed out, then asked about: it comes back still split in two.
    fake.takeover = None
    pipeline.dispatch_text("tell me about picture C", conversation_id="img_cmp", quiet=True)
    assert fake.takeover["highlight"] == "C" and len(fake.takeover["groups"]) == 2
    pipeline.dispatch_text("stop the display", conversation_id="img_cmp", quiet=True)


def main():
    settings.configure({})
    test_parser()
    test_themes()
    test_replies()
    test_timers_payload_and_hook()
    test_concat_wavs()
    test_client_calls_the_display_tools()
    test_route()
    test_recipe_parser()
    test_recipe_card()
    test_recipe_card_highlights()
    test_step_ingredients()
    test_recipe_route()
    test_recipe_choices_route()
    test_recipe_box_route()
    test_clock_mode_parser()
    test_clock_mode_route()
    test_screen_turn_parser()
    test_screen_turn_route()
    test_image_search_and_detail_route()
    test_image_nav_steps_through_the_collage()
    test_image_comparison_route()
    test_timer_mirror_backs_off_a_dead_display()
    test_timer_mirror_rides_out_a_restart()
    print("PASS — pineda display suite")


if __name__ == "__main__":
    main()
