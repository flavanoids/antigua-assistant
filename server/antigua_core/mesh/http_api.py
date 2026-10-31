"""HTTP endpoints mixin for Antigua Mesh node pairing, state sync, and skill delegation."""

import json
import logging
import secrets
import time
from urllib.parse import urlparse

from .auth import MeshAuth
from .identity import MeshIdentity
from .peers import Peer, PeerRegistry
from .router import MeshSkillsRouter
from .sync import MeshSync

log = logging.getLogger("antigua_core.mesh.http")

MAX_MESH_JSON_BYTES = 5 * 1024 * 1024  # 5 MB max payload for sync


class MeshRoutes:
    """Mixin for Antigua HTTP server handlers to serve the /mesh/* endpoints.

    Requires:
      - `self.mesh_identity`: MeshIdentity
      - `self.mesh_peers`: PeerRegistry
      - `self.mesh_auth`: MeshAuth
      - `self.mesh_sync`: MeshSync
      - `self.mesh_router`: MeshSkillsRouter
      - `self._json(status_code: int, data: dict)`
      - `self.rfile`, `self.headers`, `self.path`
    """

    mesh_identity: MeshIdentity | None = None
    mesh_peers: PeerRegistry | None = None
    mesh_auth: MeshAuth | None = None
    mesh_sync: MeshSync | None = None
    mesh_router: MeshSkillsRouter | None = None

    def route_mesh(self, method: str) -> bool:
        """Dispatches /mesh/* routes. Returns True if handled, False otherwise."""
        path = urlparse(self.path).path
        if not path.startswith("/mesh"):
            return False

        if path == "/mesh/info" and method == "GET":
            self._handle_mesh_info()
        elif path == "/mesh/pair/initiate" and method == "POST":
            self._handle_mesh_pair_initiate()
        elif path == "/mesh/pair/confirm" and method == "POST":
            self._handle_mesh_pair_confirm()
        elif path == "/mesh/peers" and method == "GET":
            self._handle_mesh_peers_list()
        elif path.startswith("/mesh/peers/") and method == "DELETE":
            node_id = path[len("/mesh/peers/"):]
            self._handle_mesh_peer_delete(node_id)
        elif path == "/mesh/sync" and method == "POST":
            self._handle_mesh_sync()
        elif path == "/mesh/delegate" and method == "POST":
            self._handle_mesh_delegate()
        else:
            self._json(404, {"error": "no such mesh endpoint"})
        return True

    def _read_mesh_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_MESH_JSON_BYTES:
                return b""
            return self.rfile.read(length) if length > 0 else b""
        except Exception:
            return b""

    def _read_mesh_json(self) -> dict | None:
        raw = self._read_mesh_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _authenticate_peer(self, body: bytes) -> tuple[Peer | None, str]:
        sender_id = self.headers.get("X-Antigua-Node-ID")
        if not sender_id:
            return None, "missing X-Antigua-Node-ID header"
        peer = self.mesh_peers.get(sender_id) if self.mesh_peers else None
        if not peer:
            return None, f"unrecognized peer node ID: {sender_id}"

        valid, reason = MeshAuth.verify_request(
            peer.shared_secret,
            "POST",
            urlparse(self.path).path,
            body,
            dict(self.headers),
        )
        if not valid:
            return None, f"authentication failed: {reason}"
        return peer, "ok"

    def _handle_mesh_info(self):
        info = self.mesh_identity.info if self.mesh_identity else None
        if not info:
            self._json(500, {"error": "mesh identity not configured"})
            return
        self._json(200, {
            "node_id": info.node_id,
            "name": info.name,
            "platform": info.platform,
            "version": info.version,
            "capabilities": info.capabilities,
            "peers_count": len(self.mesh_peers.get_all()) if self.mesh_peers else 0,
        })

    def _handle_mesh_pair_initiate(self):
        data = self._read_mesh_json()
        if data is None:
            self._json(400, {"error": "invalid JSON"})
            return

        initiator_id = data.get("node_id")
        initiator_name = data.get("name", "Unknown Node")
        initiator_endpoint = data.get("endpoint", "")
        if not initiator_id:
            self._json(400, {"error": "node_id required to initiate pairing"})
            return

        if not self.mesh_auth:
            self._json(500, {"error": "mesh auth not configured"})
            return

        session_id, pin = self.mesh_auth.create_pairing_session(
            initiator_node_id=initiator_id,
            initiator_name=initiator_name,
            initiator_endpoint=initiator_endpoint,
        )
        log.info("Pairing initiated from %s (%s). PIN: %s", initiator_name, initiator_id, pin)
        self._json(200, {
            "session_id": session_id,
            "pin": pin,
            "expires_in": 300,
        })

    def _handle_mesh_pair_confirm(self):
        data = self._read_mesh_json()
        if data is None:
            self._json(400, {"error": "invalid JSON"})
            return

        session_id = data.get("session_id")
        pin = data.get("pin")
        if not session_id or not pin:
            self._json(400, {"error": "session_id and pin required"})
            return

        valid, session, reason = self.mesh_auth.verify_pin(session_id, pin)
        if not valid or not session:
            self._json(401, {"error": f"pairing failed: {reason}"})
            return

        # Generate a high-entropy pairwise shared secret
        shared_secret = secrets.token_hex(32)
        peer = Peer(
            node_id=session.initiator_node_id,
            name=session.initiator_name,
            endpoint=session.initiator_endpoint,
            shared_secret=shared_secret,
            status="online",
            last_seen=time.time(),
        )
        if self.mesh_peers:
            self.mesh_peers.add_or_update(peer)

        self._json(200, {
            "ok": True,
            "node_id": self.mesh_identity.node_id if self.mesh_identity else "",
            "name": self.mesh_identity.name if self.mesh_identity else "",
            "shared_secret": shared_secret,
            "capabilities": self.mesh_identity.capabilities if self.mesh_identity else [],
        })

    def _handle_mesh_peers_list(self):
        if not self.mesh_peers:
            self._json(200, {"peers": []})
            return
        self._json(200, {"peers": self.mesh_peers.list_all(safe=True)})

    def _handle_mesh_peer_delete(self, node_id: str):
        if not self.mesh_peers:
            self._json(404, {"error": "no peers registered"})
            return
        removed = self.mesh_peers.remove(node_id)
        if removed:
            self._json(200, {"ok": True, "removed": node_id})
        else:
            self._json(404, {"error": f"peer {node_id} not found"})

    def _handle_mesh_sync(self):
        body = self._read_mesh_body()
        peer, reason = self._authenticate_peer(body)
        if not peer:
            self._json(401, {"error": reason})
            return

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            self._json(400, {"error": "invalid sync JSON"})
            return

        if not self.mesh_sync:
            self._json(500, {"error": "mesh sync engine not configured"})
            return

        try:
            summary = self.mesh_sync.apply_payload(payload)
            if payload.get("hashes"):
                peer.last_hashes = dict(payload["hashes"])

            # Export local delta only for what peer differs on
            local_payload = self.mesh_sync.export_payload(peer_hashes=payload.get("hashes"))

            peer.last_seen = time.time()
            peer.last_sync = time.time()
            peer.status = "online"
            if self.mesh_peers:
                self.mesh_peers.save()

            self._json(200, {
                "ok": True,
                "summary": summary,
                "local_payload": local_payload,
                "hashes": self.mesh_sync.compute_hashes(),
            })
        except Exception as e:
            log.exception("Mesh sync failed: %s", e)
            self._json(500, {"error": f"sync error: {e}"})

    def _handle_mesh_delegate(self):
        body = self._read_mesh_body()
        peer, reason = self._authenticate_peer(body)
        if not peer:
            self._json(401, {"error": reason})
            return

        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            self._json(400, {"error": "invalid delegate JSON"})
            return

        if not self.mesh_router:
            self._json(500, {"error": "mesh skills router not configured"})
            return

        peer.last_seen = time.time()
        peer.status = "online"
        result = self.mesh_router.handle_incoming_delegation(data)
        self._json(200, result)
