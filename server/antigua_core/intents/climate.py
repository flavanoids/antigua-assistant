"""Indoor climate questions: the thermostat's temperature, humidity and
setpoint, and whether the AC is running. Answered from the AC controller's
read-only data server (antigua_core/climate.py)."""

import re

_INSIDE = (r"(?:inside|in\s+(?:the|here|this)\s*(?:house|home)?|indoors|in\s+here|"
           r"downstairs|upstairs|in\s+the\s+living\s+room)")

_CLIMATE_RE = [
    ("running", re.compile(
        r"\b(?:is|was|has)\s+the\s+(?:a\.?\s?c\.?|air(?:\s+conditioning|\s+conditioner)?|ac)\s+"
        r"(?:running|on|working|been\s+running|cooling)\b|"
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
    """"temp" / "humidity" / "setpoint" / "running", or None."""
    for kind, rx in _CLIMATE_RE:
        if rx.search(text):
            return kind
    return None
