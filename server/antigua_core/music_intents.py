"""Music intent parsing — pure, network-free, shared by classify() and music.py.

parse_music(transcript) → MusicIntent | None. Three kinds:

  control  — playback verbs on whatever is playing: pause, resume, next,
             previous, restart_track, restart_queue, repeat_one/all/off,
             shuffle_on/off, now_playing, volume_up/down/set, transfer
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
               any      "play <something>": artist → track → album → playlist
               resume   "play music" / "play something" with nothing named

Checked in that order (control → info → play) so "play it again" restarts
the song instead of searching for a song called "it again".
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
    genre: bool = False          # "some X" / "X music": a style or mood, prefer playlists


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
                      r"from\s+the\s+top|start\s+it\s+over|play\s+(?:it|that)\s+again|replay\s+(?:it|that))\b"),
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
    ("next", r"^(?:next|skip)$|\b(?:next\s+(?:song|track|one)|skip\s+(?:this\s+|the\s+)?(?:song|track|one)|"
             r"skip\s+(?:this|it)|play\s+the\s+next\s+(?:song|track|one))\b"),
    ("previous", r"\b(?:(?:previous|prev)\s+(?:song|track|one)|go\s+back\s+(?:a|one)\s+(?:song|track)|"
                 r"play\s+the\s+(?:previous|last)\s+(?:song|track|one)|back\s+(?:a|one)\s+(?:song|track))\b"),
    ("volume_up", r"\b(?:turn\s+(?:the\s+)?music\s+up|turn\s+up\s+the\s+music|"
                  r"(?:make\s+)?the\s+music\s+louder|music\s+(?:louder|up)|crank\s+(?:up\s+)?the\s+music)\b"),
    ("volume_down", r"\b(?:turn\s+(?:the\s+)?music\s+down|turn\s+down\s+the\s+music|"
                    r"(?:make\s+)?the\s+music\s+(?:quieter|softer)|music\s+(?:quieter|softer|down)|lower\s+the\s+music)\b"),
    ("pause", r"^(?:pause|stop)(?:\s+it)?$|\bpause\s+(?:the\s+|this\s+)?(?:music|song|track|it)\b|"
              r"\bstop\s+(?:the\s+)?(?:music|song|playing|playback)\b|^pause\s+(?:please|for\s+a\s+(?:sec|second|minute))$"),
    ("resume", r"^(?:resume|unpause|continue)$|^play$|\b(?:resume|unpause|continue)\s+(?:the\s+|this\s+)?(?:music|song|track|playing|playback)\b|"
               r"\bkeep\s+playing\b|^resume\s+(?:it|please)$|\bstart\s+the\s+music\s+again\b"),
]
_CONTROLS = [(a, re.compile(p, re.IGNORECASE)) for a, p in _CONTROLS]

_VOLUME_SET = re.compile(
    r"\b(?:set\s+)?(?:the\s+)?music(?:'s)?\s+(?:volume\s+)?(?:to\s+|at\s+)?(\d{1,3})\s*(?:percent|%)?$"
    r"|\bmusic\s+volume\s+(?:to\s+)?(\d{1,3})\b", re.IGNORECASE)

# "move the music to the soundbar", "play this in the bedroom"
_TRANSFER = re.compile(
    r"^(?:move|send|transfer|switch|put)\s+(?:the\s+)?(?:music|song|this|it|playback|that)\s+(?:over\s+)?"
    r"(?:to|onto|on|in|into)\s+(?:the\s+)?(?P<spk>.+)$"
    r"|^(?:play|continue)\s+(?:this|it|that|the\s+music)\s+(?:on|in)\s+(?:the\s+)?(?P<spk2>.+)$",
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
    r"^(?P<verb>play|put\s+on|listen\s+to|shuffle|queue\s+up|"
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
    re.compile(r"^(?:some\s+|more\s+|a\s+few\s+)?(?:songs|music|hits|tracks|stuff)\s+(?:by|from)\s+(?P<artist>.+)$",
               re.IGNORECASE),
    re.compile(r"^(?:some\s+|more\s+)?(?P<artist>.+?)(?:'s|s'|’s)?\s+(?:greatest\s+hits|top\s+songs|best\s+songs|"
               r"essentials|hits|songs)$", re.IGNORECASE),
]
_BY = re.compile(r"^(?P<q>.+?)\s+by\s+(?P<artist>.+)$", re.IGNORECASE)
_NOTHING_NAMED = re.compile(r"^(?:some\s+)?(?:music|something|anything|some\s+tunes|a\s+song|songs|tunes)$",
                            re.IGNORECASE)


def _clean(text: str) -> str:
    t = re.sub(r"\s+", " ", text.strip().strip(".?!,")).strip()
    t = _TRAIL.sub("", t)
    t = re.sub(_POLITE, "", t, flags=re.IGNORECASE).strip()
    # Whisper often hears "play X" over music as "played X"
    return re.sub(r"^played\b", "play", t, flags=re.IGNORECASE)


def _speaker_suffix(text: str):
    """Split "… on the soundbar" into (text, "soundbar")."""
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


def parse_music(transcript: str) -> MusicIntent | None:
    t = _clean(transcript)
    if not t:
        return None

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
    rest = _ON_APPLE.sub("", rest).strip()
    shuffle = pm.group("verb").lower() == "shuffle"
    if not rest or _NOT_MUSIC.match(rest):
        return None

    def play(action, **kw):
        return MusicIntent("play", action, speaker=spk, shuffle=shuffle, raw=rest, **kw)

    if _NOTHING_NAMED.match(rest):
        return play("resume")
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
        return play("track", query=m.group("q").strip(), artist=m.group("artist").strip())
    genre = bool(re.match(r"^some\s+", rest, re.IGNORECASE) or re.search(r"\s+music$", rest, re.IGNORECASE))
    return play("any", query=re.sub(r"^(?:some|the)\s+", "", rest, flags=re.IGNORECASE).strip(), genre=genre)
