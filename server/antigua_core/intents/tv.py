"""Living room TV commands (Apple TV + Roku, see home_control.py)."""

import re


# A mention of the TV. "apple tv" counts: it's the same screen.
_TV = r"(?:apple\s+tv|tv|roku|television)"

_TV_RE = re.compile(
    r"\b("
    rf"turn\s+(on|off)\s+(the\s+)?{_TV}|"
    rf"turn\s+(the\s+)?{_TV}\s+(on|off)|"
    rf"power\s+(on|off)\s+(the\s+)?{_TV}|"
    rf"{_TV}\s+(power\s+)?(on|off)|"
    rf"mute\s+(the\s+)?{_TV}|"
    rf"unmute\s+(the\s+)?{_TV}|"
    rf"{_TV}\s+(volume\s+)?(up|down)|"
    rf"volume\s+(up|down)\s+(on\s+)?(the\s+)?{_TV}|"
    rf"{_TV}\s+home|"
    rf"go\s+home\s+(on\s+)?(the\s+)?{_TV}|"
    rf"{_TV}\s+back|"
    rf"go\s+back\s+(on\s+)?(the\s+)?{_TV}|"
    # Playback needs the TV named — bare "pause" is the satellite's media route.
    rf"(pause|unpause|resume|play)\s+(the\s+)?{_TV}|"
    rf"{_TV}\s+(pause|unpause|resume|play)"
    r")\b",
    re.IGNORECASE,
)

# Matches input-switching phrases and captures the target name/label.
_TV_INPUT_RE = re.compile(
    r"\b(?:"
    r"switch(?:\s+the\s+(?:tv|roku))?\s+to|"
    r"change(?:\s+the\s+(?:tv|roku))?\s+(?:input\s+)?to|"
    # "set the TV to HDMI 3" — the TV must be named: bare "set it to 3" is a
    # timer or volume. "to" keeps "turn the TV on" with _TV_RE.
    r"(?:set|turn|put|flip)\s+the\s+(?:tv|roku)(?:\s+input)?\s+to|"
    r"(?:tv|roku)\s+input\s+(?:to\s+)?|"
    r"put\s+(?:it|the\s+(?:tv|roku))\s+on"
    r")\s+(.+?)(?:\s+on\s+(?:the\s+)?(?:tv|roku)|[?.,!]|$)",
    re.IGNORECASE,
)

# "hdmi 3", "input three", or a bare "3" ("change the tv input to 3").
_HDMI_NUM_RE = re.compile(r"^(?:(?:hdmi|input)[\s\-]?)?(\w+)$", re.IGNORECASE)

# App launching ("open netflix", "put on youtube on the tv"). A command verb
# is required so "I watched Netflix" stays with the LLM. Checked before
# _TV_INPUT_RE so "switch to hulu" opens the app rather than looking for an
# input called hulu. Names must have a row in home_control._TV_APPS.
_TV_APP_RE = re.compile(
    r"\b(?:open|launch|start|put\s+on|pull\s+up|go\s+to|turn\s+on|"
    r"switch(?:\s+the\s+(?:tv|roku))?\s+to)\s+(?:the\s+)?("
    r"netflix|you\s?tube(?:\s+tv)?|amazon\s+prime(?:\s+video)?|prime\s+video|hulu|"
    r"disney\s*(?:plus|\+)|hbo\s+max|apple\s+tv\s*(?:plus|\+)|peacock|"
    r"paramount\s*(?:plus|\+)|espn|tubi|sling(?:\s+tv)?|starz|pluto(?:\s+tv)?|"
    r"plex|twitch|spotify|crunchyroll|apple\s+music"
    r")(?![\w+])",
    re.IGNORECASE,
)
