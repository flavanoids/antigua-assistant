#!/usr/bin/env python3
"""netstatus (the internet prober's state machine) and sysinfo (the spoken
answers and their routing). No network: the prober is faked.

    python3 tests/test_sysinfo.py
"""

import sys
import tempfile
import time
from pathlib import Path

import _isolated  # noqa: F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from antigua_core import netstatus, sysinfo  # noqa: E402
from antigua_core.intents.sysinfo import parse_sysinfo_request  # noqa: E402


def _up(web_s=0.2):
    return {"ok": True, "cause": None, "web_s": web_s, "at": time.time()}


def _down(cause):
    return {"ok": False, "cause": cause, "web_s": None, "at": time.time()}


def _monitor(rounds):
    it = iter(rounds)
    path = Path(tempfile.mkdtemp()) / "net_outages.json"
    return netstatus.NetStatus(path=path, probe_fn=lambda: next(it)), path


def test_one_dropped_round_is_not_an_outage():
    m, _ = _monitor([_up(), _down("isp"), _up()])
    seen = []
    m.listeners.append(lambda *a: seen.append(a[0]))
    for _ in range(3):
        m.check()
    assert m.state == "online" and seen == [] and m.outages == []


def test_an_outage_and_its_return():
    m, path = _monitor([_up(), _down("dns"), _down("dns"), _down("isp"), _up()])
    seen = []
    m.listeners.append(lambda state, cause, outage: seen.append((state, cause)))
    m.check()
    m.check()
    assert m.online
    m.check()
    assert m.state == "offline" and m.cause == "dns" and not m.online
    assert seen == [("offline", "dns")]
    m.check()                                  # the cause moves on: the provider's out
    assert m.cause == "isp" and len(seen) == 1
    netstatus.monitor = m
    assert "down since" in sysinfo.net_answer("status")
    assert "internet provider" in sysinfo.net_answer("why")
    m.check()
    assert m.state == "online" and seen[-1] == ("online", None)
    assert m.outages[-1]["end"] and m.outages[-1]["cause"] == "isp"
    assert "It was out for" in sysinfo.net_answer("status")
    assert "The last outage was from" in sysinfo.net_answer("outages")
    # A restart mid-outage remembers it's offline
    m2, _ = _monitor([])
    m2.path = path
    m2.outages = m2._load()
    assert m2.outages[-1]["end"]


def test_slow_is_degraded():
    m, _ = _monitor([_up(web_s=5)])
    m.check()
    netstatus.monitor = m
    assert m.state == "degraded" and m.online
    assert "slow" in sysinfo.net_answer("status")


def test_system_answers_read_this_box():
    for kind in ("summary", "load", "memory", "disk", "temp", "gpu", "uptime"):
        reply = sysinfo.system_answer(kind)
        assert reply and reply[0].isupper() and reply.endswith("."), (kind, reply)


def test_parse():
    cases = {
        "is the internet down": "net_status",
        "do you have an internet connection right now": "net_status",
        "are we online": "net_status",
        "is the wifi working": "net_status",
        "why is the internet down": "net_why",
        "what's wrong with the internet": "net_why",
        "when did the internet go out": "net_outages",
        "how long was the internet down": "net_outages",
        "how's the server doing": "summary",
        "system status": "summary",
        "is everything running": "services",
        "what's the cpu load": "load",
        "how much memory is used": "memory",
        "how much disk space is left": "disk",
        "what's the gpu temperature": "temp",
        "how busy is the gpu": "gpu",
        "how long has the server been up": "uptime",
    }
    for text, kind in cases.items():
        assert parse_sysinfo_request(text) == kind, (text, parse_sysinfo_request(text))
    for text in ("how are you doing", "what's the temperature inside", "is everything okay",
                 "what's the weather", "remember that the wifi password is on the fridge",
                 "how do I reset my router"):
        assert parse_sysinfo_request(text) is None, text


def test_offline_turns_answer_at_once():
    from antigua_core import pipeline
    from test_security import make_backend
    m, _ = _monitor([_down("isp"), _down("isp")])
    m.check()
    m.check()
    netstatus.monitor = m
    pipeline.init(make_backend(None))

    def say(text):
        return pipeline.dispatch_text(text, conversation_id="n1", quiet=True)["response"]

    t = time.monotonic()
    assert say("what's the news").startswith("I can't get the news right now. "
                                              "The internet's been down since")
    assert say("how's traffic to work").startswith("I can't check traffic right now.")
    assert time.monotonic() - t < 2
    assert say("is the internet down").startswith("No, the internet's been down since")
    # Local skills don't care
    assert "?" not in say("what time is it") and "internet" not in say("what time is it")
    netstatus.monitor = None


def main():
    test_one_dropped_round_is_not_an_outage()
    test_an_outage_and_its_return()
    test_slow_is_degraded()
    test_system_answers_read_this_box()
    test_parse()
    test_offline_turns_answer_at_once()
    print("PASS — sysinfo suite")


if __name__ == "__main__":
    main()
