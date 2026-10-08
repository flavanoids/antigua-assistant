"""Drive-time routing: "how long to get to Lowe's on Ella", "how far is Mom's",
"how's traffic to work". Car only, always from home."""

import re
from dataclasses import dataclass


@dataclass
class CommuteRequest:
    dest: str      # "Lowe's on Ella", "Niko Niko's", "mom's house"
    kind: str      # "eta" | "distance" | "traffic" | "area" (dest = a road, or "")


_TRAVEL = r"(?:get|drive|go|head|make\s+it|reach|travel|run|ride)"
_ON_WAY = r"(?:to|on\s+the\s+way\s+to|getting\s+to|going\s+to|heading\s+to|over\s+to)"
# "heading the Galleria": STT drops the "to" often enough to allow it.
_ON_WAY_LOOSE = (r"(?:to|on\s+the\s+way\s+(?:to|over\s+to)|over\s+to|"
                 r"(?:getting|going|heading|driving|headed)(?:\s+(?:over\s+)?to)?)")
_TRAFFIC_LOOK = (r"(?:what(?:'s|\s+is|\s+does)|how(?:'s|\s+is|\s+does))\s+(?:the\s+)?traffic\s+"
                 r"(?:look(?:ing|s)?)(?:\s+like)?")
_TRAFFIC_STATE = (r"(?:is|'s)\s+(?:the\s+)?traffic\s+(?:bad|heavy|backed\s+up|slow|terrible|awful|"
                  r"rough|ok(?:ay)?|good|moving|light|clear)")

_PATTERNS = [
    # "how long to get to X", "how long does it take to drive to X",
    # "how long will it take me to get over to X", "how long of a drive is it to X"
    ("eta", re.compile(
        r"\bhow\s+(?:long|much\s+time|many\s+minutes)\b(?:\s+(?:is\s+it|does\s+it\s+take|would\s+it\s+take|"
        r"will\s+it\s+take|is\s+the\s+drive|of\s+a\s+drive\s+is\s+it|a\s+drive\s+is\s+it|would\s+it\s+be|"
        r"will\s+it\s+be))?(?:\s+(?:me|us))?(?:\s+to\s+" + _TRAVEL + r")?(?:\s+over)?\s+to\s+"
        r"(?P<dest>.+)", re.I)),
    ("eta", re.compile(r"\bhow\s+long\s+(?:is|'s)\s+the\s+(?:drive|commute|ride|trip)\s+to\s+(?P<dest>.+)", re.I)),
    ("eta", re.compile(
        r"\bwhat(?:'s|\s+is)\s+(?:the|my|our)\s+(?:eta|e\.t\.a\.|drive\s+time|travel\s+time|commute|"
        r"drive)\s+(?:to|for)\s+(?P<dest>.+)", re.I)),
    ("eta", re.compile(r"^(?:my\s+)?(?:eta|e\.t\.a\.|drive\s+time)\s+(?:to|for)\s+(?P<dest>.+)", re.I)),
    ("distance", re.compile(r"\bhow\s+far\s+(?:away\s+)?is\s+it\s+to\s+(?P<dest>.+)", re.I)),
    ("distance", re.compile(r"\bhow\s+far\s+(?:away\s+)?(?:is|'s)\s+(?P<dest>.+)", re.I)),
    ("distance", re.compile(r"\bhow\s+far\s+(?:to|till)\s+(?P<dest>.+)", re.I)),
    ("traffic", re.compile(
        r"\b(?:how(?:'s|\s+is)|how\s+bad\s+is|what(?:'s|\s+is))\s+(?:the\s+)?traffic(?:\s+like)?\s+"
        + _ON_WAY + r"\s+(?P<dest>.+)", re.I)),
    ("traffic", re.compile(r"\b(?:is|are)\s+there\s+(?:any\s+)?(?:traffic|delays?|accidents?|wrecks?)\s+"
                           + _ON_WAY + r"\s+(?P<dest>.+)", re.I)),
    ("traffic", re.compile(r"\b" + _TRAFFIC_LOOK + r"\s+" + _ON_WAY_LOOSE + r"\s+(?P<dest>.+)", re.I)),
    ("traffic", re.compile(r"\b" + _TRAFFIC_STATE + r"\s+" + _ON_WAY_LOOSE + r"\s+(?P<dest>.+)", re.I)),
    ("traffic", re.compile(r"\bis\s+it\s+(?:bad|backed\s+up|busy|slow)\s+" + _ON_WAY_LOOSE
                           + r"\s+(?P<dest>.+)", re.I)),
    ("traffic", re.compile(r"^(?:any\s+)?traffic\s+" + _ON_WAY + r"\s+(?P<dest>.+)", re.I)),
]

# No destination: "how's traffic", "is there traffic on I-10", "any wrecks on 610".
_AREA_TAIL = (r"(?:\s+(?:right\s+now|now|today|tonight|out\s+there|around\s+here|near\s+(?:us|me|here)|"
              r"this\s+(?:morning|afternoon|evening)|in\s+houston|looking))*$")
_AREA_PATTERNS = [
    re.compile(r"^(?:how(?:'s|\s+is)|how\s+bad\s+is|what(?:'s|\s+is))\s+(?:the\s+)?traffic(?:\s+like)?"
               r"(?:\s+(?:on|along)\s+(?P<road>.+?))?" + _AREA_TAIL, re.I),
    re.compile(r"^(?:is|are)\s+there\s+(?:any\s+)?(?:bad\s+)?(?:traffic|accidents?|wrecks?|delays?|crashes?)"
               r"(?:\s+(?:on|along)\s+(?P<road>.+?))?" + _AREA_TAIL, re.I),
    re.compile(r"^(?:any\s+)?(?:traffic|accidents?|wrecks?|crashes?)\s+(?:on|along)\s+(?P<road>.+?)" + _AREA_TAIL, re.I),
    re.compile(r"^" + _TRAFFIC_LOOK + r"(?:\s+(?:on|along)\s+(?P<road>.+?))?" + _AREA_TAIL, re.I),
    re.compile(r"^" + _TRAFFIC_STATE + r"(?:\s+(?:on|along)\s+(?P<road>.+?))?" + _AREA_TAIL, re.I),
]

# The bare forms ("how long to X", "how far is X") also fit questions that
# aren't trips: "how long to boil an egg", "how far is the moon".
_NOT_A_PLACE_RE = re.compile(
    r"^(?:bake|cook|boil|make|roast|grill|fry|air\s*fry|smoke|steam|heat|reheat|microwave|"
    r"steep|brew|rest|marinate|thaw|defrost|cool|chill|soak|simmer|sear|toast|charge|wait|"
    r"sleep|nap|walk|run|jog|bike|swim|fly|learn|finish|read|watch|listen|beat|play|"
    r"get|go|do|be|have|keep|stay|become|see|hear|feel|lose|gain|recover|heal|digest|take|"
    r"pay|save|fix|build|drive|ship|deliver|arrive|load|install|download|update|upload|"
    r"christmas|thanksgiving|halloween|easter|new\s+year|my\s+birthday|the\s+weekend|"
    r"the\s+(?:moon|sun|stars?|mars|horizon|end)|mars|jupiter|space|pluto|"
    r"(?:a|an|the)\s+(?:egg|steak|turkey|chicken|ham|roast|cake|pie|bread))\b",
    re.I)
_FROM_ELSEWHERE_RE = re.compile(r"\bfrom\s+(?!here\b|home\b|the\s+house\b|my\s+house\b|our\s+house\b)\S", re.I)
_TRAILING_RE = re.compile(
    r"(?:\s+(?:right\s+now|now|from\s+here|from\s+home|from\s+(?:the|my|our)\s+house|today|tonight|"
    r"this\s+(?:morning|afternoon|evening)|by\s+car|if\s+i\s+(?:leave|left|go)\s+now|"
    r"in\s+traffic|with\s+traffic|away|please|for\s+me))+\s*$", re.I)


def parse_commute_request(text: str) -> CommuteRequest | None:
    t = text.strip().rstrip("?.! ")
    for kind, rx in _PATTERNS:
        m = rx.search(t)
        if not m:
            continue
        dest = _TRAILING_RE.sub("", m.group("dest")).strip(" ,")
        dest = re.sub(r"\bthe\s+the\b", "the", dest, flags=re.I)   # STT: "heading the The Galleria"
        dest = re.sub(r"^(?:get|go|drive)\s+to\s+", "", dest, flags=re.I)
        if not dest or _NOT_A_PLACE_RE.match(dest) or _FROM_ELSEWHERE_RE.search(m.group("dest")):
            return None
        return CommuteRequest(dest, kind)
    for rx in _AREA_PATTERNS:
        m = rx.match(t)
        if m:
            return CommuteRequest((m.group("road") or "").strip(" ,"), "area")
    return None
