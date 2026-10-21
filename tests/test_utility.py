#!/usr/bin/env python3
"""The 2026-10-15 everyday skills, end to end through dispatch_text with a
stubbed Backend: stopwatch, spelling, coin and dice, help, repeat, an
honest "can't do that", reminders with no time, the thermostat, store
hours, markets and air quality. No network: the data sources are fakes.

Run: python3 tests/test_utility.py   (also works under pytest)
"""

import random
import sys
import time
from datetime import date, datetime
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import climate, hours, live, pipeline, settings  # noqa: E402
from antigua_core.commute import Place  # noqa: E402
from antigua_core.intents import utility  # noqa: E402
from antigua_core.intents.live import parse_hours_request, parse_market_request  # noqa: E402
from antigua_core.tts_text import clean_for_tts  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_pipeline import make_backend  # noqa: E402

settings.configure({"climate": {"url": "http://ac.test"}})
settings.SEARCH_ENABLED = False


def say(text, conv="c1", **kw):
    return pipeline.dispatch_text(text, conversation_id=conv, quiet=True, **kw)["response"]


def setup():
    pipeline.init(make_backend(""))


def test_stopwatch():
    setup()
    pipeline._stopwatch.update(started=None, elapsed=0.0)
    assert say("how long has the stopwatch been running") == "The stopwatch isn't running."
    assert say("start a stopwatch") == "Stopwatch started."
    pipeline._stopwatch["started"] = time.time() - 75
    assert say("what's the stopwatch at") == "1 minute and 15 seconds."
    assert say("stop the stopwatch") == "Stopped at 1 minute and 15 seconds."
    assert say("how long has the stopwatch been running") == "1 minute and 15 seconds, stopped."
    assert say("start the stopwatch") == "Stopwatch running again."
    assert say("reset the stopwatch") == "Stopwatch reset. It's still running."
    pipeline._stopwatch.update(started=None, elapsed=0.0)


def test_spell_coin_dice_help():
    setup()
    assert say("how do you spell necessary") == \
        "Necessary is spelled en, ee, see, ee, ess, ess, ay, ar, why."
    random.seed(1)
    assert say("flip a coin") in ("It's heads.", "It's tails.")
    r = say("roll two dice")
    assert r.startswith("You rolled ") and ", for " in r, r
    assert say("pick a number between 1 and 1") == "1."
    assert utility.parse_random("roll a 20 sided die") == ("dice", 1, 20)
    assert "timers" in say("what can you do")
    assert "timers" in say("help")


def test_repeat():
    setup()
    pipeline._last_said.update(text="", at=0.0)
    assert say("repeat that") == "I haven't said anything lately."
    pipeline.dispatch_text("how do you spell cat", conversation_id="c2")   # not quiet: remembered
    assert say("say that again") == "Cat is spelled see, ay, tee."


def test_volume_levels():
    assert utility.parse_volume_level("set the volume to 3") == 30
    assert utility.parse_volume_level("volume to 40 percent") == 40
    assert utility.parse_volume_level("turn the volume up to six") == 60
    assert utility.parse_volume_level("set the volume to 300") is None
    assert utility.parse_mute("mute") == "mute" and utility.parse_mute("unmute it") == "unmute"
    assert utility.parse_mute("mute the tv") is None       # the TV's own route


def test_cant_do_that():
    setup()
    pipeline.B.check_addressed = None          # no wake check in tests
    assert say("text Sam I'm on my way") == "I can't send messages or make calls yet."
    assert say("what's on my calendar today") == "I can't see your calendar yet."
    assert say("set the thermostat to 70").startswith("I can't change the thermostat.")
    assert say("open the garage door") == "I can't open or close the garage door."
    assert say("book a table for two") == "I can't do that yet."


def test_reminder_asks_when():
    setup()
    pipeline.B.timers.cancel_all()
    assert say("remind me to buy milk", conv="r1") == "When should I remind you?"
    r = say("in 20 minutes", conv="r1")
    assert "buy milk" in r and "20" in r, r
    [row] = pipeline.B.timers.list_active()
    assert row["kind"] == "reminder" and 1150 < row["fires_at"] - time.time() <= 1200, row
    pipeline.B.timers.cancel_all()

    # Getting home is presence's (arrival reminders); other places still ask.
    r = say("remind me to get gas when I leave work", conv="r2")
    assert r.startswith("I can only remind you at a time"), r
    say("at 7 tonight", conv="r2")
    [row] = pipeline.B.timers.list_active()
    assert row["message"] == "get gas", row
    pipeline.B.timers.cancel_all()

    assert say("remind me to call the dentist", conv="r3") == "When should I remind you?"
    assert say("never mind", conv="r3") == "Okay, no reminder."
    assert pipeline.B.timers.list_active() == []


def test_climate():
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    data = {"latest": {"time": now, "indoor_temp": 72.0, "current_setpoint": 71.0,
                       "humidity_pct": 63.0, "runtime_pct_30m": 100.0}}
    assert climate.answer("temp", data) == "It's seventy-two degrees inside, and the thermostat is set to seventy-one."
    assert climate.answer("running", data).startswith("Yes, it's been running the whole last half hour")
    data["latest"]["runtime_pct_30m"] = 40
    assert climate.answer("running", data).startswith("On and off. It ran about forty percent")
    assert climate.answer("humidity", data) == "It's sixty-three percent humidity inside."
    data["latest"]["time"] = "2026-01-01T15:01"
    assert climate.answer("setpoint", data).endswith(", as of 3:01 PM.")
    assert climate.answer("temp", {}) == "I can't reach the thermostat right now."


def test_hours():
    week = hours.parse_opening_hours("Mo-Sa 06:00-22:00; Su 08:00-20:00; PH closed")
    thu = datetime(2026, 10, 15, 9, 0)
    assert hours.answer("close", "the Lowe's on Ella", week, now=thu) == \
        "The Lowe's on Ella closes at 10 PM tonight."
    assert hours.answer("open_now", "Target", week, now=datetime(2026, 10, 15, 23, 0)) == \
        "No, Target is closed right now. It opens at 6 AM tomorrow."
    assert hours.answer("open", "Target", week, day=date(2026, 10, 18), now=thu) == \
        "Target opens at 8 AM on Sunday."
    assert hours.parse_opening_hours("sunrise-sunset") is None
    assert hours.parse_opening_hours("24/7")[3] == [(0, 1440)]
    r = parse_hours_request("what time does Costco open on Sunday", today=date(2026, 10, 15))
    assert (r.kind, r.place, r.day) == ("open", "Costco", date(2026, 10, 18)), r
    assert parse_hours_request("wake me up in 8 hours") is None
    assert parse_hours_request("is it open") is None

    # Through the pipeline, with a fake resolver and OSM.
    class _Resolver:
        def resolve(self, dest):
            return "found", Place("Costco", 0, 0, label="Costco", osm="way/1")

    class _Commute:
        resolver = _Resolver()

    setup()
    pipeline.B.commute = _Commute()
    real = hours.opening_hours
    hours.opening_hours = lambda p: "24/7"
    try:
        assert say("is Costco open") == "Costco is open twenty-four hours."
    finally:
        hours.opening_hours = real


def test_markets_and_air():
    assert parse_market_request("how's the stock market doing").key == "^GSPC"
    assert parse_market_request("what's the price of bitcoin").kind == "crypto"
    assert parse_market_request("how's Apple stock doing").key == "apple"
    assert parse_market_request("how are the Astros doing") is None
    real = live._get
    fake = {
        "coingecko": {"bitcoin": {"usd": 82318.4, "usd_24h_change": 0.64}},
        "search": {"quotes": [{"symbol": "AAPL", "shortname": "Apple Inc.", "quoteType": "EQUITY"}]},
        "chart": {"chart": {"result": [{"meta": {"regularMarketPrice": 342.1, "chartPreviousClose": 343.0,
                                                 "regularMarketTime": time.time()}}]}},
        "air-quality": {"current": {"us_aqi": 54}, "hourly": {"us_aqi": [40, 54, 120]}},
        "pollen": {"Location": {"periods": [
            {"Type": "Today", "Index": 8.4, "Triggers": [{"Name": "Ragweed"}, {"Name": "Grasses"}]},
            {"Type": "Tomorrow", "Index": 4.0}]}},
    }
    live._get = lambda url, ttl, **kw: next(v for k, v in fake.items() if k in url)
    settings.DEFAULT_WEATHER_LOCATION = "77001"
    try:
        assert live.market_answer("crypto", "bitcoin") == \
            "Bitcoin is at 82,318 dollars, up 0.6 percent in the last day."
        assert live.market_answer("stock", "apple") == \
            "Apple stock is at 342 dollars and 10 cents, down 0.3 percent."
        assert live.air_answer() == ("The air quality is moderate, with an index of 54. "
                                     "It's forecast to reach unhealthy for sensitive groups later today.")
        assert live.pollen_answer() == "Pollen is medium to high today, mostly ragweed and grass. Tomorrow looks low to medium."
        for s in (live.market_answer("crypto", "bitcoin"), live.air_answer()):
            assert clean_for_tts(s)
    finally:
        live._get = real


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
