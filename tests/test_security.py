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
            "binary_sensor.doorbell_person_detected": "off",
            "binary_sensor.garage_person_detected": "off",
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


class FakePineda:
    def __init__(self):
        self.card = None
        self.calls = []

    def show_takeover(self, card, seconds):
        self.calls.append(("show", card["kind"], card.get("camera")))
        self.card = {"card": card}

    def update_takeover(self, change):
        self.card["card"].update(change)

    def takeover(self):
        return self.card

    def takeover_up(self):
        return self.card is not None

    def clear_takeover(self):
        self.calls.append(("clear",))
        self.card = None


def _config():
    return security.Config.from_dict({
        "live_seconds": 30,
        "cameras": {
            "front_door": {"entity": "camera.doorbell", "title": "Front door", "stream": "SN1",
                           "battery": "sensor.doorbell_battery_percentage",
                           "person": "binary_sensor.doorbell_person_detected",
                           "ring": "binary_sensor.doorbell_ringing"},
            "garage": {"entity": "camera.garage", "title": "Garage", "stream": "SN2",
                       "battery": "sensor.garage_battery_percentage",
                       "person": "binary_sensor.garage_person_detected"},
        },
        "lock": {"entity": "lock.front_door", "status": "sensor.front_door_lock_status",
                 "battery": "sensor.front_door_battery_percentage"},
    })


def _security(ha=None, pineda=None):
    tmp = Path(tempfile.mkdtemp(prefix="antigua_security_"))
    return security.Security(ha or FakeHA(), _config(),
                             security.EventLog(tmp / "events.db"), pineda=pineda or FakePineda())


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


def test_livestream_stops_when_the_card_goes():
    ha, P = FakeHA(), FakePineda()
    S = _security(ha, P)
    S.show_camera("front_door", seconds=30)
    assert ha.calls[0] == ("eufy_security", "start_p2p_livestream", "camera.doorbell")
    assert P.calls[-1] == ("show", "camera", "front_door")
    P.clear_takeover()                       # "stop the display"
    for _ in range(60):
        if ("eufy_security", "stop_p2p_livestream", "camera.doorbell") in ha.calls:
            break
        time.sleep(0.1)
    assert ("eufy_security", "stop_p2p_livestream", "camera.doorbell") in ha.calls
    # Switching cameras stops the first stream at once.
    ha.calls.clear()
    S.show_camera("front_door", seconds=30)
    S.show_camera("garage", seconds=30)
    assert ("eufy_security", "stop_p2p_livestream", "camera.doorbell") in ha.calls
    P.clear_takeover()


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


def main():
    settings.configure({})
    test_parser()
    test_event_log_and_poller()
    test_motion_and_person_dont_take_over()
    test_lock_credits_the_voice_and_never_unlocks()
    test_livestream_stops_when_the_card_goes()
    test_snapshot_wakes_the_camera_once_and_keeps_the_schedule()
    test_route()
    print("PASS — security suite")


if __name__ == "__main__":
    main()
