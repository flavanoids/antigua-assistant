#!/usr/bin/env python3
"""End-to-end run of antigua_core.pipeline with a stubbed Backend.

No services needed: STT, LLM and TTS are fakes. This is the parity net for
Phase 2 — the same pipeline the primary and fallback servers run, exercised
through the dispatch table.

Run: python3 tests/test_pipeline.py   (also works under pytest)
"""

import sys
import tempfile
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import pipeline, settings  # noqa: E402
from antigua_core.stores import ListStore, MemoryStore, TimerManager  # noqa: E402


class _FakeSearx:
    def search(self, query, **kw):
        return None, []

    def search_best(self, query, **kw):
        return None, [], query

    def format_for_prompt(self, *a, **kw):
        return ""


def make_backend(transcript, llm_sentences=("Okay.",), identify_speaker=None):
    tmp = Path(tempfile.mkdtemp(prefix="antigua_test_"))

    def transcribe(path):
        return {"text": transcript, "time_s": 0.0, "confidence": 1.0}

    def synthesize(text, lang="en"):
        p = tmp / "out.wav"
        p.write_bytes(b"RIFF")
        return str(p)

    def ask_llm_stream(*a, **kw):
        yield from llm_sentences

    return pipeline.Backend(
        transcribe=transcribe,
        synthesize=synthesize,
        ask_llm_stream=ask_llm_stream,
        audio_url_base=lambda: "http://test:0",
        memory_store=MemoryStore(path=tmp / "memories.json"),
        list_store=ListStore(path=tmp / "lists.json"),
        timers=TimerManager(),
        weather_cache=None,   # weather route not exercised here
        news_cache=None,
        searxng=_FakeSearx(),
        identify_speaker=identify_speaker,
    )


def run(transcript, **kw):
    pipeline.init(make_backend(transcript))
    return pipeline.run_pipeline("/nonexistent.wav", **kw)


HOUSEHOLD = [{"name": "Alex"}, {"name": "Katherine", "nicknames": ["kat"]}]


def main():
    settings.configure({"household": HOUSEHOLD})
    settings.SEARCH_ENABLED = False

    # Volume with no TV or music backend: never falls back to Antigua's own
    r = run("turn it up")
    assert r["response"] == "I can't control the TV right now." and not r.get("action"), r

    # One-word garbage exemption end-to-end (routes to volume, not garbage)
    r = run("louder")
    assert r["response"] == "I can't control the TV right now.", r

    # Timer set: deterministic confirmation, timer actually set
    pipeline.init(make_backend("set a pasta timer for 10 minutes"))
    r = pipeline.run_pipeline("/nonexistent.wav")
    assert r["response"] == "Pasta timer set for 10 minutes.", r
    active = pipeline.B.timers.list_active()
    assert len(active) == 1 and active[0]["label"] == "pasta timer", active
    assert active[0]["kind"] == "timer" and active[0]["name"] == "pasta", active
    pipeline.B.timers.cancel_all()

    # Alarm set: distinct kind, deterministic clock-time confirmation
    pipeline.init(make_backend("set an alarm for 7 am"))
    r = pipeline.run_pipeline("/nonexistent.wav")
    assert r["response"].startswith("Alarm set for 7 AM"), r
    a = pipeline.B.timers.list_active()
    assert len(a) == 1 and a[0]["kind"] == "alarm", a
    pipeline.B.timers.cancel_all()

    # Recurring alarm
    pipeline.init(make_backend("set an alarm for 6:30 every weekday"))
    r = pipeline.run_pipeline("/nonexistent.wav")
    assert r["response"] == "Alarm set for 6:30 AM every weekday.", r
    assert pipeline.B.timers.list_active()[0]["repeat"] == "weekdays"
    pipeline.B.timers.cancel_all()

    # Multi-turn timer flow on one backend (shared TimerManager).
    be = make_backend("")
    pipeline.init(be)

    def turn(text):
        be.transcribe = lambda p, _t=text: {"text": _t, "time_s": 0.0, "confidence": 1.0}
        return pipeline.run_pipeline("/nonexistent.wav")

    turn("set a tea timer for 3 minutes")
    r = turn("cancel my tea timer")
    assert r["response"] == "Cancelled the tea timer." and "streaming" not in r, r
    assert pipeline.B.timers.list_active() == []

    turn("set a rice timer for 10 minutes")
    r = turn("add 5 minutes to the rice timer")
    assert "15 minutes left" in r["response"], r

    r = turn("how much time is left on the rice timer")
    assert "rice timer" in r["response"] and "left" in r["response"], r
    pipeline.B.timers.cancel_all()

    # Telling timers apart: two identical 5-minute timers, by ordinal, by
    # length, "which one?" follow-ups, "the other one", "it".
    def say(text):
        be.transcribe = lambda p, _t=text: {"text": _t, "time_s": 0.0, "confidence": 1.0}
        return pipeline.run_pipeline("/nonexistent.wav", conversation_id="convT")["response"]

    assert say("set a timer for 5 minutes") == "Timer set for 5 minutes."
    assert say("set a timer for 5 minutes") == \
        "Timer set for 5 minutes. That's your second 5-minute timer."
    assert say("how much time is left") == ("You've got your first 5-minute timer at 5 minutes, "
                                            "and your second 5-minute timer at 5 minutes.")
    assert say("how long on the second one") == "Your second 5-minute timer has 5 minutes left."
    assert say("cancel the 5 minute timer") == ("You have two 5-minute timers. Which one? The first, "
                                                "with 5 minutes left, the second, with 5 minutes left, or both?")
    assert say("never mind") == "Okay, I'll leave them."
    assert len(pipeline.B.timers.list_active()) == 2
    say("cancel the timer")
    assert say("the second one") == "Cancelled the second 5-minute timer."
    assert say("how much time is left") == "Your timer has 5 minutes left."
    pipeline.B.timers.cancel_all()

    # Length as an identifier, even after adding time; add no longer sums lengths
    say("set a timer for 5 minutes")
    say("set a timer for ten minutes")
    assert say("add 2 minutes to the 5 minute timer") == \
        "Added 2 minutes. Your 5-minute timer now has 7 minutes left."
    assert say("how much time is left on the five minute timer") == \
        "Your 5-minute timer has 7 minutes left."
    assert say("cancel the 10 minute timer") == "Cancelled the 10-minute timer."
    assert say("cancel the 3 minute timer") == "I couldn't find a 3-minute timer."
    pipeline.B.timers.cancel_all()

    # Named timers without the word "timer"; add asks which, answer by name; "it"
    say("set a timer called eggs for 5 minutes")
    say("set a timer named laundry for 45 minutes")
    assert say("how much time is left on eggs") == "Your eggs timer has 5 minutes left."
    assert say("what timers do I have") == \
        "You've got your eggs timer at 5 minutes, and your laundry timer at 45 minutes."
    assert say("add a minute to the timer") == "Which one? The eggs timer or the laundry timer?"
    assert say("the eggs one") == "Added 1 minute. Your eggs timer now has 6 minutes left."
    assert say("give it 2 more minutes") == "Added 2 minutes. Your eggs timer now has 8 minutes left."
    assert say("cancel the other timer") == "Cancelled the laundry timer."
    say("set a timer for 1 minute")
    assert say("cancel the timer").endswith("or both?")
    assert say("both") == "Cancelled both."
    assert pipeline.B.timers.list_active() == []

    # A new request while a "which one?" is pending routes normally
    say("set a timer for 5 minutes")
    say("set a timer for 5 minutes")
    say("cancel the timer")
    assert say("add milk to the shopping list").startswith("Added milk")
    assert "convT" not in pipeline._pending_timer_choice
    pipeline.B.timers.cancel_all()

    # Alarms: named, by clock, time until, cancel by answer
    assert say("set an alarm for 7 am called gym").startswith("Gym alarm set for 7 AM")
    say("set an alarm for 8 am")
    r = say("when is my gym alarm")
    assert r.startswith("Your gym alarm is set for 7 AM") and "from now." in r, r
    r = say("how much time until my 8 am alarm")
    assert r.startswith("Your alarm is set for 8 AM") and "from now." in r, r
    assert say("cancel the alarm") == "Which one? The gym alarm, the 8 AM alarm, or both?"
    assert say("the gym one") == "Cancelled the gym alarm."
    assert [a["hour"] for a in pipeline.B.timers.list_active()] == [8]
    say("set a timer for 5 minutes")
    assert say("cancel all alarms") == "Cancelled the 8 AM alarm."     # timers untouched
    assert say("how much time is left") == "Your timer has 5 minutes left."
    pipeline.B.timers.cancel_all()

    # Reminder flow: payload survives set -> status -> cancel
    r = turn("remind me to move the laundry in 40 minutes")
    assert r["response"] == "Okay, I'll remind you to move the laundry in 40 minutes.", r
    row = pipeline.B.timers.list_active()[0]
    assert row["kind"] == "reminder" and row["message"] == "move the laundry", row
    r = turn("what are my reminders")
    assert "move the laundry" in r["response"], r
    r = turn("cancel the laundry reminder")
    assert "aundry" in r["response"] and pipeline.B.timers.list_active() == [], r

    # Under-specified reminder: Antigua asks the time, then sets it from the
    # answer on the next turn (same conversation_id, like the satellite sends).
    def cturn(text, cid="convR"):
        be.transcribe = lambda p, _t=text: {"text": _t, "time_s": 0.0, "confidence": 1.0}
        return pipeline.run_pipeline("/x.wav", conversation_id=cid)

    r = cturn("remind me to call the dentist tomorrow")
    assert "what time" in r["response"].lower() and "tomorrow" in r["response"].lower(), r
    assert pipeline.B.timers.list_active() == [], r  # nothing set yet
    r = cturn("3 pm")
    assert r["response"] == "Okay, I'll remind you to call the dentist at 3 PM tomorrow.", r
    row = pipeline.B.timers.list_active()[0]
    assert row["kind"] == "reminder" and row["hour"] == 15, row
    pipeline.B.timers.cancel_all()

    # ...and the user can back out of the time prompt
    cturn("remind me to call the plumber on friday", cid="convR2")
    r = cturn("never mind", cid="convR2")
    assert r["response"] == "Okay, no reminder." and pipeline.B.timers.list_active() == [], r

    # Garbage on a follow-up turn ends silently
    r = run("uh", follow_up=True)
    assert r["end_conversation"] is True and r["response"] == "", r

    # No words at all (false wake word on music) ends silently too
    r = run("")
    assert r["end_conversation"] is True and r["response"] == "", r

    # Garbage on a normal turn speaks a retry prompt
    r = run("uh")
    assert r["response"] and "end_conversation" not in r, r

    # Roku without the capability: spoken refusal, no crash (fallback path)
    r = run("mute the tv")
    assert r["response"] == "I can't control the TV right now.", r

    # Govee without the capability
    r = run("turn on the chandelier")
    assert r["response"] == "I can't control the lights right now.", r

    # Music without Music Assistant (no token): spoken refusal
    r = run("play Beyonce")
    assert r["response"] == "I can't play music right now.", r

    # TV + Govee through MCP, against a fake hub that records tool calls.
    from antigua_core.home_control import HomeControl
    from antigua_core.mcp_client import McpResult

    class _FakeHub:
        def __init__(self, roku_ok=True, atv_ok=True):
            self.calls, self.roku_ok, self.atv_ok = [], roku_ok, atv_ok

        def get(self, name):
            return object()

        def call(self, server, tool, arguments=None, timeout=None):
            self.calls.append((server, tool, arguments or {}))
            if server == "roku":
                return McpResult(ok=True, text="Successfully sent." if self.roku_ok else "Failed to send.")
            if server == "appletv":
                if not self.atv_ok:
                    return McpResult(ok=False, text="Could not establish a full connection")
                if tool == "list_apps":
                    return McpResult(ok=True, text='[{"name": "Netflix", "bundle_id": "com.netflix.Netflix"},'
                                                   ' {"name": "Disney+", "bundle_id": "com.disney.disneyplus"}]')
                if tool == "get_volume":
                    return McpResult(ok=True, text="42.0")
                return McpResult(ok=True, text="Done")
            return McpResult(ok=True, text='{"ok": true, "transport": "lan"}')

    home_cfg = {
        "living_room_tv": {
            "appletv_device": "Living Room",
            "providers": {"power": ["appletv"], "volume": ["appletv"], "mute": ["appletv"],
                          "apps": ["appletv"], "navigation": ["appletv"],
                          "playback": ["appletv"], "inputs": ["roku"]},
            "inputs": {"playstation 5": "InputHDMI1"}},
        "govee": {"devices": {"hallway-1": "H1", "hallway-2": "H2", "tv-bar": "TB"}},
    }

    def home_run(text, hub, cfg=home_cfg):
        be = make_backend(text)
        be.home = HomeControl(hub, cfg)
        pipeline.init(be)
        return pipeline.run_pipeline("/x.wav")

    LR = {"device": "Living Room"}
    hub = _FakeHub()
    r = home_run("turn on the tv", hub)
    assert r["response"] == "Turning on the Living Room TV", r
    assert hub.calls == [("appletv", "turn_on", LR)], hub.calls
    hub.calls.clear()
    home_run("turn the apple tv off", hub)
    assert hub.calls == [("appletv", "turn_off", LR)], hub.calls
    hub.calls.clear()
    home_run("tv volume down", hub)
    assert hub.calls == [("appletv", "volume_down", LR)], hub.calls
    hub.calls.clear()
    home_run("go back on the tv", hub)
    assert hub.calls == [("appletv", "navigate", {"direction": "menu", **LR})], hub.calls
    hub.calls.clear()
    r = home_run("pause the tv", hub)
    assert r["response"] == "Pausing the Living Room TV", r
    assert hub.calls == [("appletv", "pause", LR)], hub.calls
    # Inputs are the Roku's job
    hub.calls.clear()
    r = home_run("switch to the playstation", hub)
    assert r["response"] == "Switching to Playstation 5", r
    assert hub.calls == [("roku", "press_key", {"key_name": "InputHDMI1"})], hub.calls
    # Apps resolve against the Apple TV's installed list, launched by bundle ID
    hub.calls.clear()
    r = home_run("open disney plus", hub)
    assert r["response"] == "Opening Disney Plus", r
    assert hub.calls[-1] == ("appletv", "launch_app", {"app": "com.disney.disneyplus", **LR}), hub.calls
    r = home_run("open plex", hub)
    assert r["response"] == "I don't see Plex on the Living Room TV.", r
    # Bare volume: the TV when no music is playing, the music player when it is
    class _FakeMusic:
        def __init__(self, on):
            self.on, self.handled = on, []
        def playing(self):
            return self.on
        def handle(self, intent, rehear=None):
            self.handled.append(intent.action)
            return ""
    for text, action in (("turn it up", "volume_up"), ("too loud", "volume_down")):
        hub = _FakeHub()
        be = make_backend(text)
        be.home, be.music = HomeControl(hub, home_cfg), _FakeMusic(False)
        pipeline.init(be)
        r = pipeline.run_pipeline("/x.wav")
        assert r["action"] == f"tv_{action}" and hub.calls == [("appletv", action, LR)], (r, hub.calls)
        be.music = _FakeMusic(True)
        hub.calls.clear()
        r = pipeline.run_pipeline("/x.wav")
        assert r["action"] == f"music_{action}" and be.music.handled == [action], r
        assert hub.calls == [], hub.calls
    # Second opinion: over music, a short transcript no skill claims is
    # re-transcribed by the careful model; used only if that one routes.
    careful = []
    def run_with(heard, better, on):
        be = make_backend(heard)
        be.music = _FakeMusic(on)
        be.transcribe_careful = lambda p: careful.append(p) or {"text": better, "time_s": 2.0}
        pipeline.init(be)
        return be, pipeline.run_pipeline("/x.wav")
    be, r = run_with("Follow us.", "Pause.", True)
    assert r["action"] == "music_pause" and be.music.handled == ["pause"], r
    careful.clear()
    be, r = run_with("Follow us.", "Pause.", False)           # no music: no second pass
    assert not careful and be.music.handled == [], r
    be, r = run_with("tell me a joke", "tell me a joke", True)  # careful agrees: LLM as usual
    assert careful and be.music.handled == [], r
    careful.clear()
    be, r = run_with("turn on the tv", "x", True)             # already a skill: untouched
    assert not careful, careful
    # Spanish: language picking. Short or unsure clips stay English.
    pick = pipeline.pick_language
    assert pick({"en": 0.06, "es": 0.94}, "Pon música de Bad Bunny") == "es"
    assert pick({"en": 0.72, "tr": 0.16, "es": 0.01}, "Pause.") == "en"
    assert pick({"en": 0.02, "es": 0.95}, "Pausa.") == "en"            # one word
    assert pick({"en": 0.02, "es": 0.95}, "Pausa.", prefer="es") == "es"  # already Spanish
    assert pick({"en": 0.30, "es": 0.60}, "qué hora es") == "en"         # not confident
    assert pick({"tr": 0.9, "en": 0.05, "es": 0.02}, "x y z") == "en"
    assert pick({}, "anything") == "en"                                   # CPU fallback
    # A Spanish turn reaches the LLM and TTS as Spanish, and the next turn in
    # that conversation asks STT to prefer Spanish.
    be = make_backend("")
    seen = {"prefer": [], "llm": [], "tts": []}
    def transcribe_es(p, prefer=None):
        seen["prefer"].append(prefer)
        return {"text": "cuéntame un chiste", "language": "es", "time_s": 0.0}
    def llm_es(*a, language="en", **kw):
        seen["llm"].append(language)
        yield "Claro."
    _synth = be.synthesize
    be.transcribe, be.ask_llm_stream = transcribe_es, llm_es
    be.synthesize = lambda text, lang="en": seen["tts"].append(lang) or _synth(text)
    pipeline.init(be)
    pipeline.run_pipeline("/x.wav", conversation_id="convES")
    pipeline.run_pipeline("/x.wav", conversation_id="convES")
    assert seen == {"prefer": [None, "es"], "llm": ["es", "es"], "tts": ["es", "es"]}, seen
    be.transcribe = lambda p, prefer=None: {"text": "tell me a joke", "language": "en", "time_s": 0.0}
    pipeline.run_pipeline("/x.wav", conversation_id="convES")   # back to English
    assert seen["llm"][-1] == "en" and "convES" not in pipeline._conv_language
    # Spanish commands (Phase 2): run as the English command, answered in
    # Spanish — a template where there is one, else the LLM translation, else
    # English. A command too short for STT's language detection still counts.
    be = make_backend("¿Qué hora es?")
    tts_langs = []
    _synth = be.synthesize
    be.synthesize = lambda text, lang="en": tts_langs.append(lang) or _synth(text)
    be.translate_to_spanish = lambda text: f"(es) {text}"
    pipeline.init(be)
    r = pipeline.run_pipeline("/x.wav", conversation_id="convES2")
    assert r["response"].startswith(("Son las", "Es la")) and tts_langs[-1] == "es", r
    assert pipeline._conv_language["convES2"]["lang"] == "es"
    be.transcribe = lambda p, prefer=None: {"text": "¿Cuánto le falta al temporizador?", "time_s": 0.0}
    r = pipeline.run_pipeline("/x.wav")                 # no template for this reply
    assert r["response"].startswith("(es) ") and tts_langs[-1] == "es", r
    be.translate_to_spanish = lambda text: None         # translation failed: English
    r = pipeline.run_pipeline("/x.wav")
    assert not r["response"].startswith("(es)") and tts_langs[-1] == "en", r
    # Mute: remember the level, set 0; unmute restores it (one HomeControl)
    hub = _FakeHub()
    be = make_backend("mute the tv")
    be.home = HomeControl(hub, home_cfg)
    pipeline.init(be)
    assert pipeline.run_pipeline("/x.wav")["response"] == "Muting the Living Room TV"
    be.transcribe = lambda p: {"text": "unmute the tv", "time_s": 0.0, "confidence": 1.0}
    pipeline.run_pipeline("/x.wav")
    assert hub.calls == [("appletv", "get_volume", LR), ("appletv", "set_volume", {"level": 0, **LR}),
                         ("appletv", "set_volume", {"level": 42.0, **LR})], hub.calls
    # Unreachable Apple TV as the only provider: spoken failure, and it's
    # tried again next time (no cooldown without a backup to use instead)
    hub = _FakeHub(atv_ok=False)
    be = make_backend("turn on the tv")
    be.home = HomeControl(hub, home_cfg)
    pipeline.init(be)
    assert pipeline.run_pipeline("/x.wav")["response"].startswith("The Living Room TV did not respond")
    pipeline.run_pipeline("/x.wav")
    assert len(hub.calls) == 2, hub.calls
    # With a backup listed, the backup takes over, and during the cooldown the
    # failed provider is skipped outright
    fb_cfg = {**home_cfg, "living_room_tv": {**home_cfg["living_room_tv"],
              "providers": {"power": ["appletv", "roku"]}}}
    hub = _FakeHub(atv_ok=False)
    be = make_backend("turn on the tv")
    be.home = HomeControl(hub, fb_cfg)
    pipeline.init(be)
    assert pipeline.run_pipeline("/x.wav")["response"] == "Turning on the Living Room TV"
    assert [c[:2] for c in hub.calls] == [("appletv", "turn_on"), ("roku", "power_on")], hub.calls
    pipeline.run_pipeline("/x.wav")
    assert [c[:2] for c in hub.calls[2:]] == [("roku", "power_on")], hub.calls

    # Govee: optimistic reply; each device gets power-on then the setting, by ID.
    import time as _time
    hub = _FakeHub()
    r = home_run("set the hallway lights to 50 percent", hub)
    assert r["response"] == "Setting the hallway lights to 50 percent", r
    for _ in range(50):
        if len(hub.calls) == 4:
            break
        _time.sleep(0.01)
    assert sorted(hub.calls, key=str) == sorted([
        ("govee", "set_power", {"name": "H1", "on": True}),
        ("govee", "set_brightness", {"name": "H1", "level": 50}),
        ("govee", "set_power", {"name": "H2", "on": True}),
        ("govee", "set_brightness", {"name": "H2", "level": 50}),
    ], key=str), hub.calls

    # Alarm flash: snapshot, run the kind's scene, restore white mode.
    class _StateHub(_FakeHub):
        def call(self, server, tool, arguments=None, timeout=None):
            if tool == "get_device_state":
                self.calls.append((server, tool, arguments or {}))
                return McpResult(ok=True, text='{"power": "on", "brightness": 80, "color": '
                                               '{"r": 255, "g": 255, "b": 255}, "color_temp_k": 2700}')
            return super().call(server, tool, arguments, timeout)
    hub = _StateHub()
    flash_cfg = {"govee": {"devices": {"tv-bar": "TB"}, "alarm_flash": {
        "enabled": True, "devices": ["tv-bar"], "seconds": 0,
        "scenes": {"alarm": "Siren", "timer": "Breathe"}}}}
    HomeControl(hub, flash_cfg).govee_flash("reminder")   # no reminder scene → timer's
    TB = {"name": "TB"}
    assert hub.calls == [
        ("govee", "get_device_state", TB),
        ("govee", "set_scene", {**TB, "scene": "Breathe"}),
        ("govee", "set_color_temp", {**TB, "kelvin": 2700}),
        ("govee", "set_brightness", {**TB, "level": 80}),
    ], hub.calls
    hub.calls.clear()
    HomeControl(hub, {"govee": {"devices": {"tv-bar": "TB"}}}).govee_flash("alarm")
    assert hub.calls == [], hub.calls   # off unless alarm_flash.enabled

    # Memory save disambiguation across two turns (same conversation)
    pipeline.init(make_backend("remember that I took ibuprofen"))
    r1 = pipeline.run_pipeline("/x.wav", conversation_id="conv1")
    assert "Alex or Katherine" in r1["response"], r1
    pipeline.B.transcribe = lambda p: {"text": "Alex", "time_s": 0.0, "confidence": 1.0}
    r2 = pipeline.run_pipeline("/x.wav", conversation_id="conv1")
    assert "I'll remember Alex" in r2["response"] and "ibuprofen" in r2["response"], r2

    # Lists: add (multi-item), query, remove, clear — full roundtrip on one store
    backend = make_backend("add milk, eggs and bread to the shopping list")
    pipeline.init(backend)
    r = pipeline.run_pipeline("/x.wav")
    assert r["response"] == "Added milk, eggs, and bread to your shopping list.", r

    backend.transcribe = lambda p: {"text": "what's on my shopping list", "time_s": 0.0, "confidence": 1.0}
    r = pipeline.run_pipeline("/x.wav")
    assert r["response"] == "On your shopping list: milk, eggs, and bread.", r

    backend.transcribe = lambda p: {"text": "remove eggs from the shopping list", "time_s": 0.0, "confidence": 1.0}
    r = pipeline.run_pipeline("/x.wav")
    assert r["response"] == "Removed eggs from your shopping list.", r

    backend.transcribe = lambda p: {"text": "remove eggs from the shopping list", "time_s": 0.0, "confidence": 1.0}
    r = pipeline.run_pipeline("/x.wav")
    assert r["response"] == "I don't see eggs on your shopping list.", r

    backend.transcribe = lambda p: {"text": "clear the shopping list", "time_s": 0.0, "confidence": 1.0}
    r = pipeline.run_pipeline("/x.wav")
    assert r["response"] == "Cleared 2 items from your shopping list.", r

    backend.transcribe = lambda p: {"text": "what's on my shopping list", "time_s": 0.0, "confidence": 1.0}
    r = pipeline.run_pipeline("/x.wav")
    assert r["response"] == "Your shopping list is empty.", r

    # A named list ("todo") and default ("shopping") don't collide
    backend.transcribe = lambda p: {"text": "add call the vet to my to-do list", "time_s": 0.0, "confidence": 1.0}
    r = pipeline.run_pipeline("/x.wav")
    assert r["response"] == "Added call the vet to your to-do list.", r
    backend.transcribe = lambda p: {"text": "what's on my shopping list", "time_s": 0.0, "confidence": 1.0}
    r = pipeline.run_pipeline("/x.wav")
    assert r["response"] == "Your shopping list is empty.", r

    # Speaker ID: confident match skips the disambiguation turn entirely
    backend = make_backend("remember I took my vitamins", identify_speaker=lambda p: "Katherine")
    pipeline.init(backend)
    r = pipeline.run_pipeline("/x.wav")
    assert "Alex or Katherine" not in r["response"], r
    assert "Katherine" in r["response"] and "vitamins" in r["response"], r

    # An explicit name in the transcript always wins over speaker ID, even
    # when they disagree — the user said who they mean.
    backend = make_backend("remember that Alex took his vitamins", identify_speaker=lambda p: "Katherine")
    pipeline.init(backend)
    r = pipeline.run_pipeline("/x.wav")
    assert "I'll remember Alex" in r["response"], r

    # Safety property: identify_speaker returning None (unconfident/unenrolled)
    # falls through to the existing disambiguation prompt — no new failure mode.
    backend = make_backend("remember I took my vitamins", identify_speaker=lambda p: None)
    pipeline.init(backend)
    r = pipeline.run_pipeline("/x.wav")
    assert "Alex or Katherine" in r["response"], r

    # No identify_speaker capability at all (the fallback server's case) — same
    # disambiguation behavior as before Phase 4, not a crash.
    backend = make_backend("remember I took my vitamins")  # identify_speaker=None (default)
    pipeline.init(backend)
    r = pipeline.run_pipeline("/x.wav")
    assert "Alex or Katherine" in r["response"], r

    # A raising identify_speaker must not take down the pipeline — the
    # transcript still gets answered.
    def _boom(path):
        raise RuntimeError("model exploded")
    backend = make_backend("remember I took my vitamins", identify_speaker=_boom)
    pipeline.init(backend)
    r = pipeline.run_pipeline("/x.wav")
    assert "Alex or Katherine" in r["response"], r

    # Unmatched utterance streams through the LLM tail
    pipeline.init(make_backend("tell me a joke", llm_sentences=("Here is a joke.",)))
    r = pipeline.run_pipeline("/x.wav")
    assert r["streaming"] is True and r["response"] == "Here is a joke.", r

    # Multi-sentence answer: first sentence is synthesized alone, the rest as one
    # batched utterance — two synth calls, full text in the response.
    b = make_backend("tell me about rome",
                     llm_sentences=("Rome fell in 476.", "Then came the dark ages.",
                                    "Byzantium lasted longer."))
    synth_calls = []
    _orig = b.synthesize
    b.synthesize = lambda text, lang="en": synth_calls.append(text) or _orig(text)
    pipeline.init(b)
    r = pipeline.run_pipeline("/x.wav")
    assert r["response"] == "Rome fell in 476. Then came the dark ages. Byzantium lasted longer.", r
    assert synth_calls == ["Rome fell in 476.",
                           "Then came the dark ages. Byzantium lasted longer."], synth_calls

    # Weather: the deterministic skill answers via a stub provider, no LLM.
    from datetime import datetime, timedelta
    from antigua_core import weather as w

    class _StubProvider:
        home = w.Location(29.76, -95.37, "America/Chicago", kind="home")

        def get(self, loc=None):
            base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
            days = [w.Day(d=base.date() + timedelta(days=i), high_f=90, low_f=70,
                          code=2, rain_pct=70 if i == 0 else 0, sunrise="6:20 AM",
                          sunset="8:20 PM") for i in range(7)]
            hourly = [w.Hour(dt=base + timedelta(hours=h), temp_f=80,
                             rain_pct=70 if 3 <= h <= 8 else 0, code=2)
                      for h in range(48)]
            return w.Forecast(location=self.home, tz="America/Chicago",
                              current=w.Conditions(85, 88, 50, 5, 2),
                              hourly=hourly, days=days)

    be = make_backend("is it going to rain today")
    be.weather_provider = _StubProvider()
    pipeline.init(be)
    r = pipeline.run_pipeline("/x.wav")
    assert "70 percent" in r["response"] and "streaming" not in r, r

    # No provider (fallback backend) + no wttr cache -> graceful, not a crash.
    be = make_backend("is it going to rain today")
    be.weather_provider = None
    be.weather_cache = type("_C", (), {"get": lambda self, *a: None})()
    pipeline.init(be)
    r = pipeline.run_pipeline("/x.wav")
    assert r["response"], r

    # Time queries say "noon"/"midnight" instead of "12:00 PM"/"12:00 AM".
    class _FixedDatetime(datetime):
        _fixed = None

        @classmethod
        def now(cls, tz=None):
            return cls._fixed

    orig_datetime = pipeline.datetime
    pipeline.datetime = _FixedDatetime
    try:
        _FixedDatetime._fixed = datetime(2026, 9, 13, 12, 0)
        assert pipeline.format_time_date_response("what time is it") == "It's noon."
        _FixedDatetime._fixed = datetime(2026, 9, 13, 0, 0)
        assert pipeline.format_time_date_response("what time is it") == "It's midnight."
        _FixedDatetime._fixed = datetime(2026, 9, 13, 13, 5)
        assert pipeline.format_time_date_response("what time is it") == "It's 1:05 PM."
        _FixedDatetime._fixed = datetime(2026, 9, 13, 12, 30)
        assert pipeline.format_time_date_response("what time is it") == "It's 12:30 PM."
    finally:
        pipeline.datetime = orig_datetime

    print("PASS — pipeline stub suite")


if __name__ == "__main__":
    main()
