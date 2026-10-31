"""Cryptographic authentication, request signing, and pairing PIN verification."""

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from threading import Lock

log = logging.getLogger("antigua_core.mesh.auth")

MAX_DRIFT_SECONDS = 300  # 5 minutes replay window
MAX_PIN_ATTEMPTS = 5
PIN_EXPIRY_SECONDS = 300  # 5 minutes


@dataclass
class PairingSession:
    session_id: str
    pin: str
    initiator_node_id: str
    initiator_name: str
    initiator_endpoint: str
    created_at: float
    attempts: int = 0
    expires_at: float = 0.0

    def __post_init__(self):
        if not self.expires_at:
            self.expires_at = self.created_at + PIN_EXPIRY_SECONDS

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class MeshAuth:
    """Handles request signing, verification, and pairing challenge/response."""

    _replay_lock = Lock()
    _seen_signatures: dict[str, float] = {}

    def __init__(self):
        self._lock = Lock()
        self._pairing_sessions: dict[str, PairingSession] = {}

    # ── Request Signing & Verification ──────────────────────────────────────

    @staticmethod
    def hash_body(body: bytes | str | None) -> str:
        if body is None:
            raw = b""
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            raw = body
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def compute_signature(shared_secret: str, method: str, path: str,
                          timestamp: int | float | str, body_hash: str,
                          nonce: str = "") -> str:
        canonical = f"{int(float(timestamp))}\n{nonce}\n{method.upper()}\n{path}\n{body_hash}"
        return hmac.new(
            shared_secret.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def sign_request(cls, shared_secret: str, node_id: str, method: str,
                     path: str, body: bytes | str | None = None) -> dict[str, str]:
        """Generate headers for authenticating an outgoing peer request."""
        ts = int(time.time())
        nonce = secrets.token_hex(8)
        b_hash = cls.hash_body(body)
        sig = cls.compute_signature(shared_secret, method, path, ts, b_hash, nonce=nonce)
        return {
            "X-Antigua-Node-ID": node_id,
            "X-Antigua-Timestamp": str(ts),
            "X-Antigua-Nonce": nonce,
            "X-Antigua-Signature": sig,
        }

    @classmethod
    def verify_request(cls, shared_secret: str, method: str, path: str,
                       body: bytes | str | None, headers: dict) -> tuple[bool, str]:
        """Verify the signature and timestamp of an incoming peer request.

        Includes strict replay attack prevention via sliding signature cache.
        Returns (valid: bool, reason: str).
        """
        # Case-insensitive header lookup
        hdrs = {k.lower(): v for k, v in headers.items()}
        ts_str = hdrs.get("x-antigua-timestamp")
        sig = hdrs.get("x-antigua-signature")
        nonce = hdrs.get("x-antigua-nonce", "")

        if not ts_str or not sig:
            return False, "missing required signature headers"

        try:
            ts = int(ts_str)
        except ValueError:
            return False, "invalid timestamp header"

        now = int(time.time())
        if abs(now - ts) > MAX_DRIFT_SECONDS:
            return False, "timestamp drift exceeded (possible replay attack)"

        b_hash = cls.hash_body(body)
        # Verify with nonce first; fall back to empty nonce for legacy requests
        expected = cls.compute_signature(shared_secret, method, path, ts, b_hash, nonce=nonce)
        if not hmac.compare_digest(expected, sig) and nonce:
            expected_legacy = cls.compute_signature(shared_secret, method, path, ts, b_hash, nonce="")
            if hmac.compare_digest(expected_legacy, sig):
                expected = expected_legacy

        if not hmac.compare_digest(expected, sig):
            return False, "signature mismatch"

        # Sliding replay cache check
        with cls._replay_lock:
            now_f = time.time()
            cutoff = now_f - MAX_DRIFT_SECONDS
            # Evict expired entries
            expired = [s for s, t in cls._seen_signatures.items() if t < cutoff]
            for s in expired:
                cls._seen_signatures.pop(s, None)

            if sig in cls._seen_signatures:
                return False, "replay detected: duplicate request signature"
            cls._seen_signatures[sig] = now_f

        return True, "ok"

    # ── Pairing Session Manager ─────────────────────────────────────────────

    def create_pairing_session(self, initiator_node_id: str, initiator_name: str,
                               initiator_endpoint: str) -> tuple[str, str]:
        """Creates a pairing session. Returns (session_id, 6_digit_pin)."""
        with self._lock:
            self._cleanup_expired()
            session_id = secrets.token_hex(16)
            # 6-digit numeric PIN
            pin = f"{secrets.randbelow(1000000):06d}"
            session = PairingSession(
                session_id=session_id,
                pin=pin,
                initiator_node_id=initiator_node_id,
                initiator_name=initiator_name,
                initiator_endpoint=initiator_endpoint,
                created_at=time.time(),
            )
            self._pairing_sessions[session_id] = session
            log.info("Pairing session created: %s for %s (%s)",
                     session_id, initiator_name, initiator_node_id)
            return session_id, pin

    def verify_pin(self, session_id: str, pin: str) -> tuple[bool, PairingSession | None, str]:
        """Verifies the PIN for a pairing session.

        Returns (success: bool, session: PairingSession | None, reason: str).
        """
        with self._lock:
            self._cleanup_expired()
            session = self._pairing_sessions.get(session_id)
            if not session:
                return False, None, "pairing session expired or not found"

            session.attempts += 1
            if session.attempts > MAX_PIN_ATTEMPTS:
                self._pairing_sessions.pop(session_id, None)
                return False, None, "too many incorrect attempts; session terminated"

            if not hmac.compare_digest(session.pin, pin.strip()):
                remaining = MAX_PIN_ATTEMPTS - session.attempts
                return False, None, f"invalid PIN ({remaining} attempts remaining)"

            # Validated successfully — consume session
            self._pairing_sessions.pop(session_id, None)
            return True, session, "ok"

    def _cleanup_expired(self):
        expired = [sid for sid, s in self._pairing_sessions.items() if s.is_expired()]
        for sid in expired:
            self._pairing_sessions.pop(sid, None)
