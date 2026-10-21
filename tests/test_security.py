#!/usr/bin/env python3
"""Security skill (Eufy cameras + front door lock via Home Assistant):
parsing, the event log, the livestream lifecycle, and the route end to end
against a fake HA and a fake pineda-web.

Run: python3 tests/test_security.py   (also works under pytest)
"""

import sys
import tempfile
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import pipeline, security, settings  # noqa: E402
from antigua_core.intents.security import parse_security_request  # noqa: E402
from antigua_core.stores import ListStore, MemoryStore, TimerManager  # noqa: E402


def test_parser():
    cases = {
        "lock the front door": ("lock", None),
        "can you lock the door": ("lock", None),
        "lock up": ("lock", None),
        "unlock the front door": ("unlock", None),
        "is the front door locked": ("lock_state", None),
        "did I lock the door": ("lock_state", None),
        "when was the front door locked": ("lock_when", "locked"),
        "when was the door last unlocked": ("lock_when", "unlocked"),
        "show me the front door": ("show_camera", "front_door"),
        "pull up the doorbell cam": ("show_camera", "front_door"),
        "show the garage camera": ("show_camera", "garage"),
        "let me see the driveway": ("show_camera", "garage"),
        "who's at the door": ("show_camera", "front_door"),
        "who triggered the garage cam": ("activity", "garage"),
        "when did someone ring the doorbell": ("activity", "front_door"),
        "how's the doorbell battery": ("battery", "front_door"),
        "lock battery": ("battery", "lock"),
    }
    for text, want in cases.items():
        assert parse_security_request(text) == want, (text, parse_security_request(text))
    for text in ("add a lock to the shopping list", "turn on the porch light",
                 "open the garage door", "show me the garage door", "play the doors",
                 "show me the recipe", "lock in on that", "what time is it"):
        assert parse_security_request(text) is None, text


class FakeHA:
    def __init__(self):
        self.s = {
            "lock.front_door": "unavailable",
            "sensor.front_door_lock_status": "Unlocked",
            "sensor.front_door_battery_percentage": "65",
            "sensor.doorbell_battery_percentage": "17",
            "sensor.garage_battery_percentage": "99",
            "binary_sensor.doorbell_ringing": "off",
            "binary_sensor.doorbell_motion_detected": "off",
            "binary_sensor.doorbell_person_detected": "off",
            "binary_sensor.garage_motion_detected": "off",
            "binary_sensor.garage_person_detected": "off",
            "sensor.doorbell_charging_status": "Not Charging",
        }
        self.calls = []

    def states(self):
        return {e: {"entity_id": e, "state": v} for e, v in self.s.items()}

    def state(self, e):
        return {"entity_id": e, "state": self.s[e]} if e in self.s else None

    def call(self, domain, service, entity_id, **data):
        self.calls.append((domain, service, entity_id))
        if (domain, service) == ("lock", "lock"):
            self.s["lock.front_door"] = "locked"

    def restart_addon(self, slug):
        self.calls.append(("restart", slug))


class FakePineda:
    """pineda-web's MCP tools as PinedaClient exposes them: it runs the
    livestream itself while a camera card is up."""

    def __init__(self):
        self.calls = []
        self.streaming = None
        self.seconds = None

    def show_camera(self, name, seconds, pip=None):
        self.calls.append(("show", "camera", name))
        self.streaming, self.seconds = name, seconds
        return {"camera": name}

    def caption_camera(self, text):
        self.calls.append(("caption", text))

    def status(self):
        return {"camera_streaming": self.streaming}

    def clear_takeover(self):
        self.calls.append(("clear",))
        self.streaming = None


def _config():
    return security.Config.from_dict({
        "live_seconds": 30,
        "eufy_addon": "eufy_ws",
        "cameras": {
            "front_door": {"entity": "camera.doorbell", "title": "Front door", "stream": "SN1",
                           "battery": "sensor.doorbell_battery_percentage",
                           "motion": "binary_sensor.doorbell_motion_detected",
                           "person": "binary_sensor.doorbell_person_detected",
                           "ring": "binary_sensor.doorbell_ringing",
                           "charging": "sensor.doorbell_charging_status"},
            "garage": {"entity": "camera.garage", "title": "Garage", "stream": "SN2",
                       "battery": "sensor.garage_battery_percentage",
                       "motion": "binary_sensor.garage_motion_detected",
                       "person": "binary_sensor.garage_person_detected"},
        },
        "lock": {"entity": "lock.front_door", "status": "sensor.front_door_lock_status",
                 "battery": "sensor.front_door_battery_percentage"},
    })


def _security(ha=None, pineda=None, cfg=None, announce=None):
    tmp = Path(tempfile.mkdtemp(prefix="antigua_security_"))
    return security.Security(ha or FakeHA(), cfg or _config(),
                             security.EventLog(tmp / "events.db"), pineda=pineda or FakePineda(),
                             announce=announce)


def test_event_log_and_poller():
    ha = FakeHA()
    S = _security(ha)
    S.poll_once(ha.states())                 # first look: nothing logged
    assert S.events.last("front_door_lock", ("locked", "unlocked")) is None
    ha.s["sensor.front_door_lock_status"] = "Locked"
    S.poll_once(ha.states())
    assert S.events.last("front_door_lock", ("locked",))["source"] == ""
    ha.s["binary_sensor.doorbell_ringing"] = "on"
    S.poll_once(ha.states())
    S.poll_once(ha.states())                 # still on: one ring, not two
    ha.s["binary_sensor.doorbell_ringing"] = "off"
    S.poll_once(ha.states())
    with S.events._db() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE event='ring'").fetchone()[0] == 1
    # The ring put the doorbell's live video up, once.
    for _ in range(30):
        if S.pineda.calls:
            break
        time.sleep(0.1)
    assert S.pineda.calls == [("show", "camera", "front_door")]
    S.pineda.clear_takeover()


def test_motion_and_person_dont_take_over():
    ha = FakeHA()
    S = _security(ha)
    S.poll_once(ha.states())
    ha.s["binary_sensor.doorbell_person_detected"] = "on"
    S.poll_once(ha.states())
    time.sleep(0.3)
    assert S.pineda.calls == []


def _restarts(ha):
    time.sleep(0.2)
    return [c for c in ha.calls if c[0] == "restart"]


def test_a_finished_charge_restarts_the_eufy_bridge():
    ha = FakeHA()
    S = _security(ha)
    S.poll_once(ha.states())
    ha.s["sensor.doorbell_charging_status"] = "Charging"
    S.poll_once(ha.states())
    assert _restarts(ha) == []
    # The restart itself turns the lock unavailable; its return isn't a lock event.
    ha.s["sensor.front_door_lock_status"] = "Locked"
    S.poll_once(ha.states())
    ha.s["sensor.doorbell_charging_status"] = "unavailable"
    S.poll_once(ha.states())
    assert _restarts(ha) == []
    # A live view is using the bridge: the restart waits for it.
    S.pineda.streaming = "front_door"
    ha.s["sensor.doorbell_charging_status"] = "Not Charging"
    S.poll_once(ha.states())
    assert _restarts(ha) == []
    S.pineda.streaming = None
    ha.s["sensor.front_door_lock_status"] = "unavailable"
    S.poll_once(ha.states())
    assert _restarts(ha) == [("restart", "eufy_ws")]
    ha.s["sensor.front_door_lock_status"] = "Locked"
    S.poll_once(ha.states())
    with S.events._db() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE event='locked'").fetchone()[0] == 1
    # Another charge within the hour doesn't restart it again.
    for state in ("Charging", "Not Charging"):
        ha.s["sensor.doorbell_charging_status"] = state
        S.poll_once(ha.states())
    assert _restarts(ha) == [("restart", "eufy_ws")]


def test_lock_credits_the_voice_and_never_unlocks():
    ha = FakeHA()
    S = _security(ha)
    S.poll_once(ha.states())
    # No operate permission yet (lock entity unavailable): it says so, does nothing.
    assert S.lock("voice:Sam") == "unavailable"
    assert ("lock", "lock", "lock.front_door") not in ha.calls
    ha.s["lock.front_door"] = "unlocked"
    assert S.lock("voice:Sam") == "locking"
    assert ("lock", "lock", "lock.front_door") in ha.calls
    S.poll_once(ha.states())
    assert S.events.last("front_door_lock", ("locked",))["source"] == "voice:Sam"
    assert S.lock("voice:Sam") == "already"
    assert not any(service == "unlock" for _, service, _ in ha.calls)


def test_the_display_runs_the_livestream():
    # pineda-web starts and stops the stream with its card: we only ask.
    ha, P = FakeHA(), FakePineda()
    S = _security(ha, P)
    S.show_camera("front_door")
    assert P.calls == [("show", "camera", "front_door")] and P.seconds == 30
    assert not any(c[0] == "eufy_security" for c in ha.calls)
    try:
        S.show_camera("attic")
        raise AssertionError("an unknown camera should raise")
    except security.SecurityError:
        pass


def test_a_pushed_change_acts_once():
    # HA pushes one entity at a time; the poller then sees the same state
    # and doesn't act again.
    ha, P = FakeHA(), FakePineda()
    S = _security(ha, P)
    S.poll_once(ha.states())
    ha.s["binary_sensor.doorbell_ringing"] = "on"
    S.poll_once({"binary_sensor.doorbell_ringing": ha.states()["binary_sensor.doorbell_ringing"]})
    S.poll_once(ha.states())
    for _ in range(30):
        if P.calls:
            break
        time.sleep(0.05)
    time.sleep(0.1)
    assert P.calls == [("show", "camera", "front_door")] and P.seconds == 30


def test_motion_at_the_front_door_puts_it_up():
    cfg = _config()
    cfg.cameras["front_door"].show_on_motion = 30
    ha, P = FakeHA(), FakePineda()
    S = _security(ha, P, cfg=cfg)
    S.poll_once(ha.states())

    def wait_for(n):
        for _ in range(30):
            if len(P.calls) >= n:
                return
            time.sleep(0.05)

    ha.s["binary_sensor.doorbell_motion_detected"] = "on"
    ha.s["binary_sensor.doorbell_person_detected"] = "on"   # together: one show, not two
    S.poll_once(ha.states())
    wait_for(1)
    time.sleep(0.1)
    assert P.calls == [("show", "camera", "front_door")] and P.seconds == 30
    for state in ("off", "on"):                              # again within the cooldown
        ha.s["binary_sensor.doorbell_motion_detected"] = state
        S.poll_once(ha.states())
    time.sleep(0.1)
    assert len(P.calls) == 1
    S._last_motion_show["front_door"] -= 61                  # the cooldown passes
    for state in ("off", "on"):
        ha.s["binary_sensor.doorbell_motion_detected"] = state
        S.poll_once(ha.states())
    wait_for(2)
    assert len(P.calls) == 2
    # The garage has no show_on_motion: logged, not shown.
    ha.s["binary_sensor.garage_motion_detected"] = "on"
    S.poll_once(ha.states())
    time.sleep(0.1)
    assert len(P.calls) == 2
    # Overnight (outside motion_hours): nothing goes up.
    S.cfg.motion_hours = "00:00-00:01" if time.localtime().tm_hour else "12:00-12:01"
    S._last_motion_show.clear()
    for state in ("off", "on"):
        ha.s["binary_sensor.doorbell_motion_detected"] = state
        S.poll_once(ha.states())
    time.sleep(0.1)
    assert len(P.calls) == 2


def test_snapshot_wakes_the_camera_once_and_keeps_the_schedule():
    ha = FakeHA()
    S = _security(ha)
    S.cfg.snapshots_dir = Path(tempfile.mkdtemp(prefix="antigua_snaps_"))
    cam = S.cfg.cameras["front_door"]
    cam.snapshot_minutes = 30

    class Resp:
        ok, content = True, b"\xff\xd8frame"

    asked = []
    real = security.requests.get
    security.requests.get = lambda url, params=None, timeout=None: asked.append(params) or Resp()
    real_sleep = security.time.sleep
    security.time.sleep = lambda s: None
    try:
        assert S.snapshot(cam)
    finally:
        security.requests.get = real
        security.time.sleep = real_sleep
    assert asked == [{"src": "SN1"}]
    assert ha.calls == [("eufy_security", "start_p2p_livestream", "camera.doorbell"),
                        ("eufy_security", "stop_p2p_livestream", "camera.doorbell")]
    meta = __import__("json").loads((S.cfg.snapshots_dir / "front_door.json").read_text())
    assert meta["battery"] == 17 and meta["every"] == 30 and meta["title"] == "Front door"
    assert (S.cfg.snapshots_dir / "front_door.jpg").read_bytes() == b"\xff\xd8frame"
    # After a restart the next one is due 30 minutes after this one, not now.
    S2 = _security(ha)
    S2.cfg.snapshots_dir = S.cfg.snapshots_dir
    assert S2._first_due(cam) > time.time() + 29 * 60


def _wav(path: Path) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 10)
    return str(path)


def make_backend(sec):
    tmp = Path(tempfile.mkdtemp(prefix="antigua_security_b_"))
    n = [0]

    def synthesize(text, lang="en"):
        n[0] += 1
        return _wav(tmp / f"tts_{n[0]}.wav")

    def ask_llm_stream(transcript, **kw):
        yield "(llm)"

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
        pineda=sec.pineda if sec else None,
        security=sec,
    )


def test_route():
    ha = FakeHA()
    S = _security(ha)
    pipeline.init(make_backend(S))

    def say(text):
        return pipeline.dispatch_text(text, conversation_id="s1", quiet=True)["response"]

    assert say("unlock the front door") == "I can't unlock doors. Use the keypad or the Eufy app."
    assert say("lock the front door").startswith("The front door is unlocked, but I'm not allowed")
    ha.s["lock.front_door"] = "unlocked"
    assert say("lock the front door") == "Locking the front door now."
    assert say("lock the door") == "The front door is already locked."
    S.poll_once(ha.states())
    assert say("is the front door locked") == "The front door is locked."
    yesterday = (datetime.now() - timedelta(days=1)).replace(hour=21, minute=5).timestamp()
    S.events.add("front_door_lock", "unlocked", ts=yesterday)
    assert say("when was the door last unlocked") == \
        "The front door was last unlocked yesterday at 9:05 PM."
    assert say("how's the doorbell battery") == \
        "The doorbell battery is at 17 percent, so it needs charging soon."
    assert say("garage camera battery") == "The garage camera battery is at 99 percent."
    assert say("who triggered the garage cam") == "I haven't logged any activity at the garage yet."
    ha.s["binary_sensor.garage_person_detected"] = "on"
    S.poll_once(ha.states())
    assert say("who triggered the garage cam").startswith("The garage camera last saw a person today at")
    assert say("show me the garage") == "Here's the garage."
    assert S.pineda.calls[-1] == ("show", "camera", "garage")
    S.pineda.clear_takeover()

    pipeline.init(make_backend(None))
    assert say("lock the front door") == "I can't see the cameras or the lock from here."


def _wait(announced, n):
    for _ in range(20):
        if len(announced) >= n:
            break
        time.sleep(0.05)


def test_motion_announcements():
    ha = FakeHA()
    cfg = _config()
    cfg.cameras["front_door"].announce_motion = True
    cfg.motion_cooldown_seconds = 60.0
    cfg.motion_hours = "00:00-24:00"  # all day for test
    announced = []
    S = _security(ha, cfg=cfg, announce=announced.append)

    S.poll_once(ha.states())
    assert announced == []

    # Plain motion (wind, a passing car) is logged but doesn't speak
    ha.s["binary_sensor.doorbell_motion_detected"] = "on"
    S.poll_once(ha.states())
    time.sleep(0.1)
    assert announced == []
    assert S.events.last("front_door", ("motion",))

    # A person does
    ha.s["binary_sensor.doorbell_person_detected"] = "on"
    S.poll_once(ha.states())
    _wait(announced, 1)
    assert announced == ["Somebody is at the front door."]

    # Another person while the cooldown runs is debounced
    ha.s["binary_sensor.doorbell_person_detected"] = "off"
    S.poll_once(ha.states())
    ha.s["binary_sensor.doorbell_person_detected"] = "on"
    S.poll_once(ha.states())
    time.sleep(0.1)
    assert len(announced) == 1

    # After the cooldown, a new person announces again
    S._last_motion_announce["front_door"] -= 65
    ha.s["binary_sensor.doorbell_person_detected"] = "off"
    S.poll_once(ha.states())
    ha.s["binary_sensor.doorbell_person_detected"] = "on"
    S.poll_once(ha.states())
    _wait(announced, 2)
    assert len(announced) == 2

    # Garage camera does not announce because announce_motion is False
    ha.s["binary_sensor.garage_person_detected"] = "on"
    S.poll_once(ha.states())
    time.sleep(0.1)
    assert len(announced) == 2


def test_motion_announces_when_opted_in():
    ha = FakeHA()
    cfg = _config()
    cfg.cameras["front_door"].announce_motion = True
    cfg.cameras["front_door"].announce_on = ["person", "motion"]
    cfg.motion_hours = "00:00-24:00"
    announced = []
    S = _security(ha, cfg=cfg, announce=announced.append)
    S.poll_once(ha.states())
    ha.s["binary_sensor.doorbell_motion_detected"] = "on"
    S.poll_once(ha.states())
    _wait(announced, 1)
    assert announced == ["Somebody is at the front door."]


def test_motion_announcement_quiet_hours():
    ha = FakeHA()
    cfg = _config()
    cfg.cameras["front_door"].announce_motion = True
    cfg.motion_hours = "00:00-00:00"  # empty window = quiet
    announced = []
    S = _security(ha, cfg=cfg, announce=announced.append)
    S.poll_once(ha.states())
    ha.s["binary_sensor.doorbell_person_detected"] = "on"
    S.poll_once(ha.states())
    time.sleep(0.1)
    assert announced == []


def test_custom_motion_announcement_phrase():
    ha = FakeHA()
    cfg = _config()
    cfg.cameras["front_door"].announce_motion = True
    cfg.cameras["front_door"].motion_announcement = "Look out, someone is at the porch."
    cfg.motion_hours = "00:00-24:00"
    announced = []
    S = _security(ha, cfg=cfg, announce=announced.append)
    S.poll_once(ha.states())
    ha.s["binary_sensor.doorbell_person_detected"] = "on"
    S.poll_once(ha.states())
    for _ in range(20):
        if announced:
            break
        time.sleep(0.05)
    assert announced == ["Look out, someone is at the porch."]


def test_format_visitor_announcements():
    fmt = security.format_visitor_announcement

    # 0 people / unknown
    assert fmt("Front door", []) == "Somebody is at the front door."
    assert fmt("Front door", None) == "Somebody is at the front door."
    assert fmt("Front door", ["unknown"]) == "Somebody is at the front door."
    assert fmt("Front door", [], fallback_announcement="Custom alert") == "Custom alert"

    # 1 person
    assert fmt("Front door", ["Sam"]) == "Sam's at the front door."
    assert fmt("Front door", ["Jordan"]) == "Jordan's at the front door."
    assert fmt("Front door", ["James"]) == "James is at the front door."
    assert fmt("Front door", ["sam"]) == "Sam's at the front door."  # Auto capitalization

    # 1 person with guest / unknown
    assert fmt("Front door", ["Sam"], has_unknown=True) == "Sam is at the front door with a guest."
    assert fmt("Front door", ["Sam", "unknown"]) == "Sam is at the front door with a guest."

    # 2 people
    assert fmt("Front door", ["Maria", "Vivicca"]) == "Maria is at the front door with Vivicca."
    assert fmt("Front door", ["maria", "vivicca"]) == "Maria is at the front door with Vivicca."
    assert fmt("Front door", ["Maria", "Vivicca"], has_unknown=True) == "Maria and Vivicca are at the front door with a guest."

    # 3+ people
    assert fmt("Front door", ["Sam", "Jordan", "Maria"]) == "Sam, Jordan, and Maria are at the front door."
    assert fmt("Front door", ["Sam", "Jordan", "Maria"], has_unknown=True) == "Sam, Jordan, and Maria are at the front door with a guest."

    # Other camera titles
    assert fmt("Garage", ["Sam"]) == "Sam's at the garage."
    assert fmt("Garage", []) == "Somebody is at the garage."
    assert fmt("The Porch", ["Jordan"]) == "Jordan's at the porch."


def test_extract_people_from_state():
    extract = security.extract_people_from_state

    # None / empty
    assert extract(None) == ([], False)
    assert extract({}) == ([], False)

    # HA sensor state
    assert extract({"state": "Sam", "attributes": {}}) == (["Sam"], False)
    assert extract({"state": "unknown", "attributes": {}}) == ([], False)
    assert extract({"state": "on", "attributes": {}}) == ([], False)

    # Attributes: Eufy BionicMind person_name
    assert extract({"state": "on", "attributes": {"person_name": "Sam"}}) == (["Sam"], False)
    assert extract({"state": "on", "attributes": {"person_name": "Maria, Vivicca"}}) == (["Maria", "Vivicca"], False)
    assert extract({"state": "on", "attributes": {"person_name": "unknown"}}) == ([], True)

    # Attributes: Frigate sub_label
    assert extract({"state": "on", "attributes": {"sub_label": "Sam"}}) == (["Sam"], False)
    assert extract({"state": "on", "attributes": {"sub_labels": [{"label": "Jordan"}]}}) == (["Jordan"], False)

    # Attributes: Double Take matched_faces
    assert extract({"state": "on", "attributes": {"matched_faces": ["Sam", "Jordan"]}}) == (["Sam", "Jordan"], False)


def test_visitor_announcements_with_people():
    ha = FakeHA()
    cfg = _config()
    cfg.cameras["front_door"].announce_motion = True
    cfg.motion_hours = "00:00-24:00"
    cfg.motion_cooldown_seconds = 10.0
    announced = []
    S = _security(ha, cfg=cfg, announce=announced.append)

    # Direct announce_visitor with 1 person
    phrase = S.announce_visitor("front_door", ["Sam"])
    assert phrase == "Sam's at the front door."
    time.sleep(0.05)
    assert announced[-1] == "Sam's at the front door."
    last_event = S.events.last("front_door", ("person",))
    assert last_event["detail"] == "Sam"

    # Reset cooldown
    S._last_motion_announce["front_door"] -= 15

    # Direct announce_visitor with 2 people
    phrase = S.announce_visitor("front_door", ["Maria", "Vivicca"])
    assert phrase == "Maria is at the front door with Vivicca."
    time.sleep(0.05)
    assert announced[-1] == "Maria is at the front door with Vivicca."
    last_event = S.events.last("front_door", ("person",))
    assert last_event["detail"] == "Maria, Vivicca"

    # A camera whose video goes up on motion says so
    S._last_motion_announce["front_door"] -= 15
    cfg.cameras["front_door"].show_on_motion = 30
    assert S.announce_visitor("front_door", []) == "Somebody is at the front door, live on Pineda Display."
    cfg.cameras["front_door"].show_on_motion = 0

    # Reset cooldown and test poller reading person_name attribute
    S._last_motion_announce["front_door"] -= 15
    ha.s["binary_sensor.doorbell_person_detected"] = "off"
    S.poll_once(ha.states())
    ha.s["binary_sensor.doorbell_person_detected"] = "on"
    states = ha.states()
    states["binary_sensor.doorbell_person_detected"]["attributes"] = {"person_name": "Jordan"}
    S.poll_once(states)
    time.sleep(0.05)
    assert announced[-1] == "Jordan's at the front door."


def test_a_non_admin_user_restarts_the_bridge_with_a_button():
    """HA refuses hassio services to non-admin users: with eufy_restart_button
    set, a finished charge presses that input_button (an HA automation does
    the restart) and never calls hassio."""
    ha = FakeHA()
    cfg = _config()
    cfg.eufy_addon, cfg.eufy_restart_button = None, "input_button.restart_eufy"
    S = _security(ha, cfg=cfg)
    S.poll_once(ha.states())
    ha.s["sensor.doorbell_charging_status"] = "Charging"
    S.poll_once(ha.states())
    ha.s["sensor.doorbell_charging_status"] = "Not Charging"
    S.poll_once(ha.states())
    time.sleep(0.2)
    assert ("input_button", "press", "input_button.restart_eufy") in ha.calls, ha.calls
    assert _restarts(ha) == []


def test_watch_uses_state_changed_events():
    """watch() subscribes to state_changed (allowed for non-admin users;
    subscribe_trigger isn't) and passes on only the watched entities."""
    import json
    import threading
    stop = threading.Event()
    sent, got = [], []
    events = [
        {"type": "auth_required"}, {"type": "auth_ok"}, {"id": 1, "type": "result", "success": True},
        {"id": 1, "type": "event", "event": {"event_type": "state_changed", "data": {
            "entity_id": "light.kitchen", "new_state": {"state": "on"}}}},
        {"id": 1, "type": "event", "event": {"event_type": "state_changed", "data": {
            "entity_id": "lock.front_door", "new_state": {"entity_id": "lock.front_door",
                                                          "state": "locked"}}}},
        {"id": 1, "type": "event", "event": {"event_type": "state_changed", "data": {
            "entity_id": "lock.front_door", "new_state": None}}},
    ]

    class FakeWS:
        def recv(self):
            if events:
                return json.dumps(events.pop(0))
            stop.set()
            raise security.websocket.WebSocketTimeoutException()

        def send(self, m):
            sent.append(json.loads(m))

        def settimeout(self, t):
            pass

        def ping(self):
            pass

        def close(self):
            pass

    real = security.websocket.create_connection
    security.websocket.create_connection = lambda url, timeout=None: FakeWS()
    try:
        ha = security.HomeAssistant("http://ha.local:8123", "token")
        ha.watch(["lock.front_door"], lambda d: got.append(d), stop)
    finally:
        security.websocket.create_connection = real
    assert sent[1] == {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}, sent
    assert got == [{"lock.front_door": {"entity_id": "lock.front_door", "state": "locked"}}], got


def main():
    settings.configure({})
    test_parser()
    test_event_log_and_poller()
    test_motion_and_person_dont_take_over()
    test_motion_announcements()
    test_motion_announces_when_opted_in()
    test_motion_announcement_quiet_hours()
    test_custom_motion_announcement_phrase()
    test_format_visitor_announcements()
    test_extract_people_from_state()
    test_visitor_announcements_with_people()
    test_a_finished_charge_restarts_the_eufy_bridge()
    test_a_non_admin_user_restarts_the_bridge_with_a_button()
    test_watch_uses_state_changed_events()
    test_lock_credits_the_voice_and_never_unlocks()
    test_the_display_runs_the_livestream()
    test_motion_at_the_front_door_puts_it_up()
    test_a_pushed_change_acts_once()
    test_snapshot_wakes_the_camera_once_and_keeps_the_schedule()
    test_route()
    print("PASS — security suite")


if __name__ == "__main__":
    main()
