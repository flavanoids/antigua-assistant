"""PinedaDisplay (the airplaypi kiosk screen) commands and questions.

Pure regex, no network: which theme to switch to is settled by the handler
against the themes the display actually has (see antigua_core.pineda).
"""

import re


# "Pi" often comes out of STT as "pie"; "airplay pi" as one word or two.
_PI = r"(?:raspberry\s+(?:pi|pie)|air\s*play\s*(?:pi|pie)|(?:the\s+)?(?:pi|pie))"
_SCREEN = r"(?:display|screen|dashboard|kiosk)"

_REBOOT_RE = re.compile(
    rf"\b(?:reboot|restart|power\s+cycle)\s+(?:the\s+)?(?:display\s+)?{_PI}\b",
    re.IGNORECASE,
)
_RESTART_DISPLAY_RE = re.compile(
    rf"\b(?:restart|reload|refresh|reboot|reset)\s+(?:the\s+)?{_SCREEN}\b",
    re.IGNORECASE,
)

# Not "the theme of Macbeth", "the Star Wars theme song", "a theme for the party".
_THEME_WORD_RE = re.compile(
    r"\bthemes?\b(?!\s+(?:of|from|for|in|song|songs|music|tune|park)\b)", re.IGNORECASE
)
_THEME_CURRENT_RE = re.compile(
    r"\b(?:what|which)(?:'s|\s+is)?\s+(?:the\s+)?(?:current\s+)?theme\b(?!s)",
    re.IGNORECASE,
)
_THEME_LIST_RE = re.compile(
    r"\b(?:what|which)\s+themes\b|\blist\s+(?:the\s+|all\s+)?themes\b"
    r"|\bthemes?\s+(?:are\s+there|do\s+you\s+have|can\s+you)\b",
    re.IGNORECASE,
)
_THEME_RANDOM_RE = re.compile(
    r"\b(?:random|surprise\s+me|different|another|new)\b", re.IGNORECASE
)
_THEME_SET_RE = re.compile(
    r"\b(?:change|switch|set|make|use|turn|try|go\s+with)\b", re.IGNORECASE
)
# "play the Jeopardy theme" is music, whatever else it says.
_MUSIC_RE = re.compile(r"\b(?:play|put\s+on|listen\s+to|hum|sing)\b", re.IGNORECASE)
# "change the display to groovy" — a theme change without the word "theme".
_SCREEN_TO_RE = re.compile(
    rf"\b(?:change|switch|set|make|turn)\s+(?:the\s+)?{_SCREEN}\s+(?:to|into)\s+\w+",
    re.IGNORECASE,
)

# A determiner is required so "give me a quote" still reaches the LLM.
_QUOTE_RE = re.compile(
    r"\b(?:the|this|that|today'?s)\s+(?:quote|quotation)\b|\bquote\s+of\s+the\s+day\b",
    re.IGNORECASE,
)
_QUOTE_WHO_RE = re.compile(r"\bwho\b|\bwhose\b|\bauthor\b", re.IGNORECASE)
_QUOTE_MORE_RE = re.compile(
    r"\b(?:mean|meaning|more|explain|expand|about|context|background|behind"
    r"|significance|elaborate)\b",
    re.IGNORECASE,
)

_PHRASE_RE = re.compile(
    r"\b(?:the|this|that|today'?s)\s+spanish\s+(?:phrase|word|expression|saying)\b"
    r"|\bspanish\s+(?:phrase|word)\s+of\s+the\s+day\b"
    r"|\b(?:phrase|word)\s+of\s+the\s+day\b"
    rf"|\b(?:the|this|that)\s+(?:phrase|expression)\s+on\s+(?:the\s+)?{_SCREEN}\b"
    # "say the phrase" / "pronounce the phrase (in spanish)" — the bare phrase
    # must end the request, so "what does the phrase break a leg mean" doesn't match.
    r"|\b(?:say|pronounce|read)\s+(?:the|this|that|today'?s)\s+phrase(?:\s+in\s+spanish)?\s*[.?!]*$",
    re.IGNORECASE,
)

_PHOTO = r"(?:the|this|that)\s+(?:photo|picture|pic|image)"
_PHOTO_WHEN_RE = re.compile(
    rf"\b(?:when|what\s+(?:year|day|date|month|time)|how\s+old)\b.*\b{_PHOTO}\b"
    rf"|\b{_PHOTO}\s+(?:is\s+)?from\s+when\b"
    r"|\bwhen\s+was\s+(?:this|that)\s+(?:taken|shot)\b",
    re.IGNORECASE,
)

_IMAGE_SEARCH_RE = re.compile(
    r"^(?:(?:alexa|antigua|hey)[\s,]+)*(?:(?:can|could)\s+you\s+)?(?:please\s+)?"
    r"(?:show(?:\s+me|\s+us)?|look\s+up|search\s+for|display|find)?\s*"
    r"(?:some\s+|a\s+few\s+)?(?:pictures|photos|images|pics)\s+of\s+(.+?)(?:\s+please)?\s*[.?!]*$",
    re.IGNORECASE,
)
_NUM_WORDS = {
    "1": "A", "2": "B", "3": "C", "4": "D", "5": "E", "6": "F", "7": "G", "8": "H",
    "one": "A", "two": "B", "three": "C", "four": "D", "five": "E", "six": "F", "seven": "G", "eight": "H",
    "first": "A", "second": "B", "third": "C", "fourth": "D", "fifth": "E", "sixth": "F", "seventh": "G", "eighth": "H",
}
_IMAGE_DETAIL_RE = re.compile(
    r"^(?:(?:alexa|antigua|hey)[\s,]+)*(?:(?:can|could)\s+you\s+)?(?:please\s+)?"
    r"(?:tell\s+me\s+(?:more\s+)?about|what\s+(?:is|was)(?:\s+in)?|what's(?:\s+in)?|describe|explain|show\s+me|who\s+is)?\s*"
    r"(?:(?:picture|photo|image|pic|tile|block|item|letter|number)\s+([a-hA-H]|[1-8]|one|two|three|four|five|six|seven|eight)"
    r"|(?:the\s+)?(first|second|third|fourth|fifth|sixth|seventh|eighth)\s+(?:picture|photo|image|pic|tile|block|item))"
    r"(?:\s+please)?\s*[.?!]*$",
    re.IGNORECASE,
)
_IMAGE_DETAIL_SHORT_RE = re.compile(
    r"^(?:(?:alexa|antigua|hey)[\s,]+)*(?:(?:can|could)\s+you\s+)?(?:please\s+)?"
    r"(?:tell\s+me\s+(?:more\s+)?about|what\s+is|what's)\s+([a-hA-H])(?:\s+please)?\s*[.?!]*$",
    re.IGNORECASE,
)


def parse_pineda_request(transcript: str):
    """(action, arg) for a display request, or None.

    Actions: reboot, restart_display, theme_list, theme_current,
    theme_random, theme (arg = the transcript, for the handler to match
    against real theme names; no match means a random one), theme_named
    (no change verb — "the groovy theme please" — so only a matched name
    counts), quote_who, quote_more, quote_read, phrase, photo_when.
    """
    if not transcript:
        return None
    t = transcript.strip()

    if _REBOOT_RE.search(t):
        return "reboot", None
    if _RESTART_DISPLAY_RE.search(t):
        return "restart_display", None

    if _THEME_WORD_RE.search(t) and not _MUSIC_RE.search(t):
        if _THEME_LIST_RE.search(t):
            return "theme_list", None
        if _THEME_CURRENT_RE.search(t):
            return "theme_current", None
        if _THEME_RANDOM_RE.search(t):
            return "theme_random", None
        if _THEME_SET_RE.search(t):
            return "theme", t
        return "theme_named", t
    elif _SCREEN_TO_RE.search(t):
        return "theme", t

    if _QUOTE_RE.search(t):
        if _QUOTE_WHO_RE.search(t):
            return "quote_who", None
        if _QUOTE_MORE_RE.search(t):
            return "quote_more", None
        return "quote_read", None

    if _PHRASE_RE.search(t):
        return "phrase", None

    if _PHOTO_WHEN_RE.search(t):
        return "photo_when", None

    m = _IMAGE_SEARCH_RE.search(t)
    if m:
        query = m.group(1).strip()
        if query and not query.lower().startswith("the recipe"):
            return "image_search", query

    m = _IMAGE_DETAIL_RE.search(t)
    if m:
        token = (m.group(1) or m.group(2)).lower()
        return "image_detail", _NUM_WORDS.get(token, token.upper())

    m = _IMAGE_DETAIL_SHORT_RE.search(t)
    if m:
        return "image_detail", m.group(1).upper()

    return None


# ── the full-screen recipe card ──────────────────────────────────────────────

# "Show" is the keyword: "can you show the recipe again", "show me the
# ingredients". Not "show me a recipe for tacos" / "show me the recipe for
# lasagna" — those ask for a new one.
_RECIPE_SHOW_RE = re.compile(
    r"\bshow\b(?:\s+(?:me|us))?(?:\s+(?:the|that|this|my|our|last))+\s+(?:recipe|ingredients|steps)\b"
    r"(?!\s+for\s+(?!(?:it|this|that|the\s+same)\b))",
    re.IGNORECASE,
)
# "Stop the display", "have the display go back", "hide the recipe". Not
# "close the recipe": that ends the recipe itself (and the card with it).
_RECIPE_HIDE_RE = re.compile(
    rf"\b(?:stop|hide|clear|dismiss|exit|remove|take\s+down|get\s+rid\s+of)\s+(?:the\s+)?(?:recipe\s+)?(?:{_SCREEN}|card|view)\b"
    rf"|\b{_SCREEN}\s+(?:to\s+)?go\s+back\b"
    rf"|\b{_SCREEN}\s+back\s+to\s+normal\b"
    r"|\b(?:take|get)\s+(?:the\s+)?recipe\s+(?:off|down)\b"
    r"|\b(?:hide|dismiss|take\s+down)\s+(?:the\s+)?recipe\b"
    r"|\bgo\s+back\s+to\s+(?:the\s+)?(?:dashboard|normal|home\s+screen|photos?|clock)\b",
    re.IGNORECASE,
)


# ── Clock only / the dashboard ───────────────────────────────────────────────

# "Clock mode", "switch the display to the clock", "show the clock", "go
# back to the clock". Not "alarm clock", "set the clock", "what time is it".
# A whole request on its own may still carry the wake word: "Alexa, home".
_LEAD = r"^(?:(?:alexa|antigua|hey)[\s,]+)*(?:please\s+)?"
_CLOCK = r"(?<!alarm\s)clock(?:\s+(?:mode|only|face|screen|view))?"
_CLOCK_ON_RE = re.compile(
    rf"{_LEAD}(?:the\s+)?{_CLOCK}(?:\s+on)?(?:\s+please)?\s*[.!?]*$"
    rf"|\b(?:switch|change|go|flip|put|turn|set)\s+(?:back\s+)?(?:the\s+)?(?:{_SCREEN}\s+|it\s+)?"
    rf"(?:back\s+)?(?:to|into|on)\s+(?:the\s+)?(?:a\s+)?{_CLOCK}\b"
    rf"|\b(?:show|give)\s+(?:me\s+|us\s+)?(?:just\s+)?(?:the\s+)?(?:big\s+)?{_CLOCK}\b"
    rf"(?!\s+(?:settings|options))"
    rf"|\b(?:turn\s+on|start|enter)\s+(?:the\s+)?clock\s+(?:mode|only)\b"
    rf"|\bjust\s+(?:the\s+|a\s+)?clock\b",
    re.IGNORECASE,
)
# "Home", "go home", "back to the dashboard", "home screen", "exit clock mode".
_CLOCK_OFF_RE = re.compile(
    rf"{_LEAD}(?:go\s+)?(?:back\s+)?(?:to\s+)?(?:the\s+)?(?:home|dashboard)(?:\s+screen)?"
    r"(?:\s+please)?\s*[.!?]*$"
    rf"|\b(?:switch|change|go|flip|put|turn|set|take)\s+(?:back\s+)?(?:the\s+)?(?:{_SCREEN}\s+|it\s+)?"
    r"(?:back\s+)?(?:to|on)\s+(?:the\s+)?(?:home(?:\s+screen)?|dashboard)\b"
    # "turn on the home lights", "switch to the home theater" aren't the screen.
    r"(?!\s+(?!please|now|again|instead)\w)"
    r"|\bshow\s+(?:me\s+|us\s+)?the\s+(?:dashboard|home\s+screen)\b"
    r"|\b(?:exit|leave|close|stop|end|quit|turn\s+off|switch\s+off|cancel)\s+(?:the\s+)?clock"
    r"(?:\s+(?:mode|only|face|screen|view))?\b"
    r"|\bclock\s+(?:mode|only)\s+off\b"
    rf"|\b{_SCREEN}\s+(?:to\s+)?go\s+(?:back\s+)?home\b",
    re.IGNORECASE,
)


def parse_clock_mode(transcript: str):
    """"clock" (Clock only on the kiosk) / "home" (the dashboard), or None."""
    if not transcript:
        return None
    t = transcript.strip()
    if _CLOCK_OFF_RE.search(t):
        return "home"
    if _CLOCK_ON_RE.search(t):
        return "clock"
    return None


def parse_recipe_display(transcript: str):
    """"show" / "hide" for the recipe card on the display, or None."""
    if not transcript:
        return None
    t = transcript.strip()
    if _RECIPE_HIDE_RE.search(t):
        return "hide"
    if _RECIPE_SHOW_RE.search(t):
        return "show"
    return None
