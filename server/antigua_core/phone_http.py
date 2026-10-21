"""The Ask Antigua shortcut's door: a small HTTP listener for the household's
iPhones, iPads, Macs and Watches (docs/ios_shortcut.md).

Antigua's own port only answers over wg0, so this is a separate one with a
single job: POST /ask {"text": "...", "device": "Alex's iPhone"} runs the turn
the way a spoken one runs after STT and answers {"reply": ..., ...}; the phone
says or shows the reply itself, nothing plays in the kitchen.

Two locks on it: the caller must be on the LAN, wg0 or this host, and must send
the household key (X-Antigua-Key) from phone.key_file, which the shortcut
carries. Without a key file the listener doesn't start.

Who's asking: the phone's MAC (from its address) against household devices,
else a household name in the device name ("Alex's iPhone"). That's the speaker
for "remind me", memories and announcements.
"""

import hmac
import ipaddress
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

from . import household, server_common

log = logging.getLogger("antigua_core")

MAX_TEXT_CHARS = 500


def read_key(path) -> str | None:
    try:
        key = Path(path).read_text().strip()
    except OSError:
        return None
    return key if len(key) >= 16 else None


def allowed_client(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_private or a.is_loopback or a.is_link_local


def person_from_device(name: str) -> str | None:
    """'Alex's iPhone' -> 'Alex'; None unless exactly one household name is in it."""
    if not name:
        return None
    found = {household.canonical(m.group(1)) for m in household.people()["query"].finditer(name)}
    found.discard(None)
    return found.pop() if len(found) == 1 else None


def make_handler(key: str, dispatch, presence=None):
    """dispatch(text, conversation_id, speaker) -> the pipeline's result dict."""

    class PhoneHandler(BaseHTTPRequestHandler):
        server_version = "Antigua"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("phone %s - %s", self.client_address[0], fmt % args)

        def _json(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _gate(self) -> bool:
            ip = self.client_address[0]
            if not allowed_client(ip):
                log.warning("phone: refused %s (not on the LAN)", ip)
                self._json(403, {"error": "LAN only"})
                return False
            sent = self.headers.get("X-Antigua-Key", "")
            if not hmac.compare_digest(sent.encode(), key.encode()):
                log.warning("phone: bad key from %s", ip)
                self._json(401, {"error": "wrong or missing X-Antigua-Key"})
                return False
            return True

        def do_GET(self):
            if urlparse(self.path).path == "/health":
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/ask":
                self._json(404, {"error": "not found"})
                return
            if not self._gate():
                return
            body = server_common.read_body(self, server_common.MAX_JSON_BYTES)
            if body is None:
                return
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON"})
                return
            if not isinstance(data, dict):
                self._json(400, {"error": "expected an object"})
                return
            text = re.sub(r"\s+", " ", str(data.get("text") or "")).strip()[:MAX_TEXT_CHARS]
            if not text:
                self._json(200, {"reply": "What would you like to ask?", "expects_reply": True})
                return
            ip = self.client_address[0]
            device = str(data.get("device") or "")[:80]
            person = (presence.person_for_ip(ip) if presence is not None else None) \
                or person_from_device(device)
            conv = "phone-" + (person or re.sub(r"[^A-Za-z0-9]+", "-", device) or ip).lower()
            log.info("phone: %s (%s, %s) asks %r", person or "someone", device or "?", ip, text)
            try:
                result = dispatch(text, conv, person)
            except Exception:   # noqa: BLE001 — the phone gets an answer either way
                log.exception("phone: turn failed")
                self._json(200, {"reply": "Sorry, something went wrong on my end.",
                                 "expects_reply": False})
                return
            reply = (result.get("response") or "").strip() or "Sorry, I didn't catch that."
            self._json(200, {
                "reply": reply,
                "expects_reply": bool(result.get("expects_reply")),
                "person": person,
                "action": result.get("action", ""),
            })

    return PhoneHandler


def start(cfg: dict, dispatch, presence=None) -> ThreadingHTTPServer | None:
    p = cfg.get("phone") or {}
    if not p.get("enabled", True) or not p:
        return None
    key = read_key(p.get("key_file", "/etc/antigua/phone.key"))
    if key is None:
        log.warning("phone: no key in %s (16+ characters) — Ask Antigua is off",
                    p.get("key_file", "/etc/antigua/phone.key"))
        return None
    host, port = p.get("host", "0.0.0.0"), int(p.get("port", 9395))
    server = ThreadingHTTPServer((host, port), make_handler(key, dispatch, presence))
    server.daemon_threads = True
    Thread(target=server.serve_forever, name="phone-http", daemon=True).start()
    log.info("phone: Ask Antigua on %s:%d", host, port)
    return server
