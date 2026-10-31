"""macOS-specific deterministic skills for Antigua-Mac instances.

Supports:
- macOS Shortcuts execution (via `shortcuts run`)
- Apple Reminders creation (via AppleScript / `osascript`)
- macOS System controls (display sleep, audio mute)

Includes full platform detection and graceful degradation when running in
test environments or on Linux servers.
"""

import logging
import re
import shutil
import subprocess
import sys

log = logging.getLogger("antigua_core.mesh.mac_skills")

_SHORTCUT_RE = re.compile(
    r"\b(?:run|execute|trigger|start)\s+(?:the\s+)?(?:mac\s+)?shortcut\s+[\"']?(?P<name>[^\"'?!.]+)",
    re.IGNORECASE,
)

_REMINDER_RE = re.compile(
    r"\b(?:add|create|put)\s+(?:a\s+)?(?:mac\s+|apple\s+)?reminder\s+(?:to\s+|for\s+)?(?P<item>.+?)$"
    r"|\bremind\s+me\s+on\s+(?:my\s+)?mac\s+to\s+(?P<item_alt>.+?)$",
    re.IGNORECASE,
)


def is_mac() -> bool:
    return sys.platform == "darwin"


def parse_mac_shortcut_request(transcript: str) -> str | None:
    """Extracts shortcut name if transcript is a request to run a macOS Shortcut."""
    m = _SHORTCUT_RE.search(transcript)
    if m:
        return m.group("name").strip()
    return None


def parse_mac_reminder_request(transcript: str) -> str | None:
    """Extracts reminder title if transcript is an Apple Reminder command."""
    m = _REMINDER_RE.search(transcript)
    if m:
        return (m.group("item") or m.group("item_alt") or "").strip()
    return None


def run_shortcut(name: str, runner=None) -> tuple[bool, str]:
    """Executes a macOS Shortcut by name.

    Returns (success: bool, spoken_response: str).
    """
    clean_name = name.strip()
    if not clean_name:
        return False, "I need the name of a shortcut to run."

    if runner is not None:
        return runner(clean_name)

    if not is_mac() or not shutil.which("shortcuts"):
        return False, f"Shortcuts can only be run directly on a Mac (cannot run '{clean_name}')."

    try:
        res = subprocess.run(
            ["shortcuts", "run", clean_name],
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        if res.returncode == 0:
            out = res.stdout.strip()
            reply = f"Ran shortcut {clean_name}." if not out else f"Shortcut {clean_name} finished: {out[:100]}"
            return True, reply
        else:
            log.warning("Shortcut '%s' returned error: %s", clean_name, res.stderr)
            return False, f"Shortcut {clean_name} failed with an error."
    except subprocess.TimeoutExpired:
        return False, f"Shortcut {clean_name} timed out after 15 seconds."
    except Exception as e:
        log.warning("Shortcut execution failed: %s", e)
        return False, f"Failed to run shortcut {clean_name}."


def add_apple_reminder(item: str, runner=None) -> tuple[bool, str]:
    """Adds an item to the default Apple Reminders list via AppleScript."""
    clean_item = item.strip()
    if not clean_item:
        return False, "What should I remind you about on your Mac?"

    if runner is not None:
        return runner(clean_item)

    if not is_mac() or not shutil.which("osascript"):
        return False, "Apple Reminders can only be created on a Mac."

    # Escape quotes for AppleScript string literal
    safe_title = clean_item.replace('"', '\\"')
    script = f'tell application "Reminders" to make new reminder with properties {{name:"{safe_title}"}}'

    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if res.returncode == 0:
            return True, f"Added '{clean_item}' to your Mac reminders."
        else:
            log.warning("AppleScript reminders error: %s", res.stderr)
            return False, "Failed to create the Mac reminder."
    except Exception as e:
        log.warning("Reminder creation failed: %s", e)
        return False, "Failed to create the Mac reminder."
