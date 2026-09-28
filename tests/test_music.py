#!/usr/bin/env python3
"""Music skill against a fake Music Assistant — resolution, replies, controls.

No network: MusicAssistant.cmd is replaced by a fake that serves a tiny
catalogue and records every command. Lyrics search is stubbed at
requests.get.

Run: python3 tests/test_music.py   (also works under pytest)
"""

import sys
import tempfile
import time
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import music as music_mod, settings  # noqa: E402
from antigua_core.music import MusicControl  # noqa: E402
from antigua_core.music_intents import parse_music  # noqa: E402


def _a(name, uri, year, typ, artist):
    return {"name": name, "uri": uri, "year": year, "album_type": typ,
            "artists": [{"name": artist}], "item_id": uri, "provider": "apple_music"}


BEY = {"name": "Beyoncé", "uri": "apple_music://artist/bey", "item_id": "bey", "provider": "apple_music"}
TORO = {"name": "Toro y Moi", "uri": "apple_music://artist/toro", "item_id": "toro", "provider": "apple_music"}
BB = {"name": "Bad Bunny", "uri": "apple_music://artist/bb", "item_id": "bb", "provider": "apple_music"}
ALBUMS = {
    "bey": [_a("Lemonade", "am://al/lem", 2016, "album", "Beyoncé"),
            _a("Cowboy Carter", "am://al/cc", 2024, "album", "Beyoncé"),
            _a("Renaissance", "am://al/ren", 2022, "album", "Beyoncé"),
            _a("Some Single", "am://al/ss", 2025, "single", "Beyoncé"),
            _a("Dangerously in Love", "am://al/dil", 2003, "album", "Beyoncé")],
    "toro": [_a("Causers of This", "am://al/coT", 2010, "album", "Toro y Moi"),
             _a("Underneath the Pine", "am://al/utp", 2011, "album", "Toro y Moi"),
             _a("Anything in Return", "am://al/air", 2013, "album", "Toro y Moi"),
             _a("Outer Peace", "am://al/op", 2019, "album", "Toro y Moi"),
             _a("Sandhills", "am://al/sh", 2020, "ep", "Toro y Moi")],
    "bb": [_a("El Último Tour del Mundo", "am://al/eut", 2020, "album", "Bad Bunny"),
           _a("Yonaguni", "am://al/yon", 2021, "single", "Bad Bunny"),
           _a("Lo Siento BB:/", "am://al/lsbb", 2021, "single", "Bad Bunny"),
           _a("Un Verano Sin Ti", "am://al/uvst", 2022, "album", "Bad Bunny")],
}
TRACKS = [
    {"name": "Halo", "uri": "am://tr/halo", "artists": [{"name": "Beyoncé"}]},
    {"name": "Stand by Me", "uri": "am://tr/sbm", "artists": [{"name": "Ben E. King"}]},
    {"name": "Shape of You", "uri": "am://tr/soy", "artists": [{"name": "Ed Sheeran"}]},
]
PLAYLISTS = [
    {"name": "Beyoncé Essentials", "uri": "am://pl/bey-ess", "owner": "Apple Music"},
    {"name": "Jazz Essentials", "uri": "am://pl/jazz", "owner": "Apple Music"},
]
PLAYERS = {"Kitchen Speaker": "ap_pi", "Soundbar": "up_sb", "Bedroom (2)": "ap_bed", "Living Room": "ap_atv"}


class FakeMA:
    def __init__(self):
        self.calls = []
        self.queues = {}          # pid -> {"state", "current_item"}

    def cmd(self, command, timeout=None, **args):
        self.calls.append((command, args))
        if command == "players/all":
            return [{"player_id": pid, "name": n, "type": "player", "available": True}
                    for n, pid in PLAYERS.items()] + [
                    {"player_id": "proto_pi", "name": "Kitchen Speaker", "type": "protocol", "available": True}]
        if command == "music/search":
            q = args["search_query"].lower()
            out = {}
            words = [w for w in q.replace("'", "").split() if len(w) > 2]

            def hit(name):
                n = music_mod._norm(name)
                return any(w in n for w in words) or music_mod._norm(q) in n
            types = args["media_types"]
            if "artist" in types:
                out["artists"] = [a for a in (BEY, TORO, BB) if hit(a["name"])]
            if "album" in types:
                out["albums"] = [a for al in ALBUMS.values() for a in al if hit(a["name"])]
            if "track" in types:
                out["tracks"] = [t for t in TRACKS if hit(t["name"])]
            if "playlist" in types:
                out["playlists"] = [p for p in PLAYLISTS if hit(p["name"])]
            return out
        if command == "music/artists/artist_albums":
            return ALBUMS[args["item_id"]]
        if command == "player_queues/play_media":
            media = args["media"]
            first = media[0] if isinstance(media, list) else media
            self.queues[args["queue_id"]] = {"state": "playing", "current_item": {
                "name": "Halo", "media_item": {"name": "Halo", "artists": [{"name": "Beyoncé"}],
                                               "album": {"name": "I Am... Sasha Fierce"}},
                "uri": first}}
            return None
        if command == "player_queues/transfer":
            self.queues[args["target_queue_id"]] = self.queues.pop(args["source_queue_id"])
            return None
        if command == "players/get":
            return {"player_id": args["player_id"], "volume_level": 40}
        if command == "player_queues/get":
            return self.queues.get(args["queue_id"], {"state": "idle", "current_item": None})
        return None

    def last(self, command):
        return next(a for c, a in reversed(self.calls) if c == command)


def make():
    mc = MusicControl({"music": {"labels": {"Soundbar": "the soundbar",
                                            "Bedroom (2)": "the bedroom HomePod"}}}, "tok")
    mc.ma = FakeMA()
    mc._played_path = Path(tempfile.mkdtemp()) / "played.json"
    mc._vocab_at = time.time()          # no background library/chart fetch
    mc._popular = lambda heard: set()   # no Deezer calls
    mc.play_in_background = False
    return mc


def say(mc, text):
    it = parse_music(text)
    assert it is not None, text
    return mc.handle(it)


SPEAKERS = {
    "kitchen": "Kitchen Speaker", "kitchen speaker": "Kitchen Speaker",
    "living room": "Living Room", "living room tv": "Living Room",
    "apple tv": "Living Room", "tv": "Living Room",
    "soundbar": "Soundbar", "sound bar": "Soundbar", "jbl": "Soundbar",
    "bedroom": "Bedroom (2)", "homepod": "Bedroom (2)",
}


def main():
    settings.configure({"music": {"speakers": SPEAKERS, "default_player": "Kitchen Speaker"}})
    settings.SEARCH_URL = "http://searx.test"

    # Artist → Apple's Essentials playlist on the default (kitchen) speaker
    mc = make()
    assert say(mc, "play Beyonce") == "Playing Beyoncé Essentials.", mc.ma.calls
    pm = mc.ma.last("player_queues/play_media")
    assert pm["queue_id"] == "ap_pi" and pm["media"] == "am://pl/bey-ess", pm

    # Newest album skips the 2025 single
    assert say(mc, "play Beyonce's newest album") == "Playing Cowboy Carter by Beyoncé."
    assert mc.ma.last("player_queues/play_media")["media"] == "am://al/cc"
    assert say(mc, "play Beyonce's first album") == "Playing Dangerously in Love by Beyoncé."

    # Album by name, whole album, no radio
    r = say(mc, "play the album Lemonade")
    assert r == "Playing Lemonade by Beyoncé.", r
    pm = mc.ma.last("player_queues/play_media")
    assert pm["media"] == "am://al/lem" and pm["radio_mode"] is False, pm

    # A song continues with radio, like Alexa
    assert say(mc, "play Halo by Beyonce") == "Playing Halo by Beyoncé."
    assert mc.ma.last("player_queues/play_media")["radio_mode"] is True
    # "Stand by Me" survives being parsed as "Stand" by "Me"
    assert say(mc, "play Stand by Me") == "Playing Stand by Me by Ben E. King."

    # Speaker choice
    assert say(mc, "play Halo on the soundbar") == "Playing Halo by Beyoncé on the soundbar."
    assert mc.ma.last("player_queues/play_media")["queue_id"] == "up_sb"

    # Genre → playlist, shuffled
    assert say(mc, "play some jazz") == "Playing Jazz Essentials."

    # Year filter: both 2021 singles, oldest first
    mc = make()
    r = say(mc, "play Bad Bunny songs from 2021")
    assert r == "Playing Bad Bunny's music from 2021.", r
    assert mc.ma.last("player_queues/play_media")["media"] == ["am://al/yon", "am://al/lsbb"]
    assert say(mc, "play Beyonce songs from the 90s") == "I couldn't find anything Beyoncé released in the 90s."

    # Discography question → list → follow-up by name and by ordinal
    mc = make()
    r = say(mc, "What is the name of older albums by Toro y Moi?")
    assert r == ("Toro y Moi's earliest albums are Causers of This, from 2010, Underneath the Pine, "
                 "from 2011, and Anything in Return, from 2013. Want me to play one?"), r
    assert say(mc, "Play Underneath the Pine") == "Playing Underneath the Pine by Toro y Moi."
    assert say(mc, "play the third one") == "Playing Anything in Return by Toro y Moi."
    assert say(mc, "play it").startswith("Which one?")
    r = say(mc, "what's Beyonce's latest album")
    assert r == "Beyoncé's newest album is Cowboy Carter, from 2024. Want me to play it?", r
    assert say(mc, "play it") == "Playing Cowboy Carter by Beyoncé."
    r = say(mc, "what albums does Toro y Moi have")
    assert r.startswith("Toro y Moi has 4 albums. The most recent are Outer Peace, from 2019"), r

    # Lyrics → web search titles → catalogue
    class _Resp:
        def json(self):
            return {"results": [{"title": "Ed Sheeran – Shape of You Lyrics | Genius Lyrics"},
                                {"title": "Ed Sheeran - Shape of You Lyrics | AZLyrics.com"}]}
    orig_get = music_mod.requests.get
    music_mod.requests.get = lambda *a, **k: _Resp()
    try:
        r = say(mc, "play the song that goes I'm in love with the shape of you")
        assert r == "Playing Shape of You by Ed Sheeran.", r
    finally:
        music_mod.requests.get = orig_get

    # Controls act on the player Antigua last used; transport ones are silent
    mc = make()
    assert say(mc, "next song") == "Nothing's playing right now."
    say(mc, "play Halo on the soundbar")
    assert say(mc, "next song") == ""
    assert mc.ma.calls[-1] == ("player_queues/next", {"queue_id": "up_sb"})
    assert say(mc, "restart the song") == ""
    assert mc.ma.calls[-1] == ("player_queues/seek", {"queue_id": "up_sb", "position": 0})
    assert say(mc, "replay the album") == ""
    assert mc.ma.calls[-1] == ("player_queues/play_index", {"queue_id": "up_sb", "index": 0})
    assert say(mc, "repeat this song") == "Repeating this song."
    assert mc.ma.calls[-1] == ("player_queues/repeat", {"queue_id": "up_sb", "repeat_mode": "one"})
    assert say(mc, "turn off shuffle") == "Shuffle is off."
    assert say(mc, "what song is this") == "This is Halo by Beyoncé, from I Am... Sasha Fierce."
    assert say(mc, "set the music volume to 30") == "Music volume 30."
    assert mc.ma.calls[-1] == ("players/cmd/volume_set", {"player_id": "up_sb", "volume_level": 30})
    assert say(mc, "move the music to the bedroom") == "Moving the music to the bedroom HomePod."
    assert mc.ma.calls[-1][0] == "player_queues/transfer"
    assert say(mc, "pause") == ""
    assert mc.ma.calls[-1] == ("player_queues/pause", {"queue_id": "ap_bed"})

    # Ducking: kitchen music drops to 25% while the user talks and comes
    # back when they stop; a volume the user sets mid-turn is kept.
    def listening(mc, active):                 # on_listening without the thread
        mc._listening = active
        mc._apply_listening()
    mc = make()
    say(mc, "play Halo")                       # default player = kitchen
    listening(mc, True)
    assert mc.ma.calls[-1] == ("players/cmd/volume_set", {"player_id": "ap_pi", "volume_level": 10})
    listening(mc, True)                        # a repeat start doesn't duck twice
    assert mc.ma.calls[-1] == ("players/cmd/volume_set", {"player_id": "ap_pi", "volume_level": 10})
    listening(mc, False)
    assert mc.ma.calls[-1] == ("players/cmd/volume_set", {"player_id": "ap_pi", "volume_level": 40})
    # "Stopped" landing before the queued "started" worker runs: the worker
    # applies the latest state, so nothing is left ducked.
    n = len(mc.ma.calls)
    mc._listening = False
    mc._apply_listening()
    assert not [c for c in mc.ma.calls[n:] if c[0] == "players/cmd/volume_set"], mc.ma.calls[n:]
    mc._duck()
    say(mc, "set the music volume to 60")
    n = len(mc.ma.calls)
    mc._unduck()
    assert len(mc.ma.calls) == n, mc.ma.calls[n:]   # nothing to restore
    # Music elsewhere isn't ducked
    mc = make()
    say(mc, "play Halo on the soundbar")
    n = len(mc.ma.calls)
    mc._duck()
    assert not [c for c in mc.ma.calls[n:] if c[0] == "players/cmd/volume_set"]

    # default_volume: set when starting from idle, left alone mid-playback
    mc = make()
    mc.default_volume = 65
    say(mc, "play Halo")
    assert ("players/cmd/volume_set", {"player_id": "ap_pi", "volume_level": 65}) in mc.ma.calls
    n = len(mc.ma.calls)
    say(mc, "play Halo")
    assert not [c for c in mc.ma.calls[n:] if c[0] == "players/cmd/volume_set"], mc.ma.calls[n:]

    # Re-hearing: a name we don't know is re-transcribed with sound-alike
    # known names as hints; taken only if it now names something known.
    mc = make()
    mc._vocab = ["Toro y Moi", "Beyoncé"]
    heard = []
    rehear = lambda hints: heard.append(hints) or "play Toro y Moi"
    assert mc.handle(parse_music("play Toro Iguana"), rehear=rehear) == "Playing Toro y Moi.", mc.ma.calls
    assert "Toro y Moi" in heard[0][:3], heard
    heard.clear()
    say(mc, "play Beyonce")                               # known: no second STT pass
    assert mc.handle(parse_music("play Beyonce"), rehear=rehear) and not heard
    assert "Beyoncé" in mc._played or "Beyoncé Essentials" in mc._played
    # A popular name from Deezer that isn't in the library: offered as a hint,
    # taken once Whisper hears it; Deezer's guess alone isn't trusted.
    mc._popular = lambda heard: {"Chappell Roan"}
    mc._rehear = lambda hints: heard.append(hints) or "play Chappell Roan"
    assert mc._reheard(parse_music("play Chapel Room")).query == "Chappell Roan"
    assert heard[-1][0] == "Chappell Roan", heard[-1]
    mc._rehear = lambda hints: "play Tauro Imoa"          # Whisper disagrees
    mc._popular = lambda heard: {"Taio Cruz"}
    assert mc._reheard(parse_music("play Tauro Imoa")).query == "Tauro Imoa"
    mc._rehear = None
    mc._popular = lambda heard: set()
    rehear = lambda hints: "play Chapel Room"             # same again: keep it
    mc.handle(parse_music("play Chapel Room"), rehear=rehear)
    assert mc.ma.last("player_queues/play_media")

    # Nothing found
    assert say(mc, "play Zzqxv Blorp") == "I couldn't find Zzqxv Blorp on Apple Music."

    print("PASS — music suite")


if __name__ == "__main__":
    main()
