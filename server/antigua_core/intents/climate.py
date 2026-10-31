"""Indoor climate questions: the thermostat's temperature, humidity and
setpoint, and whether the AC is cooling, heating or off. Answered from the AC
controller's read-only data server (antigua_core/climate.py). Also the one
thing voice may change there: windows-open mode ("I'm gonna open the
windows"), which the controller turns into system Off for a while."""

import re

_INSIDE = (r"(?:inside|in\s+(?:the|here|this)\s*(?:house|home)?|indoors|in\s+here|"
           r"downstairs|upstairs|in\s+the\s+living\s+room)")

# Windows-open mode. Said about what people are doing ("I'm gonna open the
# windows", "we closed the windows", "the windows are open") or to the mode
# itself ("turn off windows mode"); a bare "open the windows" is a command
# she can't carry out, so it's left alone. Closing is tested first so "turn
# off windows open mode" isn't an open.
_WIN = (r"(?:the\s+|some\s+(?:of\s+the\s+)?|a\s+few\s+|all\s+(?:of\s+)?the\s+|a\s+|those\s+)?"
        r"(?:house\s+)?windows?"
        r"(?!\s+(?:on|in)\s+(?:my|the|your|his|her)\s+(?:laptop|computer|pc|desktop|screen|browser))")
_WHO = (r"(?:i|we|i'?m|we'?re|i\s+am|we\s+are|i'?ll|we'?ll|let'?s|let\s+me)"
        r"(?:\s+(?:gonna|going\s+to|about\s+to|fixing\s+to|will|just|already|have|went\s+and))*")
_MODE = r"windows?(?:[\s-]+open)?\s+mode"

_CLIMATE_RE = [
    ("windows_status", re.compile(
        rf"\bis\s+(?:the\s+)?{_MODE}\s+(?:on|engaged|active)\b|"
        r"\bare\s+(?:the\s+)?windows\s+(?:still\s+)?open\b", re.I)),
    ("windows_closed", re.compile(
        rf"\b{_WHO}\s+(?:clos(?:e|ed|ing)|shut(?:ting)?)\s+{_WIN}\b|"
        rf"\b{_WIN}\s+(?:are|is)\s+(?:all\s+)?(?:now\s+)?(?:closed|shut)\b|"
        rf"\b(?:turn|switch|shut)\s+off\s+(?:the\s+)?{_MODE}\b|"
        rf"\b(?:disengage|end|stop|cancel|disable)\s+(?:the\s+)?{_MODE}\b|\b{_MODE}\s+off\b", re.I)),
    ("windows_open", re.compile(
        rf"\b{_WHO}\s+(?:open(?:ed|ing)?|crack(?:ed|ing)?)\s+(?:up\s+)?{_WIN}\b|"
        rf"\b{_WIN}\s+(?:are|is)\s+(?:all\s+)?(?:now\s+)?open\b|"
        rf"\b(?:turn|switch)\s+on\s+(?:the\s+)?{_MODE}\b|"
        rf"\b(?:engage|start|enable|activate)\s+(?:the\s+)?{_MODE}\b|\b{_MODE}\s+on\b", re.I)),
    ("running", re.compile(
        r"\b(?:is|was|has)\s+the\s+(?:a\.?\s?c\.?|air(?:\s+conditioning|\s+conditioner)?|ac)\s+"
        r"(?:running|on|working|been\s+running|cooling|heating)\b|"
        r"\bis\s+the\s+(?:heat(?:er|ing)?|furnace|hvac)\s+(?:on|running|working)\b|"
        r"\bwhat(?:'s|\s+is)\s+the\s+(?:a\.?\s?c\.?|ac|air(?:\s+conditioning)?)\s+doing\b|"
        r"\bis\s+the\s+(?:a\.?\s?c\.?|ac|air)\s+(?:cooling\s+or\s+heating|heating\s+or\s+cooling)\b|"
        r"\bhow\s+(?:long|much)\s+has\s+the\s+(?:a\.?\s?c\.?|ac|air)\s+(?:been\s+)?run|"
        r"\bis\s+the\s+(?:ac|a\.?c\.?|air)\s+off\b", re.I)),
    ("setpoint", re.compile(
        r"\bwhat(?:'s|\s+is)\s+the\s+(?:thermostat|a\.?\s?c\.?|ac)\s+(?:set\s+(?:to|at)|on)\b|"
        r"\bwhat(?:'s|\s+is)\s+the\s+(?:thermostat|ac)\s+setting\b|"
        r"\bwhat\s+(?:temperature\s+)?is\s+the\s+(?:thermostat|ac)\s+set\b|"
        r"\bwhat\s+did\s+the\s+(?:thermostat|ac)\s+get\s+set\b", re.I)),
    ("humidity", re.compile(rf"\bhow\s+humid\s+is\s+it\s+{_INSIDE}|\bhumidity\s+{_INSIDE}|"
                            r"\bindoor\s+humidity\b|\bhumidity\s+in\s+the\s+house\b", re.I)),
    ("temp", re.compile(
        rf"\b(?:temperature|temp)\s+{_INSIDE}|\bhow\s+(?:hot|warm|cold|cool)\s+is\s+it\s+{_INSIDE}|"
        rf"\bwhat(?:'s|\s+is)\s+it\s+{_INSIDE}\b|\bindoor\s+temp(?:erature)?\b|"
        r"\bwhat(?:'s|\s+is)\s+the\s+thermostat\s+(?:say|reading|read|showing)\b|"
        r"\bwhat\s+does\s+the\s+thermostat\s+(?:say|read|show)\b|"
        r"\bhow\s+(?:hot|warm|cold)\s+is\s+the\s+house\b", re.I)),
]


def parse_climate_request(text: str) -> str | None:
    """"windows_closed" / "windows_open" / "windows_status" / "running" /
    "setpoint" / "humidity" / "temp", or None."""
    for kind, rx in _CLIMATE_RE:
        if rx.search(text):
            return kind
    return None
