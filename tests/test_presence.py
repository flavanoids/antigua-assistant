#!/usr/bin/env python3
"""Who's home, arrival reminders, announcements and the Ask Antigua phone
port: presence.py, announce.py, phone_http.py and their pipeline routes.
No network: the ARP scanner and Music Assistant are fakes; the phone port is
real HTTP on a throwaway localhost port.

Run: python3 tests/test_presence.py   (also works under pytest)
"""

import sys
import tempfile
import time
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import requests  # noqa: E402

from antigua_core import announce, phone_http, pipeline, presence, settings  # noqa: E402
from antigua_core.music import MusicError  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_pipeline import make_backend  # noqa: E402

ALEX_PHONE, KAT_PHONE = "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"
CFG = {
    "household": [{"name": "Alex", "devices": [ALEX_PHONE.upper()]},
                  {"name": "Katherine", "devices": [KAT_PHONE, "not-a-mac"]},
                  {"name": "Maria"}],
    "presence": {"scan_seconds": 30, "away_after_minutes": 10, "full_scan_seconds": 120},
    "announce": {"rooms": {"bedroom": "Bedroom (2)", "upstairs": "Bedroom (2)",
                           "kitchen": "kitchen", "garage": "Garage"},
                 "default": ["bedroom"], "everywhere": ["kitchen", "bedroom"]},
}
settings.configure(CFG)
settings.SEARCH_ENABLED = False


class FakeLan:
    """arp_scan stand-in: which MACs answer, at which addresses."""

    def __init__(self):
        self.up = {}          # mac -> ip
        self.calls = []

    def __call__(self, interface, targets):
        self.calls.append(targets)
        if targets is None:
            return dict(self.up)
        return {m: ip for m, ip in self.up.items() if ip in targets}


def test_presence_tracking():
    lan, arrived = FakeLan(), []
    p = presence.Presence(CFG, scan=lan, on_arrive=arrived.append)
    assert p.enabled and p.people == ["Alex", "Katherine"] and not p.tracks("Maria")
    lan.up[ALEX_PHONE] = "192.168.1.20"
    t0 = 1_000_000.0
    p.tick(t0)
    assert lan.calls == [None], lan.calls          # no addresses yet: full scan
    assert p.is_home("Alex") is True
    assert p.is_home("Katherine") is None           # not seen, still in the grace period
    assert arrived == []                           # first sight isn't an arrival
    p.tick(t0 + 30)
    assert lan.calls[-1] == ["192.168.1.20"]      # known address only (full scan waits)
    # Katherine's phone never shows: away once the grace runs out.
    p.tick(t0 + 601)
    assert p.is_home("Katherine") is False
    # Alex's phone misses scans for a while: still home inside away_after...
    del lan.up[ALEX_PHONE]
    p.tick(t0 + 900)
    assert p.is_home("Alex") is True
    # ...gone after it.
    p.tick(t0 + 601 + 601)
    assert p.is_home("Alex") is False
    # Back, at a new address: the full scan finds it, and that's an arrival.
    lan.up[ALEX_PHONE] = "192.168.1.77"
    p.tick(t0 + 2000)
    assert p.is_home("Alex") is True and arrived == ["Alex"], arrived
    assert p.person_for_ip("192.168.1.77") == "Alex"
    assert p.person_for_ip("192.168.1.99", lookup=lambda ip: KAT_PHONE) == "Katherine"
    assert p.person_for_ip("8.8.8.8", lookup=lambda ip: KAT_PHONE) is None
    assert presence.Presence({"household": [{"name": "Alex"}]}).enabled is False


def test_replies():
    lan = FakeLan()
    p = presence.Presence(CFG, scan=lan)
    assert presence.query_reply("*", p) == "I'm still checking. Ask me again in a minute."
    lan.up[ALEX_PHONE] = "192.168.1.20"
    p.tick(1000.0)
    p.tick(1000.0 + 601)
    assert presence.query_reply("*", p) == "Alex is home. Katherine is out."
    assert presence.query_reply("Alex", p) == "Yes, Alex is home."
    assert presence.query_reply("Katherine", p) == "No, Katherine isn't home."
    assert presence.query_reply("Maria", p) == \
        "I don't know which phone is Maria's, so I can't tell if they're home."
    assert presence.query_reply("*", None).startswith("I can't tell who's home yet.")
    assert presence.set_reply("Katherine", "Alex", p) == "Okay, I'll remind Katherine when they get home."
    assert presence.set_reply("Alex", "Alex", p) == \
        "Okay. You are home now, so I'll remind you the next time you get home."
    assert presence.set_reply("Maria", None, p).endswith("I don't know which phone is Maria's.")
    rows = [{"message": "to take out the trash", "by": "Alex", "kind": "remind"},
            {"message": "dinner is in the fridge", "by": "Katherine", "kind": "tell"}]
    assert presence.arrival_text("Alex", rows) == (
        "Welcome home, Alex. You asked me to remind you to take out the trash. "
        "Katherine asked me to tell you dinner is in the fridge.")


def test_arrival_store():
    store = presence.ArrivalReminders(Path(tempfile.mkdtemp()) / "arrivals.json")
    store.add("Alex", "to call mom", by="Katherine")
    store.add("Katherine", "to water the plants")
    again = presence.ArrivalReminders(store.path)          # survives a restart
    assert [r["message"] for r in again.pending("Alex")] == ["to call mom"]
    assert [r["message"] for r in again.take("Alex")] == ["to call mom"]
    assert again.take("Alex") == [] and len(again.pending()) == 1
    old = again.add("Katherine", "stale")
    old["created"] = time.time() - 40 * 86400
    assert [r["message"] for r in again.take("Katherine")] == ["to water the plants"]


class FakeMA:
    def __init__(self, available=True):
        self.available = available
        self.played = []

    def cmd(self, command, timeout=None, **args):
        if command == "players/all":
            return [{"name": "Bedroom (2)", "player_id": "ap1", "available": self.available,
                     "type": "player"}]
        if command == "players/cmd/play_announcement":
            self.played.append(args)
            return None
        raise MusicError(command)


def _backend(ma=None, lan=None):
    b = make_backend("")
    lan = lan or FakeLan()
    b.presence = presence.Presence(CFG, scan=lan)
    b.arrivals = presence.ArrivalReminders(Path(tempfile.mkdtemp()) / "arrivals.json")
    a = announce.Announcer(CFG, ma)
    a.background = False
    announce.install(a)
    b.announcer = a
    b.audio_url_base = lambda: "http://10.77.0.1:9393"
    pipeline.init(b)
    return b


def say(text, conv="p1", **kw):
    kw.setdefault("quiet", True)
    return pipeline.dispatch_text(text, conversation_id=conv, follow_up=True, **kw)["response"]


def test_arrival_route():
    b = _backend()
    assert say("remind me to take out the trash when I get home").startswith("I don't know who's asking")
    assert say("remind me to take out the trash when I get home", speaker="Alex") == \
        "Okay, I'll remind you when you get home."
    assert say("tell Alex dinner is in the fridge when he gets home", speaker="Katherine") == \
        "Okay, I'll remind Alex when they get home."
    rows = b.arrivals.pending("Alex")
    assert [(r["message"], r["by"], r["kind"]) for r in rows] == [
        ("to take out the trash", "Alex", "remind"), ("dinner is in the fridge", "Katherine", "tell")]
    assert say("who's home") == "I'm still checking. Ask me again in a minute."


def test_announce_route():
    published = []
    real = settings.mqtt_publish
    settings.mqtt_publish = lambda topic, payload: published.append((topic, payload))
    try:
        ma = FakeMA()
        _backend(ma)
        # A QA dry run (quiet, not the phone) never plays anything.
        assert say("tell the bedroom dinner is ready") == "Okay, I announced it in the bedroom."
        assert ma.played == []
        # Said in the kitchen: the default room, chime on, Antigua's clip.
        assert say("announce that dinner is ready", quiet=False) == "Okay, I announced it in the bedroom."
        [a] = ma.played
        assert a["player_id"] == "ap1" and a["pre_announce"] is True and a["volume_level"] == 45
        assert a["url"].startswith("http://10.77.0.1:9393/audio/"), a
        # "Everywhere" from the kitchen leaves the kitchen out.
        assert say("tell everyone we're leaving", quiet=False) == "Okay, I announced it in the bedroom."
        # From a phone: everywhere, the kitchen through the satellite.
        assert say("announce dinner is ready", source="phone", speaker="Alex") == \
            "Okay, I announced it in the kitchen and the bedroom."
        assert any(t == "antigua/play" for t, _ in published)
        assert say("announce the movie is starting upstairs", quiet=False) == \
            "Okay, I announced it upstairs."
        assert say("announce in the garage that the car is here", quiet=False) == \
            "I couldn't reach the garage."
        _backend(FakeMA(available=False))
        assert say("tell the bedroom dinner is ready", quiet=False) == "I couldn't reach the bedroom."
    finally:
        settings.mqtt_publish = real
    assert announce.spoken_message("dinner is ready", "Alex") == "Alex says: Dinner is ready."
    assert announce.spoken_message("are you coming?", None) == "Are you coming?"


def test_phone_port():
    calls = []

    def dispatch(text, conv, person):
        calls.append((text, conv, person))
        return {"response": "" if text == "mumble" else f"answer to {text}", "expects_reply": text == "remind me"}

    from http.server import ThreadingHTTPServer
    from threading import Thread
    key = "k" * 24
    lan = FakeLan()
    p = presence.Presence(CFG, scan=lan)
    server = ThreadingHTTPServer(("127.0.0.1", 0), phone_http.make_handler(key, dispatch, p))
    Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert requests.get(base + "/health").json() == {"ok": True}
        assert requests.post(base + "/ask", json={"text": "hi"}).status_code == 401
        assert requests.post(base + "/ask", json={"text": "hi"},
                             headers={"X-Antigua-Key": "wrong"}).status_code == 401
        h = {"X-Antigua-Key": key}
        r = requests.post(base + "/ask", json={"text": "what's   the weather", "device": "Alex’s iPhone"},
                          headers=h).json()
        assert r["reply"] == "answer to what's the weather" and r["person"] == "Alex", r
        assert calls[-1] == ("what's the weather", "phone-alex", "Alex")
        r = requests.post(base + "/ask", json={"text": "remind me", "device": "iPad"}, headers=h).json()
        assert r["expects_reply"] is True and r["person"] is None
        assert calls[-1][1] == "phone-ipad"
        assert requests.post(base + "/ask", json={"text": "mumble"}, headers=h).json()["reply"] == \
            "Sorry, I didn't catch that."
        assert requests.post(base + "/ask", data="nope", headers=h).status_code == 400
        assert requests.post(base + "/other", json={}, headers=h).status_code == 404
    finally:
        server.shutdown()
    assert phone_http.allowed_client("192.168.1.20") and phone_http.allowed_client("10.77.0.2")
    assert phone_http.allowed_client("::ffff:192.168.1.20")
    assert not phone_http.allowed_client("8.8.8.8") and not phone_http.allowed_client("junk")
    assert phone_http.person_from_device("Katherine's iPad") == "Katherine"
    assert phone_http.person_from_device("Alex and Katherine's Mac") is None
    assert phone_http.read_key("/nonexistent") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("PASS — presence / announce / phone suite")
