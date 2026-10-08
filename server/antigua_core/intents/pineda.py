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
