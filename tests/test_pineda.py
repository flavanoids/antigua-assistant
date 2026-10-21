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
    parse_clock_mode, parse_pineda_request, parse_recipe_display)  # noqa: E402
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
        {"id": "a1", "label": "pasta", "kind": "timer", "fires_at": "1970-01-01T00:00:00+00:00"},
        {"id": "b2", "label": "Timer", "kind": "timer", "fires_at": "1970-01-01T00:01:00+00:00"},
        {"id": "c3", "label": "Alarm", "kind": "alarm", "fires_at": "1970-01-01T00:01:30+00:00"},
        {"id": "d4", "label": "gym", "kind": "alarm", "fires_at": "1970-01-01T00:02:00+00:00"},
        {"id": "e5", "label": "call mom", "kind": "reminder",
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

    def show_image_collage(self, query, images, seconds=120):
        self.show_takeover({"kind": "image_search", "query": query, "images": images, "highlight": None}, seconds)

    def highlight_image(self, identifier):
        self.update_takeover({"highlight": identifier.upper()})

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

    # Opening a recipe puts it up for PINEDA_RECIPE_SECONDS.
    say("give me a recipe for pina colada")
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
    assert "Here are some pictures of strawberries" in r1["response"], r1
    assert fake.calls[-1] == ("show", "strawberries", 120)
    assert fake.takeover["kind"] == "image_search"
    assert fake.takeover["highlight"] is None

    # 2. Ask about picture B (letter)
    r2 = say("tell me more about picture B")
    assert "Picture B is Strawberry Tart from unsplash.com. Baked dessert." == r2["response"]
    assert fake.calls[-1] == ("update", {"highlight": "B"})
    assert fake.takeover["highlight"] == "B"

    # 3. Ask about picture 1 (digit/number word)
    r3 = say("what is the first picture")
    assert "Picture A is Ripe Strawberry from wikimedia.org. Fresh red berries." == r3["response"]
    assert fake.calls[-1] == ("update", {"highlight": "A"})
    assert fake.takeover["highlight"] == "A"

    # 4. Ask about an invalid picture (not in this search's results)
    r4 = say("what is picture C")
    assert "I don't see picture C. You can ask about A, B." in r4["response"]

    # 5. Screen times out / takeover cleared, but user asks follow up within session window
    fake.takeover = None
    r5 = say("tell me about picture two")
    assert "Picture B is Strawberry Tart" in r5["response"]
    assert fake.calls[-1] == ("show", "strawberries", 120)
    assert fake.takeover["highlight"] == "B"

    # 6. Stop the display dismisses takeover and clears session
    r6 = say("stop the display")
    assert r6["response"] == "Okay."
    assert fake.takeover is None

    # 7. Asking now returns no active image search
    r7 = say("tell me about picture A")
    assert "I don't have an active image search on the screen right now." in r7["response"]


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
    test_clock_mode_parser()
    test_clock_mode_route()
    test_image_search_and_detail_route()
    test_timer_mirror_backs_off_a_dead_display()
    test_timer_mirror_rides_out_a_restart()
    print("PASS — pineda display suite")


if __name__ == "__main__":
    main()
