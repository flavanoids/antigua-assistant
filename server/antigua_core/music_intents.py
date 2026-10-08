"""Music intent parsing — pure, network-free, shared by classify() and music.py.

parse_music(transcript) → MusicIntent | None. Three kinds:

  control  — playback verbs on whatever is playing: pause, resume, next,
             previous, restart_track, restart_queue, repeat_one/all/off,
             shuffle_on/off, now_playing, volume_up/down/set, transfer;
             navigation: seek (± seconds) / seek_to, skip_songs (± count),
             play_track_number, up_next, time_left, more_like_this,
             more_by_artist, whole_album, rest_of_album, sleep (seconds) /
             sleep_after_song / sleep_after_queue / sleep_cancel, like, dislike;
             multi-room: group_all, group_add / group_remove / group_only
             (speaker). A play "everywhere" has speaker "*".
  info     — discography questions answered by voice ("what are the older
             albums by Toro y Moi"); the answer is remembered so a following
             "play the second one" / "play Underneath the Pine" can use it
  play     — everything that starts music; `action` says how to resolve it:
               artist   "play Beyoncé", "play songs by Bad Bunny"
               album    "play the album Lemonade", "play Lemonade album by …"
               track    "play the song Halo", "play Halo by Beyoncé"
               newest / oldest  "play Beyoncé's newest album"
               year     "play Bad Bunny songs from 2021" / "… from the 90s"
               lyrics   "play the song that goes …"
               pick     "play the second one", "play it" (from the last list)
               any      "play <something>" / "play X by Y": the resolver weighs
                        artists, songs, albums and playlists (music.py)
               resume   "play music" / "play something" with nothing named
               favorites / library   "play my favorites" / "play my library"
               my_playlist   "play my Cocktail Hour playlist"
             enqueue: "next" ("play X next", "after this play X") or "add"
             ("add X to the queue", "queue up X"); "" replaces the queue

Checked in that order (control → info → play) so "play it again" restarts
the song instead of searching for a song called "it again".

parse_correction(transcript) → Correction | None: "no, the Adele one", "the
other one", "the original", "no, I meant the album". Only meaningful right
after Antigua started something by name; the pipeline checks that
(MusicControl.correctable()) before routing it here.
"""

import re
from dataclasses import dataclass

from . import settings


@dataclass
class MusicIntent:
    kind: str                    # "play" | "control" | "info"
    action: str
    query: str = ""
    artist: str = ""
    year_from: int | None = None
    year_to: int | None = None
    ordinal: int | None = None   # 1-based; -1 = last; None = "it"/"that one"
    speaker: str | None = None   # spoken speaker key from settings.MUSIC_SPEAKERS
    level: int | None = None     # volume_set
    shuffle: bool = False
    raw: str = ""                # everything after the play verb, for fallbacks
    genre: bool = False          # a style or mood ("some jazz", "chill music"): prefer playlists
    enqueue: str = ""            # "" replace | "next" | "add"
    seconds: float | None = None # seek / seek_to / sleep
    count: int | None = None     # skip_songs (negative = back) / play_track_number


_POLITE = r"^(?:(?:hey|ok|okay|so|and)\s+)?(?:(?:alexa|antigua),?\s+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|go\s+ahead\s+and\s+|let'?s\s+)?"
_TRAIL = re.compile(r"(?:,?\s+(?:please|for\s+me|now|thanks|thank\s+you))+$")
_ON_APPLE = re.compile(r"\s+(?:on|from)\s+apple\s+music$")

_NEWEST_WORDS = r"newest|latest|new|most\s+recent|last|recent"
_OLDEST_WORDS = r"oldest|first|debut|earliest"
_OLDER_WORDS = r"older|oldest|old|earlier|earliest|early|first"
_NEWER_WORDS = r"newer|newest|new|latest|recent|most\s+recent|last"

# ── controls ─────────────────────────────────────────────────────────────────

_CONTROLS = [
    ("now_playing", r"\b(?:what(?:'s|\s+is)\s+(?:playing|this\s+song|the\s+song\s+playing|this\s+track)|"
                    r"what\s+(?:song|track)\s+is\s+(?:this|that|playing|on)|"
                    r"who\s+(?:sings|is\s+singing|sang)\s+(?:this|that)(?:\s+song)?|"
                    r"what\s+(?:album|artist)\s+is\s+this|name\s+(?:of\s+)?this\s+song)\b"),
    ("restart_queue", r"\b(?:(?:replay|restart)\s+(?:the\s+|this\s+)?(?:album|playlist)|"
                      r"start\s+(?:the\s+|this\s+)?(?:album|playlist)\s+(?:over|again)|"
                      r"play\s+(?:the\s+|this\s+)?(?:album|playlist)\s+(?:again|over|from\s+the\s+(?:beginning|start|top)))\b"),
    ("restart_track", r"\b(?:(?:replay|restart)\s+(?:the\s+|this\s+)?(?:song|track)|"
                      r"start\s+(?:the\s+|this\s+)?(?:song|track)\s+(?:over|again)|"
                      r"play\s+(?:the\s+|this\s+)?(?:song|track)\s+(?:again|from\s+the\s+(?:beginning|start|top))|"
                      r"from\s+the\s+top|start\s+it\s+over|play\s+(?:it|that)\s+again|replay\s+(?:it|that)|"
                      r"(?:go|skip|jump|start)\s+back\s+to\s+the\s+(?:beginning|start)(?:\s+of\s+(?:the|this)\s+(?:song|track))?)\b"),
    ("repeat_off", r"\b(?:(?:turn|switch)\s+(?:off\s+)?repeat(?:\s+off)?|repeat\s+off|stop\s+repeating|"
                   r"no\s+(?:more\s+)?repeat|disable\s+repeat|don'?t\s+repeat)\b"),
    ("repeat_one", r"\b(?:(?:repeat|loop)\s+(?:the\s+|this\s+)?(?:song|track)|"
                   r"(?:put\s+)?(?:the\s+|this\s+)?(?:song|track)\s+on\s+(?:repeat|loop)|"
                   r"repeat\s+(?:this|it)$)"),
    ("repeat_all", r"\b(?:(?:repeat|loop)\s+(?:the\s+|this\s+)?(?:album|playlist|queue|all|everything)|"
                   r"(?:put\s+)?(?:the\s+|this\s+)?(?:album|playlist)\s+on\s+(?:repeat|loop)|"
                   r"(?:turn|switch)\s+on\s+repeat|repeat\s+on)\b"),
    ("shuffle_off", r"\b(?:(?:turn|switch)\s+off\s+shuffle|shuffle\s+off|stop\s+shuffling|unshuffle|"
                    r"(?:turn|switch)\s+shuffle\s+off|no\s+(?:more\s+)?shuffle|disable\s+shuffle)\b"),
    ("shuffle_on", r"^shuffle$|\b(?:(?:turn|switch)\s+on\s+shuffle|shuffle\s+on|"
                   r"shuffle\s+(?:the\s+|this\s+)?(?:album|playlist|music|songs|queue|it))\b"),
    ("next", r"^(?:next|skip|play\s+next)$|\b(?:next\s+(?:song|track|one)|skip\s+(?:this\s+|the\s+)?(?:song|track|one)|"
             r"skip\s+(?:this|it)|play\s+the\s+next\s+(?:song|track|one))\b"),
    ("previous", r"\b(?:(?:previous|prev)\s+(?:song|track|one)|go\s+back\s+(?:a|one)\s+(?:song|track)|"
                 r"play\s+the\s+(?:previous|last)\s+(?:song|track|one)|back\s+(?:a|one)\s+(?:song|track))\b"),
    ("volume_up", r"\b(?:turn\s+(?:the\s+)?music\s+up|turn\s+up\s+the\s+music|"
                  r"(?:make\s+)?the\s+music\s+louder|music\s+(?:louder|up)|crank\s+(?:up\s+)?the\s+music)\b"),
    ("volume_down", r"\b(?:turn\s+(?:the\s+)?music\s+down|turn\s+down\s+the\s+music|"
                    r"(?:make\s+)?the\s+music\s+(?:quieter|softer)|music\s+(?:quieter|softer|down)|lower\s+the\s+music)\b"),
    ("pause", r"^(?:pause|stop)(?:\s+it)?$|\bpause\s+(?:the\s+|this\s+)?(?:music|song|track|it|{pod})\b|"
              r"\b(?:stop|turn\s+off)\s+(?:the\s+|this\s+)?(?:music|song|playing|playback|{pod})\b|"
              r"^pause\s+(?:please|for\s+a\s+(?:sec|second|minute))$"),
    ("resume", r"^(?:resume|unpause|continue)$|^play$|"
               r"\b(?:resume|unpause|continue)\s+(?:the\s+|this\s+)?(?:music|song|track|playing|playback|{pod})\b|"
               r"\bkeep\s+playing\b|^resume\s+(?:it|please)$|\bstart\s+the\s+music\s+again\b"),
]
# A podcast episode (podcast.py) plays through the same queue.
_POD = r"podcast|episode|apple\s+news(?:\s+today)?|up\s+first"
_CONTROLS = [(a, re.compile(p.replace("{pod}", _POD), re.IGNORECASE)) for a, p in _CONTROLS]

_VOLUME_SET = re.compile(
    r"\b(?:set\s+)?(?:the\s+)?music(?:'s)?\s+(?:volume\s+)?(?:to\s+|at\s+)?(\d{1,3})\s*(?:percent|%)?$"
    r"|\bmusic\s+volume\s+(?:to\s+)?(\d{1,3})\b", re.IGNORECASE)

# Multi-room, on whatever's playing. The speaker is checked in parse_music.
_EVERYWHERE = (r"(?:everywhere|in\s+every\s+room|in\s+all\s+(?:the\s+)?rooms|"
               r"on\s+(?:all|every)\s+(?:of\s+)?(?:the\s+)?speakers?|throughout\s+the\s+house|all\s+over\s+the\s+house|"
               r"in\s+the\s+whole\s+house)")
_THIS = r"(?:this|it|that|the\s+music|the\s+song|music)"
_GROUP_ALL = re.compile(rf"^(?:play|put|move|send|take|spread|have)\s+{_THIS}\s+(?:on\s+|play\s+)?{_EVERYWHERE}$",
                        re.IGNORECASE)
_GROUP_ADD = re.compile(
    rf"^(?:also\s+)?(?:play|put)\s+{_THIS}\s+(?:in|on)\s+(?:the\s+)?(?P<a>.+?)\s+(?:too|as\s+well)$"
    rf"|^also\s+(?:play\s+(?:{_THIS}\s+)?)?(?:in|on)\s+(?:the\s+)?(?P<b>.+)$"
    r"|^add\s+(?:the\s+)?(?P<c>.+?)(?:\s+to\s+the\s+(?:music|group|party))?$", re.IGNORECASE)
_GROUP_REMOVE = re.compile(
    r"^(?:remove|drop|take\s+out|take)\s+(?:the\s+)?(?P<a>.+?)(?:\s+(?:out|off)(?:\s+of\s+the\s+(?:music|group))?)?$"
    rf"|^(?:stop|pause)\s+(?:playing\s+)?(?:{_THIS}\s+)?(?:in|on)\s+(?:the\s+)?(?P<b>.+)$"
    r"|^(?:not|no\s+more|no\s+music)\s+(?:in|on)\s+(?:the\s+)?(?P<c>.+)$"
    r"|^(?:pause|stop|mute)\s+(?:the\s+)?(?P<d>.+?)(?:\s+speaker)?$", re.IGNORECASE)
_GROUP_ONLY = re.compile(r"^(?:play\s+(?:it\s+|this\s+|the\s+music\s+)?)?(?:only|just)\s+(?:in|on)\s+(?:the\s+)?(?P<a>.+)$"
                         r"|^(?:only|just)\s+(?:the\s+)?(?P<b>.+?)(?:\s+speaker)?$", re.IGNORECASE)

# "move the music to the soundbar", "play this in the bedroom"
_TRANSFER = re.compile(
    r"^(?:move|send|transfer|switch|put)\s+(?:the\s+)?(?:music|song|this|it|playback|that)\s+(?:over\s+)?"
    r"(?:to|onto|on|in|into)\s+(?:the\s+)?(?P<spk>.+)$"
    r"|^(?:play|continue)\s+(?:this|it|that|the\s+music)\s+(?:on|in)\s+(?:the\s+)?(?P<spk2>.+)$",
    re.IGNORECASE)

# ── navigation ──────────────────────────────────────────────────────────────

_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
              "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
              "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
              "couple": 2, "a couple": 2, "a couple of": 2, "few": 3, "a few": 3}


def _count(word: str) -> int | None:
    w = re.sub(r"(?<=\d)(?:st|nd|rd|th)$", "", word.lower().strip())
    return int(w) if w.isdigit() else _NUM_WORDS.get(w)


def _seconds(text: str) -> float | None:
    """ "30 seconds", "a minute", "half an hour", "1:30"."""
    from .intents.timers import resolve_duration
    m = re.fullmatch(r"(?:the\s+)?(\d{1,2}):(\d\d)(?:\s+mark)?", text.strip())
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return resolve_duration(re.sub(r"\bmark\b", "", text))


_SONGS = r"(?:songs?|tracks?)"
_COUNT = r"(?P<n>\d+|a\s+couple(?:\s+of)?|a\s+few|one|two|three|four|five|six|seven|eight|nine|ten)"
_SEEK_FWD = re.compile(r"^(?:(?:skip|jump|go|move|scrub)\s+(?:ahead|forward)|fast[\s-]?forward)(?:\s+(?:by\s+)?(?P<d>.+))?$",
                       re.IGNORECASE)
_SEEK_BACK = re.compile(r"^(?:(?:skip|jump|go|move|scrub)\s+back(?:wards?)?|rewind(?:\s+it)?)(?:\s+(?:by\s+)?(?P<d>.+))?$",
                        re.IGNORECASE)
_SEEK_TO = re.compile(r"^(?:go|jump|skip|seek|fast[\s-]?forward|rewind)\s+to\s+(?P<d>.+?)"
                      r"(?:\s+(?:in|into)\s+(?:the|this)\s+song)?$", re.IGNORECASE)
_SKIP_N = re.compile(rf"^(?:skip(?:\s+ahead|\s+forward)?|go\s+forward|jump\s+ahead)\s+{_COUNT}\s+{_SONGS}$", re.IGNORECASE)
_BACK_N = re.compile(rf"^(?:go|skip|jump)\s+back\s+{_COUNT}\s+{_SONGS}$", re.IGNORECASE)
_TRACK_N = re.compile(
    r"^(?:(?:play|skip\s+to|go\s+to|jump\s+to)\s+(?:track|song)\s+(?:number\s+)?(?P<n>\w+)"
    r"|(?:play|skip\s+to|go\s+to|jump\s+to)\s+the\s+(?P<o>\w+)\s+(?:song|track)"
    r"(?:\s+(?:on|of|in)\s+(?:the|this)\s+(?:album|playlist))?)$", re.IGNORECASE)
_NAV = [
    ("up_next", r"^what(?:'s|\s+is|\s+song\s+is|\s+comes|\s+song\s+comes|\s+plays)\s+(?:playing\s+)?"
                r"(?:next|up\s+next|after\s+(?:this|that)(?:\s+(?:one|song))?)$|^what'?s\s+coming\s+up(?:\s+next)?$|"
                r"^what'?s\s+(?:in|on)\s+the\s+queue$|^what\s+(?:song\s+)?(?:is\s+)?next$"),
    # "song" required: a bare "how much time is left" is the timers' question
    ("time_left", r"^how\s+(?:long\s+is\s+(?:this|the)\s+(?:song|track)|"
                  r"much\s+(?:time\s+|longer\s+)?(?:is\s+)?(?:left\s+)?(?:in|on|of)\s+(?:this|the)\s+(?:song|track)(?:\s+left)?|"
                  r"much\s+longer\s+is\s+(?:this|the)\s+(?:song|track))$"),
    ("more_like_this", r"^(?:play\s+|queue\s+up\s+|give\s+me\s+)?(?:(?:some\s+)?more\s+(?:songs\s+|music\s+|stuff\s+)?"
                       r"like\s+(?:this|that|it)(?:\s+(?:one|song))?|something\s+similar|similar\s+(?:songs|music|stuff))$"),
    ("more_by_artist", r"^(?:play\s+|queue\s+up\s+)?more\s+(?:songs\s+|music\s+|stuff\s+)?(?:by|from)\s+"
                       r"(?:this|that|the\s+same)\s+(?:artist|band|singer|group|rapper)$|^play\s+more\s+of\s+(?:this|that|them|him|her)$"),
    ("rest_of_album", r"^(?:play|finish)\s+the\s+rest\s+of\s+(?:the|this)\s+album$"),
    ("whole_album", r"^play\s+(?:the|this)\s+(?:whole|full|entire)\s+album$|^play\s+this\s+album$|"
                    r"^play\s+the\s+album\s+(?:this|that|the)\s+(?:song|track)\s+is\s+(?:from|on)$|"
                    r"^play\s+(?:the|this)\s+(?:song'?s|track'?s)\s+album$"),
    ("sleep_cancel", r"\b(?:cancel|stop|turn\s+off|clear|remove|delete)\s+(?:the\s+)?(?:music\s+)?sleep\s+timer\b|"
                     r"^don'?t\s+stop\s+the\s+music$"),
    ("like", r"^(?:i\s+(?:really\s+)?(?:like|love|dig)\s+(?:this|that)(?:\s+(?:song|track|one))?|"
             r"(?:like|love|favorite|favourite|heart|save)\s+(?:this|that)(?:\s+(?:song|track|one))?|"
             r"add\s+(?:this|that)(?:\s+(?:song|track))?\s+to\s+(?:my\s+)?(?:library|favorites|favourites|liked\s+songs|collection)|"
             r"thumbs\s+up)$"),
    ("dislike", r"^(?:i\s+(?:don'?t|do\s+not)\s+like\s+(?:this|that)(?:\s+(?:song|track|one))?|"
                r"i\s+(?:hate|dislike|can'?t\s+stand)\s+(?:this|that)(?:\s+(?:song|track|one))?|thumbs\s+down|"
                r"(?:never|don'?t)\s+play\s+(?:this|that)(?:\s+(?:song|track|one))?\s+again)$"),
]
_NAV = [(a, re.compile(p, re.IGNORECASE)) for a, p in _NAV]
_SLEEP = re.compile(
    r"^(?:(?:stop|pause|turn\s+off|shut\s+off|end|fade\s+out)\s+(?:the\s+)?(?:music|playing|playback|song|it)"
    r"|set\s+(?:a\s+|the\s+)?(?:music\s+)?sleep\s+timer|(?:music\s+)?sleep\s+timer)"
    r"\s+(?:in|for|after|at\s+the\s+end\s+of)\s+(?P<d>.+)$"
    r"|^stop\s+(?:playing\s+)?after\s+(?P<d2>(?:this|the)\s+(?:song|track|one|album|playlist))$", re.IGNORECASE)

# ── enqueueing: "play X next", "add X to the queue" ─────────────────────────

_ENQ_NEXT_SUFFIX = re.compile(r"\s+(?:next|up\s+next|after\s+(?:this|that)(?:\s+(?:song|one|track))?)$", re.IGNORECASE)
_ENQ_NEXT_PREFIX = re.compile(r"^(?:after\s+(?:this|that)(?:\s+(?:song|one|track))?|next)[,\s]+(?:play|put\s+on)\s+(?P<rest>.+)$",
                              re.IGNORECASE)
_ENQ_ADD = re.compile(r"^(?:add|put)\s+(?P<rest>.+?)\s+(?:to|on|in|into)\s+(?:the\s+|my\s+)?(?:queue|up\s+next)$"
                      r"|^queue(?:\s+up)?\s+(?P<rest2>.+)$", re.IGNORECASE)

# ── my library ──────────────────────────────────────────────────────────────

_FAVORITES = re.compile(r"^(?:my\s+)?(?:favorites|favourites|favorite\s+songs|favourite\s+songs|liked\s+songs|"
                        r"loved\s+songs|likes|hearted\s+songs)$", re.IGNORECASE)
_LIBRARY = re.compile(r"^(?:my\s+)(?:music|library|music\s+library|songs|collection)$|^(?:songs|music)\s+from\s+my\s+library$",
                      re.IGNORECASE)
_MY_PLAYLIST = re.compile(r"^my\s+(?P<q>.+?)\s+playlist$|^(?:my|the)\s+playlist\s+(?:called\s+)?(?P<q2>.+)$",
                          re.IGNORECASE)

# ── info (discography) ──────────────────────────────────────────────────────

_INFO = [
    # "what are the names of (some of) the older albums by Toro y Moi"
    re.compile(rf"^(?:what|which)(?:'s|\s+is|\s+are|\s+were|'re)?\s+(?:the\s+)?(?:names?\s+of\s+)?(?:some\s+(?:of\s+)?)?"
               rf"(?:the\s+|a\s+few\s+(?:of\s+)?)?(?:(?P<ord>{_OLDER_WORDS}|{_NEWER_WORDS})\s+)?"
               rf"(?P<plural>albums?|records?)\s+(?:by|from)\s+(?P<artist>.+)$", re.IGNORECASE),
    # "what is Beyoncé's newest album", "what are Toro y Moi's older albums"
    re.compile(rf"^(?:what|which)(?:'s|\s+is|\s+are|\s+were)?\s+(?:the\s+names?\s+of\s+)?(?:some\s+of\s+)?"
               rf"(?P<artist>.+?)(?:'s|s'|’s)\s+(?:(?P<ord>{_OLDER_WORDS}|{_NEWER_WORDS})\s+)?(?P<plural>albums?|records?)$",
               re.IGNORECASE),
    # "what albums does Toro y Moi have", "what albums has Beyoncé released"
    re.compile(r"^(?:what|which)\s+(?P<plural>albums)\s+(?:does|did|has|have)\s+(?P<artist>.+?)\s+"
               r"(?:have|made|make|released?|put\s+out|got)$", re.IGNORECASE),
    # "tell me / name / list (some of) Toro y Moi's older albums"
    re.compile(rf"^(?:tell\s+me|name|list|give\s+me)\s+(?:the\s+names\s+of\s+)?(?:some\s+(?:of\s+)?)?(?:the\s+)?"
               rf"(?:(?P<ord>{_OLDER_WORDS}|{_NEWER_WORDS})\s+)?(?P<plural>albums?|records?)\s+(?:by|from)\s+(?P<artist>.+)$",
               re.IGNORECASE),
    re.compile(rf"^(?:tell\s+me|name|list|give\s+me)\s+(?:some\s+of\s+)?(?P<artist>.+?)(?:'s|s'|’s)\s+"
               rf"(?:(?P<ord>{_OLDER_WORDS}|{_NEWER_WORDS})\s+)?(?P<plural>albums?|records?)$", re.IGNORECASE),
]

# ── play ────────────────────────────────────────────────────────────────────

_PLAY = re.compile(
    r"^(?P<verb>play|put\s+on|listen\s+to|shuffle|"
    r"i\s+(?:want|wanna)\s+(?:to\s+)?(?:hear|listen\s+to)|i'?d\s+like\s+to\s+(?:hear|listen\s+to)|let\s+me\s+hear)\s+(?P<rest>.+)$",
    re.IGNORECASE)

# What "play …" must not grab: other skills and non-music "play".
_NOT_MUSIC = re.compile(
    r"^(?:(?:the|some|today'?s|my|a|an)\s+)?(?:news|headlines|podcast|game|games|with\b|joke|"
    r"(?:the\s+)?(?:alarm|timer|reminder)s?)\b", re.IGNORECASE)

_LYRICS = re.compile(
    r"^(?:the\s+|that\s+)?song\s+(?:that\s+goes(?:\s+like)?|with\s+the\s+(?:lyrics?|words|line)|"
    r"that\s+says|where\s+(?:they|he|she|it)\s+(?:sings?|says))\s+(?P<lyrics>.+)$", re.IGNORECASE)

_ORDINALS = {"first": 1, "1st": 1, "one": 1, "1": 1, "second": 2, "2nd": 2, "two": 2, "2": 2,
             "third": 3, "3rd": 3, "three": 3, "3": 3, "fourth": 4, "4th": 4, "four": 4, "4": 4,
             "fifth": 5, "5th": 5, "five": 5, "5": 5, "last": -1}
_PICK = re.compile(
    r"^(?:the\s+)?(?:(?P<ord>first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th)"
    r"|number\s+(?P<num>one|two|three|four|five|[1-5]))(?:\s+(?:one|album|record))?$"
    r"|^(?P<it>it|that|that\s+one|this\s+one|that\s+album|the\s+album|this|them|those)$", re.IGNORECASE)

_NEWEST = re.compile(
    rf"^(?:(?P<a1>.+?)(?:'s|s'|’s)\s+(?P<o1>{_NEWEST_WORDS}|{_OLDEST_WORDS})\s+(?:album|record)"
    rf"|(?:the\s+)?(?P<o2>{_NEWEST_WORDS}|{_OLDEST_WORDS})\s+(?:album|record)\s+(?:by|from)\s+(?P<a2>.+))$",
    re.IGNORECASE)

_YEAR = r"(?P<year>(?:19|20)\d\d|(?:the\s+)?(?:19|20)?\d0'?s|(?:the\s+)?'\d0s)"
_YEAR_PLAY = [
    re.compile(rf"^(?:some\s+)?(?:songs|music|tracks|hits|stuff)\s+(?:by|from)\s+(?P<artist>.+?)\s+"
               rf"(?:from|in|released\s+in)\s+{_YEAR}$", re.IGNORECASE),
    re.compile(rf"^(?:some\s+)?(?P<artist>.+?)(?:'s|s'|’s)?\s+(?:songs|music|tracks|hits|stuff)\s+"
               rf"(?:from|in|released\s+in)\s+(?:the\s+year\s+)?{_YEAR}$", re.IGNORECASE),
    re.compile(rf"^(?:some\s+)?(?P<artist>.+?)\s+(?:from|in)\s+(?:the\s+year\s+)?{_YEAR}$", re.IGNORECASE),
]

_ALBUM = [
    re.compile(r"^(?:the\s+)?album\s+(?P<q>.+?)(?:\s+by\s+(?P<artist>.+))?$", re.IGNORECASE),
    re.compile(r"^(?:the\s+)?(?P<q>.+?)\s+album(?:\s+by\s+(?P<artist>.+))?$", re.IGNORECASE),
]
_TRACK = re.compile(r"^(?:the\s+)?(?:song|track|single)\s+(?P<q>.+?)(?:\s+by\s+(?P<artist>.+))?$", re.IGNORECASE)
_ARTIST = [
    re.compile(r"^(?:some\s+|more\s+|a\s+few\s+)?(?:songs|music|hits|tracks|stuff|something|anything)\s+"
               r"(?:by|from)\s+(?P<artist>.+)$", re.IGNORECASE),
    re.compile(r"^(?:the\s+)?(?:artist|band|group|singer|rapper)\s+(?P<artist>.+)$", re.IGNORECASE),
    re.compile(r"^more\s+(?:by|from)\s+(?P<artist>.+)$", re.IGNORECASE),
    re.compile(r"^(?:some\s+|more\s+)?(?P<artist>.+?)(?:'s|s'|’s)?\s+(?:greatest\s+hits|top\s+songs|best\s+songs|"
               r"essentials|hits|songs)$", re.IGNORECASE),
]
_BY = re.compile(r"^(?P<q>.+?)\s+by\s+(?P<artist>.+)$", re.IGNORECASE)
# Styles, moods and occasions: "play jazz" wants a playlist, not an artist
# called Jazz. Only a nudge — the resolver still weighs everything.
_GENRE_WORDS = re.compile(
    r"\b(?:jazz|blues|soul|funk|disco|rock|pop|punk|metal|indie|folk|country|bluegrass|classical|opera|"
    r"hip[\s-]?hop|rap|r\s*(?:&|and|n)\s*b|reggae|reggaeton|reggaet[oó]n|latin|salsa|bachata|merengue|cumbia|"
    r"banda|corridos|mariachi|k-?pop|afrobeats?|edm|house|techno|electronic|dance|ambient|lo-?fi|chillhop|"
    r"gospel|worship|christmas|holiday|halloween|kids|children'?s|lullab(?:y|ies)|oldies|classics|hits|"
    r"chill|relaxing|calm|mellow|upbeat|happy|sad|romantic|love\s+songs|party|workout|running|focus|"
    r"study|sleep|dinner|cooking|brunch|morning|evening|beats|instrumental|acoustic|piano|"
    r"(?:19|20)?\d0'?s)\b", re.IGNORECASE)
_GENRE_FILLER = re.compile(r"\b(?:music|songs?|some|the|and|&|mix|playlist|station|radio|old|school|new|"
                           r"best|top|of|good|great)\b|[-'’]", re.IGNORECASE)
_SOMETHING = re.compile(r"^(?:something|anything)\s+(?P<g>.+)$", re.IGNORECASE)
_NOTHING_NAMED = re.compile(r"^(?:some\s+)?(?:music|something|anything|some\s+tunes|a\s+song|songs|tunes)$",
                            re.IGNORECASE)


def _clean(text: str) -> str:
    t = re.sub(r"\s+", " ", text.strip().strip(".?!,\"“”")).strip()
    t = _TRAIL.sub("", t)
    t = re.sub(_POLITE, "", t, flags=re.IGNORECASE).strip()
    # Whisper often hears "play X" over music as "played X"
    return re.sub(r"^played\b", "play", t, flags=re.IGNORECASE)


def _speaker_suffix(text: str):
    """Split "… on the soundbar" into (text, "soundbar"); "… everywhere" → "*"."""
    m = re.search(rf"\s+{_EVERYWHERE}$", text, re.IGNORECASE)
    if m:
        return text[: m.start()].strip(), "*"
    keys = sorted(settings.MUSIC_SPEAKERS, key=len, reverse=True)
    if not keys:
        return text, None
    alt = "|".join(re.escape(k).replace(r"\ ", r"\s+") for k in keys)
    m = re.search(rf"\s+(?:on|in|to|through|over|in\s+the)\s+(?:the\s+|my\s+)?(?P<spk>{alt})"
                  rf"(?:\s+(?:speakers?|room))?$", text, re.IGNORECASE)
    if not m:
        return text, None
    return text[: m.start()].strip(), re.sub(r"\s+", " ", m.group("spk").lower())


def speaker_key(name: str) -> str | None:
    name = re.sub(r"\s+(?:speakers?|room)$", "", re.sub(r"\s+", " ", name.lower().strip(" .?!")))
    name = re.sub(r"^(?:the|my)\s+", "", name)
    return name if name in settings.MUSIC_SPEAKERS else None


def _years(s: str) -> tuple[int, int]:
    s = s.lower().replace("the ", "").replace("'", "").strip()
    if re.fullmatch(r"(?:19|20)\d\d", s):
        return int(s), int(s)
    m = re.fullmatch(r"(19|20)?(\d)0s", s)
    century = int(m.group(1)) * 100 if m.group(1) else (2000 if m.group(2) in "012" else 1900)
    start = century + int(m.group(2)) * 10
    return start, start + 9


def _ord(word: str) -> str:
    w = word.lower()
    if re.fullmatch(_OLDER_WORDS, w) or re.fullmatch(_OLDEST_WORDS, w):
        return "oldest"
    return "newest"


def _navigation(t: str) -> MusicIntent | None:
    def nav(action, **kw):
        return MusicIntent("control", action, **kw)
    m = _SLEEP.match(t)
    if m:
        d = (m.group("d") or m.group("d2")).strip()
        if re.fullmatch(r"(?:this|the)\s+(?:song|track|one)", d, re.IGNORECASE):
            return nav("sleep_after_song")
        if re.fullmatch(r"(?:this|the)\s+(?:album|playlist|queue)", d, re.IGNORECASE):
            return nav("sleep_after_queue")
        secs = _seconds(d)
        return nav("sleep", seconds=secs) if secs else None
    for rx, sign in ((_SKIP_N, 1), (_BACK_N, -1)):
        m = rx.match(t)
        if m:
            n = _count(re.sub(r"\s+of$", "", m.group("n")))
            return nav("skip_songs", count=sign * n) if n else None
    m = _SEEK_TO.match(t)
    if m:
        secs = _seconds(m.group("d"))
        if secs is not None:
            return nav("seek_to", seconds=secs)
    for rx, sign, default in ((_SEEK_FWD, 1, 30), (_SEEK_BACK, -1, 15)):
        m = rx.match(t)
        if m:
            d = m.group("d")
            if d and re.search(rf"\b{_SONGS}$", d):
                return None           # "go back a song" is previous, "skip two songs" skip_songs
            secs = _seconds(d) if d else default
            return nav("seek", seconds=sign * secs) if secs else None
    m = _TRACK_N.match(t)
    if m:
        n = _count(m.group("n") or m.group("o"))
        if n:
            return nav("play_track_number", count=n)
    for action, rx in _NAV:
        if rx.search(t):
            return nav(action)
    return None


def parse_music(transcript: str) -> MusicIntent | None:
    t = _clean(transcript)
    if not t:
        return None

    # ── navigation (before controls: "stop the music in 30 minutes" isn't a pause)
    nav = _navigation(t)
    if nav:
        return nav

    # ── enqueue: "play X next" / "add X to the queue" → a play with enqueue set
    enq = ""
    m = _ENQ_ADD.match(t)
    if m:
        t, enq = "play " + (m.group("rest") or m.group("rest2")), "add"
    else:
        m = _ENQ_NEXT_PREFIX.match(t)
        if m:
            t, enq = "play " + m.group("rest"), "next"
        elif re.match(r"^(?:play|put\s+on)\s+", t, re.IGNORECASE) and _ENQ_NEXT_SUFFIX.search(t) \
                and not re.match(r"^(?:play|put\s+on)\s+(?:the\s+)?next(?:\s+(?:song|track|one))?$", t, re.IGNORECASE):
            t, enq = _ENQ_NEXT_SUFFIX.sub("", t), "next"
    if enq:
        it = parse_music(t)
        if it and it.kind == "play" and it.action not in ("resume", "pick"):
            it.enqueue = enq
            return it
        return None

    # ── multi-room
    if _GROUP_ALL.match(t):
        return MusicIntent("control", "group_all", speaker="*")
    for action, rx in (("group_add", _GROUP_ADD), ("group_remove", _GROUP_REMOVE), ("group_only", _GROUP_ONLY)):
        m = rx.match(t)
        if m:
            key = speaker_key(next(g for g in m.groups() if g))
            if key:
                return MusicIntent("control", action, speaker=key)

    # ── controls
    tm = _TRANSFER.match(t)
    if tm:
        key = speaker_key(tm.group("spk") or tm.group("spk2"))
        if key:
            return MusicIntent("control", "transfer", speaker=key)
    body, spk = _speaker_suffix(t)
    vm = _VOLUME_SET.search(body)
    if vm:
        level = int(vm.group(1) or vm.group(2))
        if 0 <= level <= 100:
            return MusicIntent("control", "volume_set", level=level, speaker=spk)
    for action, rx in _CONTROLS:
        if rx.search(body):
            return MusicIntent("control", action, speaker=spk)

    # ── info
    for rx in _INFO:
        m = rx.match(t)
        if m:
            artist = m.group("artist").strip()
            ordw = m.groupdict().get("ord")
            plural = m.group("plural").lower().endswith("s")
            if not ordw:
                action = "albums"
            elif _ord(ordw) == "oldest":
                action = "oldest" if not plural else "older"
            else:
                action = "newest" if not plural else "newer"
            return MusicIntent("info", action, artist=artist)

    # ── play
    pm = _PLAY.match(t)
    if not pm:
        return None
    rest, spk = _speaker_suffix(pm.group("rest").strip())
    # Punctuation Whisper adds ("the song, So Sick", "\"Chapel Rhone.\"")
    # carries nothing a name match needs.
    rest = re.sub(r"\s*,\s*", " ", rest.strip(" .?!,\"“”"))
    rest = _ON_APPLE.sub("", rest).strip()
    shuffle = pm.group("verb").lower() == "shuffle"
    if not rest or _NOT_MUSIC.match(rest):
        return None

    def play(action, **kw):
        return MusicIntent("play", action, speaker=spk, shuffle=shuffle, raw=rest, **kw)

    if _NOTHING_NAMED.match(rest):
        return play("resume")
    if _FAVORITES.match(rest):
        return play("favorites")
    if _LIBRARY.match(rest):
        return play("library")
    m = _MY_PLAYLIST.match(rest)
    if m:
        return play("my_playlist", query=(m.group("q") or m.group("q2")).strip())
    m = _LYRICS.match(rest)
    if m:
        return play("lyrics", query=m.group("lyrics").strip(" \"'"))
    m = _PICK.match(rest)
    if m:
        if m.group("it"):
            return play("pick", ordinal=None)
        return play("pick", ordinal=_ORDINALS[(m.group("ord") or m.group("num")).lower()])
    m = _NEWEST.match(rest)
    if m:
        ordw = m.group("o1") or m.group("o2")
        return play(_ord(ordw), artist=(m.group("a1") or m.group("a2")).strip())
    for rx in _YEAR_PLAY:
        m = rx.match(rest)
        if m:
            y0, y1 = _years(m.group("year"))
            return play("year", artist=m.group("artist").strip(), year_from=y0, year_to=y1)
    m = _TRACK.match(rest)
    if m:
        return play("track", query=m.group("q").strip(), artist=(m.group("artist") or "").strip())
    for rx in _ALBUM:
        m = rx.match(rest)
        if m:
            return play("album", query=m.group("q").strip(), artist=(m.group("artist") or "").strip())
    for rx in _ARTIST:
        m = rx.match(rest)
        if m:
            return play("artist", artist=m.group("artist").strip())
    m = _BY.match(rest)
    if m:
        # Song or album: the resolver decides, and tries other "by" splits
        # ("Stand by Me by Ben E. King").
        return play("any", query=m.group("q").strip(), artist=m.group("artist").strip())
    m = _SOMETHING.match(rest)
    if m:                                   # "something relaxing"
        return play("any", query=m.group("g").strip(), genre=True)
    # A genre word alone doesn't make a genre ("Uptown Funk", "Piano Man"):
    # the whole request has to be genre words and filler.
    leftover = _GENRE_FILLER.sub(" ", _GENRE_WORDS.sub(" ", rest)).strip()
    genre = bool(re.match(r"^some\s+", rest, re.IGNORECASE) or re.search(r"\s+music$", rest, re.IGNORECASE)
                 or not leftover)
    # "the" stays: matching ignores it, but "the killers" names the band
    return play("any", query=re.sub(r"^some\s+", "", rest, flags=re.IGNORECASE).strip(), genre=genre)


# ── corrections ("no, the Adele one") ──────────────────────────────────────


@dataclass
class Correction:
    kind: str          # "artist" | "other" | "original" | "type"
    artist: str = ""   # kind artist: whose version
    type: str = ""     # kind type: track | album | artist | playlist


_CORR_LEAD = re.compile(
    r"^(?:(?:no|nope|nah|wrong|actually)\b[,.!]?\s*|not\s+(?:that|this)(?:\s+(?:one|song|version))?[,.!]?\s*|"
    r"i\s+(?:meant|said|wanted)\s+|play\s+)+", re.IGNORECASE)
_CORR_OTHER = re.compile(
    r"^(?:no[,.!]?\s+)?(?:(?:play\s+)?the\s+other\s+(?:one|song|version)|(?:play\s+)?a\s+different\s+(?:one|song|version)|"
    r"(?:that'?s\s+|you\s+played\s+)?the\s+wrong\s+(?:song|one|version|artist)|wrong\s+(?:song|one|version|artist)|"
    r"not\s+(?:that|this)(?:\s+(?:one|song|version))?|that'?s\s+not\s+(?:it|the\s+(?:right\s+)?(?:one|song))|"
    r"not\s+the\s+right\s+(?:one|song)|try\s+(?:again|another\s+one))$", re.IGNORECASE)
_CORR_ORIGINAL = re.compile(r"^(?:the\s+)?original(?:\s+(?:one|version|song|recording))?$", re.IGNORECASE)
_CORR_TYPE = re.compile(r"^the\s+(?P<t>album|song|track|artist|band|playlist)(?:\s+instead)?$", re.IGNORECASE)
_CORR_BY = [
    re.compile(r"^the\s+(?:one|version|song)\s+(?:by|from)\s+(?P<a>.+)$", re.IGNORECASE),
    re.compile(r"^the\s+(?P<a>.+?)\s+(?:one|version|song)$", re.IGNORECASE),
    re.compile(r"^(?P<a>.+?)(?:'s|s'|’s)\s+(?:one|version|song)$", re.IGNORECASE),
]
_CORR_BY_BARE = re.compile(r"^(?:by|from)\s+(?P<a>.+)$", re.IGNORECASE)   # only after "no"
_CORR_TYPES = {"album": "album", "song": "track", "track": "track", "artist": "artist",
               "band": "artist", "playlist": "playlist"}
# "the X one" where X isn't an artist
_CORR_NOT_ARTIST = re.compile(r"^(?:other|different|original|first|second|third|last|next|previous|right|"
                              r"correct|same|new|old|live|studio|whole|album|song|other\s+song)$", re.IGNORECASE)


def parse_correction(transcript: str) -> Correction | None:
    t = _clean(transcript)
    if not t or _CORR_OTHER.match(t):
        return Correction("other") if t else None
    body = _CORR_LEAD.sub("", t).strip(" ,.")
    said_no = body != t
    if not body:
        return None
    if _CORR_OTHER.match(body):
        return Correction("other")
    if _CORR_ORIGINAL.match(body):
        return Correction("original")
    m = _CORR_TYPE.match(body)
    if m and (said_no or body.lower().endswith("instead")):
        return Correction("type", type=_CORR_TYPES[m.group("t").lower()])
    for rx in _CORR_BY:
        m = rx.match(body)
        if m:
            a = m.group("a").strip()
            if re.fullmatch(r"other|different", a, re.IGNORECASE):
                return Correction("other")
            if re.fullmatch(r"original", a, re.IGNORECASE):
                return Correction("original")
            if _CORR_NOT_ARTIST.match(a):
                return None
            return Correction("artist", artist=a)
    m = _CORR_BY_BARE.match(body)
    if m and said_no:
        return Correction("artist", artist=m.group("a").strip())
    return None
