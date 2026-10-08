#!/usr/bin/env python3
"""Podcast skill (Apple News Today) — parsing, page scraping, which episode,
and the weekend "want Friday's?" offer through the pipeline.

No network: the show page is a tiny synthetic copy of Apple Podcasts'
embedded JSON, and podcast.episodes / today are stubbed for the pipeline.

Run: python3 tests/test_podcast.py   (also works under pytest)
"""

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import pipeline, podcast, settings  # noqa: E402
from antigua_core.podcast import PodcastRequest, choose, parse_podcast  # noqa: E402

AUDIO = "https://news-assets.apple.com/podcast/audio"
ANT_ID = podcast.DEFAULT_SHOWS["apple_news_today"]["apple_id"]


def _ep(file, title, released, show=ANT_ID):
    return {"showAdamId": show, "title": title, "releaseDate": released,
            "mediaEnclosures": [{"streamUrl": f"{AUDIO}/{file}", "duration": 900}]}


PAGE = '<html><script type="application/json" id="serialized-server-data">' + json.dumps(
    {"shelves": [{"items": [
        _ep("a/ANT-20260928v3.mp3", "How a farmer stumbled upon terror suspects", "2026-09-28T10:00:00Z"),
        _ep("b/ANT-20260925v2.mp3", "The data-center backlash", "2026-09-25T10:30:00Z"),
        _ep("b0/ANT-20260925v1.mp3", "The data-center backlash", "2026-09-25T10:00:00Z"),  # older cut
        _ep("c/IC-20260924-AnnieLowrey-v6.mp3", "An interview", "2026-09-26T11:00:00Z"),   # In Conversation
        _ep("d/ANT-20230913-Trailer-v4.mp3", "Apple News Today", "2020-07-15T18:22:00Z"),  # trailer
        _ep("e/ANT-20260924v2.mp3", "Iran at the U.N.", "2026-09-24T10:00:00Z"),
        _ep("f/ANT-20260923v2.mp3", "Trump's U.N. speech.", "2026-09-23T10:00:00Z"),
        _ep("g/OTHER-20260928.mp3", "Some other show", "2026-09-28T10:00:00Z", show="999"),
    ]}]}) + "</script></html>"


def _item(title, pub, secs, n):
    return (f"<item><title>{title}</title><pubDate>{pub}</pubDate>"
            f"<itunes:duration>{secs}</itunes:duration>"
            f'<enclosure url="https://npr.example/{n}.mp3" type="audio/mpeg" length="1"/></item>')


FEED = ('<?xml version="1.0"?><rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        "<channel><title>Up First from NPR</title>"
        + _item("Trump Rejects Iran's Ceasefire", "Mon, 28 Sep 2026 10:01:00 GMT", 781, "mon")
        + _item("Can clergy candidates help Democrats?", "Sun, 27 Sep 2026 07:00:00 GMT", 1953, "sunstory")
        + _item("Powerful storms hit the US", "Sat, 26 Sep 2026 13:47:00 GMT", 839, "sat")
        + _item("Trump-Xi Summit", "Fri, 25 Sep 2026 10:04:00 GMT", 752, "fri")
        + _item("Tucker Carlson on his MAGA split", "Thu, 24 Sep 2026 17:00:00 GMT", 3467, "bonus")
        + _item("Court Temporarily Blocks Media Ban", "Thu, 24 Sep 2026 10:11:00 GMT", "13:02", "thu")
        + "</channel></rss>")

MON, FRI, SAT, SUN = date(2026, 9, 28), date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27)
ANT, UF = "apple_news_today", "up_first"


def main():
    settings.configure({"music": {"speakers": {"soundbar": "Soundbar"}},
                        "weather": {"home_tz": "America/Chicago"}})
    shows = podcast.shows()
    ant, uf = shows[ANT], shows[UF]

    # ── parsing ──
    R = PodcastRequest
    assert parse_podcast("Play Apple News today.") == R(ANT, None, None)
    assert parse_podcast("play apple news") == R(ANT, None, None)
    # Leading fillers / wake word the STT keeps ("So, play Apple News." on 10-05
    # fell through to music: "I couldn't find Apple News on Apple Music").
    assert parse_podcast("So, play Apple News.") == R(ANT, None, None)
    assert parse_podcast("Okay, Antigua, play Up First") == R(UF, None, None)
    assert parse_podcast("let's listen to apple news") == R(ANT, None, None)
    assert parse_podcast("Can you play the Apple News Today podcast?") == R(ANT, None, None)
    assert parse_podcast("play yesterday's Apple News Today") == R(ANT, "yesterday", None)
    assert parse_podcast("play Friday’s apple news today") == R(ANT, "friday", None)
    assert parse_podcast("put on the latest Apple News Today") == R(ANT, "latest", None)
    assert parse_podcast("play Apple News Today from Thursday") == R(ANT, "thursday", None)
    assert parse_podcast("play Apple News Today on the soundbar") == R(ANT, None, "soundbar")
    assert parse_podcast("Play Up First.") == R(UF, None, None)
    assert parse_podcast("play NPR’s Up First") == R(UF, None, None)
    assert parse_podcast("play NPR Up First podcast") == R(UF, None, None)
    assert parse_podcast("play Up First from NPR") == R(UF, None, None)
    assert parse_podcast("play today's Up First") == R(UF, None, None)
    assert parse_podcast("play Saturday's Up First on the soundbar") == R(UF, "saturday", "soundbar")
    for miss in ("play the news", "what's on apple news", "play Apple Music", "play Beyoncé",
                 "play NPR", "what's up first", ""):
        assert parse_podcast(miss) is None, miss

    assert podcast.answer("Yes please.") is True
    assert podcast.answer("sure") is True and podcast.answer("play it") is True
    assert podcast.answer("No thanks") is False and podcast.answer("never mind") is False
    assert podcast.answer("what time is it") is None

    # ── sources ──
    eps = podcast.daily(podcast.parse_apple_page(PAGE, ant), ant)
    assert [e.day.isoformat() for e in eps] == ["2026-09-28", "2026-09-25", "2026-09-24", "2026-09-23"], eps
    assert eps[1].url.endswith("b/ANT-20260925v2.mp3") and eps[1].show == "Apple News Today", eps[1]
    assert podcast.parse_apple_page("<html></html>", ant) == []

    ufe = podcast.daily(podcast.parse_feed(FEED, uf), uf)
    assert [(e.day.isoformat(), e.url[-7:]) for e in ufe] == [
        ("2026-09-28", "mon.mp3"), ("2026-09-26", "sat.mp3"), ("2026-09-25", "fri.mp3"),
        ("2026-09-24", "thu.mp3")], ufe                      # no Sunday Story, no bonus interview
    assert ufe[0].day == MON and ufe[0].show == "Up First"   # 10:01Z = 5:01am Chicago
    assert podcast._minutes("13:02") == 13 + 2 / 60 and podcast._minutes("") is None

    # ── which episode ──
    c = choose(ant, eps, R(), MON)                                # weekday, out
    assert c.episode.day == MON and not c.ask and c.reply == "", c
    c = choose(ant, eps[1:], R(), MON)                            # Monday 4am: Friday's, no question
    assert c.episode.day == FRI and not c.ask, c
    assert c.reply == "Today's Apple News Today isn't out yet, so here's Friday's.", c
    c = choose(ant, eps[2:], R(), FRI)                            # Friday 4am: yesterday's
    assert c.reply.endswith("so here's yesterday's."), c
    for weekend in (SAT, SUN):
        c = choose(ant, eps[1:], R(), weekend)
        assert c.episode.day == FRI and c.ask, c
        want = "Apple News Today doesn't come out on weekends. Want me to play "
        assert c.reply == want + ("yesterday's?" if weekend == SAT else "Friday's?"), c
    c = choose(ant, eps, R(ANT, "yesterday"), date(2026, 9, 29))
    assert c.episode.day == MON and not c.ask, c
    c = choose(ant, eps, R(ANT, "friday"), MON)
    assert c.episode.day == FRI and not c.ask, c
    c = choose(ant, eps, R(ANT, "latest"), SUN)
    assert c.episode.day == MON and not c.ask, c
    c = choose(ant, eps, R(ANT, "yesterday"), MON)                # Sunday: none that day
    assert c.episode.day == FRI and c.ask, c
    holiday = [e for e in eps if e.day != date(2026, 9, 24)]      # no Thursday episode
    c = choose(ant, holiday, R(ANT, "thursday"), MON)
    assert c.episode.day == date(2026, 9, 23) and c.ask, c
    assert c.reply == "There's no Apple News Today for Thursday. Want me to play Wednesday's?", c
    c = choose(ant, eps, R(ANT, "monday"), date(2026, 9, 21))     # older than the list
    assert c.episode is None and "couldn't find" in c.reply, c

    # Up First: Saturday is a news day, Sunday isn't
    c = choose(uf, ufe[1:], R(UF), SAT)
    assert c.episode.day == SAT and not c.ask, c
    c = choose(uf, ufe[2:], R(UF), SAT)                           # Saturday 7am, not out yet
    assert c.episode.day == FRI and not c.ask, c
    assert c.reply == "Today's Up First isn't out yet, so here's yesterday's.", c
    c = choose(uf, ufe[1:], R(UF), SUN)
    assert c.episode.day == SAT and c.ask, c
    assert c.reply == "Up First doesn't have a news episode on Sundays. Want me to play yesterday's?", c
    c = choose(uf, ufe[1:], R(UF), MON)                           # Monday 4am: Saturday's
    assert c.episode.day == SAT and not c.ask and c.reply.endswith("here's Saturday's."), c

    ep = eps[0]
    assert podcast.playing_reply(ep, MON) == \
        "Here's today's Apple News Today. How a farmer stumbled upon terror suspects."
    assert podcast.playing_reply(eps[3], date(2026, 9, 24), " on the soundbar") == \
        "Here's yesterday's Apple News Today on the soundbar. Trump's U.N. speech."
    assert podcast.playing_reply(eps[1], MON, " on the soundbar", "Today's isn't out yet, so here's Friday's.") \
        == "Today's isn't out yet, so here's Friday's on the soundbar. The data-center backlash."
    assert podcast.playing_reply(ufe[0], MON) == "Here's today's Up First. Trump Rejects Iran's Ceasefire."

    # ── pipeline: route, play, weekend offer ──
    tmp = Path(tempfile.mkdtemp(prefix="antigua_test_"))

    class FakeMusic:
        def __init__(self):
            self.played = []

        def play_url(self, url, speaker_key=None):
            self.played.append((url, speaker_key))
            return " on the soundbar" if speaker_key else ""

        def playing(self):
            return False

    def synth(text, lang="en"):
        (tmp / "o.wav").write_bytes(b"RIFF")
        return str(tmp / "o.wav")

    heard = {"text": ""}
    be = pipeline.Backend(
        transcribe=lambda p: {"text": heard["text"], "time_s": 0.0, "confidence": 1.0},
        synthesize=synth, ask_llm_stream=lambda *a, **k: iter(["Okay."]),
        audio_url_base=lambda: "http://test:0", memory_store=None, list_store=None,
        timers=None, weather_cache=None, news_cache=None, searxng=None)
    be.music = FakeMusic()
    pipeline.init(be)
    podcast.episodes = lambda show, fresh=False: {ANT: eps[1:], UF: ufe[1:]}[show.key]

    def turn(text, conv="c1"):
        heard["text"] = text
        return pipeline.run_pipeline("/x.wav", conversation_id=conv)

    podcast.today = lambda: SAT
    r = turn("Play Apple News today")
    assert r["response"].endswith("Want me to play yesterday's?"), r
    assert be.music.played == []
    r = turn("yes")
    assert r["response"] == "Here's yesterday's Apple News Today. The data-center backlash.", r
    assert be.music.played == [(eps[1].url, None)], be.music.played
    assert "c1" not in pipeline._pending_podcast

    turn("play apple news today on the soundbar")
    r = turn("no thanks")
    assert r["response"] == "Okay." and len(be.music.played) == 1, r

    turn("play apple news today")
    r = turn("yes", conv="other")                                 # another conversation
    assert len(be.music.played) == 1, be.music.played

    podcast.today = lambda: MON                                   # 4am Monday
    r = turn("play apple news today")
    assert r["response"].startswith("Today's Apple News Today isn't out yet, so here's Friday's."), r
    assert be.music.played[-1] == (eps[1].url, None)

    podcast.today = lambda: SUN
    r = turn("play Up First")
    assert r["response"].endswith("Want me to play yesterday's?"), r
    r = turn("okay")
    assert r["response"] == "Here's yesterday's Up First. Powerful storms hit the US.", r
    assert be.music.played[-1] == (ufe[1].url, None)

    def down(show, fresh=False):
        raise OSError("feed down")
    podcast.episodes = down
    assert turn("play up first")["response"] == "I couldn't get the podcast right now."

    be.music = None
    assert turn("play apple news")["response"] == "I can't play podcasts right now."

    print("PASS — podcast suite")


if __name__ == "__main__":
    main()
