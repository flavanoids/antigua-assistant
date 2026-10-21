"""Internet and server-health questions, answered by antigua_core/sysinfo.py.

Every pattern names the internet/Wi-Fi/connection or the server/system/CPU/
GPU/disk, so "how are you doing" and "is everything okay?" stay chat, and
"temperature inside" stays the thermostat's.
"""

import re

_NET = r"(?:the\s+)?(?:internet|wi-?fi|network|connection|internet\s+connection)"
_BOX = r"(?:the\s+)?(?:server|system|computer|box)"

_SYSINFO_RE = [
    ("net_outages", re.compile(
        rf"\bwhen\s+(?:did|was)\s+{_NET}\s+(?:go(?:ing)?\s+(?:out|down)|last\s+(?:out|down)|"
        r"come\s+back|drop|out|down)\b|"
        rf"\bhow\s+long\s+(?:was|has)\s+{_NET}\s+(?:been\s+)?(?:down|out)\b|"
        r"\b(?:last|recent|any)\s+(?:internet\s+|network\s+|wi-?fi\s+)?outages?\b|"
        r"\binternet\s+outages?\b|"
        rf"\bhas\s+{_NET}\s+(?:gone|been)\s+(?:out|down)\b", re.I)),
    ("net_why", re.compile(
        rf"\bwhy\s+(?:is|isn'?t|is\s+not|did)\s+{_NET}\b|"
        rf"\bwhat(?:'s|\s+is)\s+(?:wrong|going\s+on|up)\s+with\s+{_NET}\b", re.I)),
    ("net_status", re.compile(
        rf"\b(?:is|are)\s+{_NET}\s+(?:down|out|working|up|ok(?:ay)?|back(?:\s+up)?|slow|connected)\b|"
        r"\b(?:do|does)\s+(?:you|we|antigua)\s+have\s+(?:an?\s+|any\s+)?"
        r"(?:internet|wi-?fi|network|connection|internet\s+connection)\b|"
        r"\bare\s+(?:you|we)\s+(?:online|offline|connected(?:\s+to\s+the\s+internet)?)\b|"
        r"\bis\s+there\s+(?:an?\s+|any\s+)?(?:internet|connection)\b|"
        rf"\b(?:check|test)\s+{_NET}\b|\b(?:internet|network|wi-?fi)\s+status\b|"
        r"\bcan\s+you\s+(?:get\s+)?(?:online|on\s+the\s+internet|reach\s+the\s+internet)\b", re.I)),
    ("temp", re.compile(
        r"\b(?:cpu|processor|server|gpu|computer|system)\s+(?:temp|temperature)s?\b|"
        r"\bhow\s+(?:hot|warm)\s+(?:is|are)\s+(?:the\s+)?(?:server|cpu|gpu|computer|processor)\b", re.I)),
    ("gpu", re.compile(
        r"\b(?:gpu|graphics\s+card)\s+(?:usage|load|status|busy|use)\b|"
        r"\bhow(?:'s|\s+is)\s+the\s+(?:gpu|graphics\s+card)\b|"
        r"\bhow\s+busy\s+is\s+the\s+(?:gpu|graphics\s+card)\b|\bvram\b", re.I)),
    ("load", re.compile(
        r"\b(?:cpu|processor|server|system)\s+(?:load|usage|use)\b|\bload\s+average\b|"
        rf"\bhow\s+(?:busy|loaded)\s+is\s+(?:{_BOX}|the\s+(?:cpu|processor))\b", re.I)),
    ("memory", re.compile(
        r"\b(?:memory|ram)\s+(?:usage|use)\b|"
        r"\bhow\s+much\s+(?:memory|ram)\s+(?:is\s+)?(?:used|free|left|available|"
        r"(?:are\s+you|is\s+the\s+server)\s+using)\b|"
        r"\b(?:server|system)(?:'s)?\s+(?:memory|ram)\b", re.I)),
    ("disk", re.compile(
        r"\b(?:disk|drive|hard\s+drive|storage)\s+(?:space|usage|use)\b|"
        r"\bhow\s+full\s+(?:is|are)\s+(?:the\s+)?(?:disks?|drives?|storage|server)\b|"
        r"\bhow\s+much\s+(?:disk\s+|storage\s+|hard\s+drive\s+)?(?:space|storage)\s+"
        r"(?:is\s+|do\s+(?:you|we)\s+have\s+)?(?:left|free|available)\b", re.I)),
    ("uptime", re.compile(
        rf"\buptime\b|\bhow\s+long\s+has\s+{_BOX}\s+been\s+(?:up|running|on)\b|"
        rf"\bwhen\s+did\s+{_BOX}\s+(?:last\s+)?(?:restart|reboot|start)\b", re.I)),
    ("services", re.compile(
        r"\bis\s+everything\s+(?:running|up(?:\s+and\s+running)?)\b|"
        r"\bare\s+(?:all\s+)?(?:the\s+|your\s+)?services\s+(?:running|up|working|okay|ok)\b|"
        r"\bare\s+all\s+(?:your\s+|the\s+)?systems\s+(?:go|working|running|up|okay|ok)\b|"
        r"\bare\s+any\s+(?:services|systems)\s+down\b", re.I)),
    ("summary", re.compile(
        rf"\b(?:server|system|systems)\s+(?:status|health|report|check)\b|"
        rf"\bhow(?:'s|\s+is)\s+{_BOX}\s+(?:doing|running|holding\s+up)?\b|"
        r"\b(?:run|do)\s+a\s+(?:health|system|systems|status)\s+check\b|"
        r"\bhow\s+are\s+your\s+systems\b|\bstatus\s+report\b|\bhealth\s+check\b", re.I)),
]


def parse_sysinfo_request(text: str) -> str | None:
    """"net_status" / "net_why" / "net_outages" / "summary" / "services" /
    "load" / "memory" / "disk" / "temp" / "gpu" / "uptime", or None."""
    for kind, rx in _SYSINFO_RE:
        if rx.search(text):
            return kind
    return None
