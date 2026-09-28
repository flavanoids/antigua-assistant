"""Shopping and to-do lists."""

import re


# Named lists default to "shopping" and "todo"; unnamed ("the list") defaults
# to shopping — the more common household use. Aliases fold synonyms
# ("grocery"/"groceries", "to-do"/"to do"/"task"/"tasks") onto the canonical name.

_LIST_NAME_GROUP = r"(shopping|grocery|groceries|to-do|to do|todo|task|tasks)"
_LIST_ALIASES = {
    "shopping": "shopping", "grocery": "shopping", "groceries": "shopping",
    "todo": "todo", "to-do": "todo", "to do": "todo", "task": "todo", "tasks": "todo",
}

_LIST_ADD_RE = re.compile(
    r"\b(?:add|put)\s+(.+?)\s+(?:to|on)\s+(?:the\s+|my\s+)?"
    rf"(?:{_LIST_NAME_GROUP}\s+)?list\b",
    re.IGNORECASE,
)
_LIST_QUERY_RE = re.compile(
    rf"\bwhat.?s\s+on\s+(?:the\s+|my\s+)?(?:{_LIST_NAME_GROUP}\s+)?list\b"
    rf"|\bread\s+(?:me\s+)?(?:the\s+|my\s+)?(?:{_LIST_NAME_GROUP}\s+)?list\b",
    re.IGNORECASE,
)
_LIST_CLEAR_RE = re.compile(
    rf"\b(?:clear|empty)\s+(?:the\s+|my\s+)?(?:{_LIST_NAME_GROUP}\s+)?list\b",
    re.IGNORECASE,
)
_LIST_REMOVE_RE = re.compile(
    r"\b(?:remove|delete|take)\s+(.+?)\s+(?:from|off)\s+(?:the\s+|my\s+)?"
    rf"(?:{_LIST_NAME_GROUP}\s+)?list\b",
    re.IGNORECASE,
)


def _normalize_list_name(raw: str | None) -> str:
    if not raw:
        return "shopping"
    key = re.sub(r"\s+", " ", raw.strip().lower()).replace("-", " ")
    return _LIST_ALIASES.get(key, key)


def _split_list_items(raw: str) -> list[str]:
    """Split 'milk, eggs and bread' into ['milk', 'eggs', 'bread']."""
    raw = re.sub(r"\s*,?\s+and\s+", ", ", raw, flags=re.IGNORECASE)
    items = [i.strip().rstrip(".,!?") for i in raw.split(",")]
    return [i for i in items if i]


def parse_list_add_request(transcript: str) -> tuple[str, list[str]] | None:
    """Return (list_name, items) for an 'add X to the list' request, else None."""
    m = _LIST_ADD_RE.search(transcript)
    if not m:
        return None
    items = _split_list_items(m.group(1).strip())
    if not items:
        return None
    return _normalize_list_name(m.group(2)), items


def is_list_query(transcript: str) -> bool:
    return bool(_LIST_QUERY_RE.search(transcript))


def extract_list_query_name(transcript: str) -> str:
    """Return the normalized list name from a query, defaulting to shopping."""
    m = _LIST_QUERY_RE.search(transcript)
    if not m:
        return "shopping"
    named = next((g for g in m.groups() if g), None)
    return _normalize_list_name(named)


def parse_list_remove_request(transcript: str) -> tuple[str, str | None] | None:
    """Return (list_name, item) to remove one item, (list_name, None) to clear
    the whole list, or None if this isn't a list-remove request."""
    m = _LIST_CLEAR_RE.search(transcript)
    if m:
        return _normalize_list_name(m.group(1)), None
    m = _LIST_REMOVE_RE.search(transcript)
    if not m:
        return None
    item = m.group(1).strip().rstrip(".,!?")
    if not item:
        return None
    return _normalize_list_name(m.group(2)), item
