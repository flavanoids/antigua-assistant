"""Client for peer-to-peer mesh operations: discovery, pairing, syncing, and delegation."""

import json
import logging
import time
import urllib.error
import urllib.request

from .auth import MeshAuth
from .identity import MeshIdentity
from .peers import Peer, PeerRegistry
from .sync import MeshSync

log = logging.getLogger("antigua_core.mesh.client")


class MeshClient:
    """Outbound client for interacting with paired Antigua peers."""

    def __init__(
        self,
        identity: MeshIdentity,
        peers: PeerRegistry,
        sync: MeshSync,
        timeout: float = 6.0,
    ):
        self.identity = identity
        self.peers = peers
        self.sync = sync
        self.timeout = timeout

    def ping(self, peer: Peer) -> bool:
        """Pings a peer via /mesh/info and updates its status and capabilities."""
        url = f"{peer.endpoint}/mesh/info"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    caps = data.get("capabilities", [])
                    self.peers.update_status(peer.node_id, "online", capabilities=caps)
                    return True
        except Exception as e:
            log.debug("Ping to %s (%s) failed: %s", peer.name, peer.endpoint, e)
            self.peers.update_status(peer.node_id, "offline")
        return False

    def pair_with_remote_node(
        self,
        remote_url: str,
        my_endpoint: str,
        pin_callback=None,
    ) -> tuple[bool, Peer | None, str]:
        """Executes pairing handshake with a remote Antigua node.

        If pin_callback is provided, it is invoked with the PIN so the user
        can see or confirm it; otherwise prompts on console.
        """
        remote_url = remote_url.rstrip("/")
        init_url = f"{remote_url}/mesh/pair/initiate"

        # 1. Initiate pairing
        init_payload = {
            "node_id": self.identity.node_id,
            "name": self.identity.name,
            "endpoint": my_endpoint,
        }
        try:
            init_body = json.dumps(init_payload).encode("utf-8")
            req = urllib.request.Request(
                init_url,
                data=init_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                init_data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return False, None, f"failed to connect to {remote_url}: {e}"

        session_id = init_data.get("session_id")
        pin = init_data.get("pin")
        if not session_id or not pin:
            return False, None, "remote node did not provide a valid pairing session"

        if pin_callback:
            pin_callback(pin)

        # 2. Confirm pairing with the PIN
        confirm_url = f"{remote_url}/mesh/pair/confirm"
        confirm_payload = {
            "session_id": session_id,
            "pin": pin,
        }
        try:
            confirm_body = json.dumps(confirm_payload).encode("utf-8")
            req = urllib.request.Request(
                confirm_url,
                data=confirm_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                confirm_data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return False, None, f"confirmation failed on {remote_url}: {e}"

        remote_node_id = confirm_data.get("node_id")
        remote_name = confirm_data.get("name", "Remote Antigua")
        shared_secret = confirm_data.get("shared_secret")
        remote_caps = confirm_data.get("capabilities", [])

        if not remote_node_id or not shared_secret:
            return False, None, "invalid credentials returned during pairing confirmation"

        peer = Peer(
            node_id=remote_node_id,
            name=remote_name,
            endpoint=remote_url,
            shared_secret=shared_secret,
            capabilities=remote_caps,
            status="online",
            last_seen=time.time(),
        )
        self.peers.add_or_update(peer)
        log.info("Pairing complete with %s (%s)", remote_name, remote_node_id)
        return True, peer, "ok"

    def sync_peer(self, peer: Peer) -> tuple[bool, dict]:
        """Performs bidirectional delta sync with a specific peer."""
        url = f"{peer.endpoint}/mesh/sync"
        local_payload = self.sync.export_payload(peer_hashes=peer.last_hashes)
        body = json.dumps(local_payload).encode("utf-8")

        headers = MeshAuth.sign_request(
            peer.shared_secret,
            self.identity.node_id,
            "POST",
            "/mesh/sync",
            body,
        )
        headers["Content-Type"] = "application/json"

        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                peer_payload = data.get("local_payload")
                if peer_payload:
                    local_summary = self.sync.apply_payload(peer_payload)
                    if peer_payload.get("hashes"):
                        peer.last_hashes = dict(peer_payload["hashes"])
                else:
                    local_summary = {}

                if data.get("hashes"):
                    peer.last_hashes = dict(data["hashes"])

                self.peers.update_status(peer.node_id, "online")
                self.peers.update_sync_time(peer.node_id)
                self.peers.save()
                return True, {
                    "remote_summary": data.get("summary", {}),
                    "local_summary": local_summary,
                }
        except Exception as e:
            log.warning("Sync with %s (%s) failed: %s", peer.name, url, e)
            self.peers.update_status(peer.node_id, "offline")
            return False, {"error": str(e)}

    def sync_all(self) -> dict:
        """Runs synchronization with all registered peers."""
        results = {}
        for peer in self.peers.get_all():
            success, summary = self.sync_peer(peer)
            results[peer.node_id] = {
                "name": peer.name,
                "success": success,
                "summary": summary,
            }
        return results
