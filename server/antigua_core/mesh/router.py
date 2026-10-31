"""Distributed skill discovery and remote intent delegation.

Enables heterogeneous nodes (e.g. Antigua-Mac and Antigua-Home) to enhance each
other by delegating skill requests to whichever paired node owns the physical
hardware or platform capability.
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Callable

from .auth import MeshAuth
from .identity import MeshIdentity
from .peers import Peer, PeerRegistry

log = logging.getLogger("antigua_core.mesh.router")

# Maps classified intent routes to the hardware/platform capability required.
ROUTE_CAPABILITY_MAP: dict[str, str] = {
    # Home Automation / IoT
    "govee": "govee_lights",
    "govee_power": "govee_lights",
    "govee_brightness": "govee_lights",
    "govee_color": "govee_lights",
    "govee_kelvin": "govee_lights",
    "tv": "living_room_tv",
    "climate": "climate_ac",
    "security": "security_cams",
    # macOS-specific
    "macos_shortcut": "macos_shortcuts",
    "macos_reminder": "macos_reminders",
    "macos_calendar": "macos_calendar",
    "macos_system": "macos_system",
}

FRIENDLY_CAPABILITY_NAMES: dict[str, str] = {
    "govee_lights": "Living room lights",
    "living_room_tv": "The TV",
    "climate_ac": "The AC and thermostat",
    "security_cams": "Security cameras",
    "macos_shortcuts": "Mac Shortcuts",
    "macos_reminders": "Apple Reminders",
    "macos_calendar": "Apple Calendar",
}


class MeshSkillsRouter:
    """Routes voice turns to local skills or delegates them across the mesh."""

    def __init__(
        self,
        identity: MeshIdentity,
        peers: PeerRegistry,
        local_dispatcher: Callable[[str, str | None, str | None], dict] | None = None,
    ):
        self.identity = identity
        self.peers = peers
        self.local_dispatcher = local_dispatcher

    def required_capability(self, route: str) -> str | None:
        """Returns the capability needed for this route, or None if universal/local."""
        return ROUTE_CAPABILITY_MAP.get(route)

    def can_handle_locally(self, route: str) -> bool:
        """True if this node has the capability to run the route locally."""
        cap = self.required_capability(route)
        if cap is None:
            return True  # Universal skill (calc, weather, timers, LLM chat, etc.)
        return cap in self.identity.capabilities

    def find_peer_for_route(self, route: str) -> tuple[Peer | None, str | None]:
        """Finds an online peer capable of handling this route.

        Returns (peer, capability_needed).
        """
        cap = self.required_capability(route)
        if not cap:
            return None, None
        peers = self.peers.find_by_capability(cap)
        if not peers:
            return None, cap
        # Return online peer first
        for p in peers:
            if p.status == "online":
                return p, cap
        return peers[0], cap

    def should_delegate(self, route: str) -> bool:
        """Determines if a route should be delegated over the mesh."""
        if self.can_handle_locally(route):
            return False
        peer, _ = self.find_peer_for_route(route)
        return peer is not None

    def delegate(
        self,
        peer: Peer,
        transcript: str,
        route: str = "",
        speaker: str | None = None,
        conversation_id: str | None = None,
        timeout: float = 5.0,
    ) -> dict:
        """Sends a delegation request to a remote peer and returns the response."""
        url = f"{peer.endpoint}/mesh/delegate"
        payload = {
            "transcript": transcript,
            "route": route,
            "speaker": speaker,
            "conversation_id": conversation_id or "",
            "source_node": self.identity.node_id,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = MeshAuth.sign_request(
            peer.shared_secret,
            self.identity.node_id,
            "POST",
            "/mesh/delegate",
            body,
        )
        headers["Content-Type"] = "application/json"

        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                peer.status = "online"
                return data
        except Exception as e:
            log.warning("Delegation to %s (%s) failed: %s", peer.name, url, e)
            peer.status = "offline"
            cap = self.required_capability(route) if route else None
            cap_name = FRIENDLY_CAPABILITY_NAMES.get(cap or "", "That device")
            return {
                "handled": True,
                "reply": f"{cap_name} is managed by {peer.name}, which is currently unreachable.",
                "delegated": False,
                "error": str(e),
            }

    def handle_incoming_delegation(self, data: dict) -> dict:
        """Processes a delegation request received from a remote peer."""
        transcript = data.get("transcript", "")
        speaker = data.get("speaker")
        conv_id = data.get("conversation_id")

        if not transcript:
            return {"handled": False, "error": "empty transcript"}

        if self.local_dispatcher:
            try:
                res = self.local_dispatcher(transcript, speaker, conv_id)
                return {
                    "handled": True,
                    "reply": res.get("response") or res.get("reply", ""),
                    "action": res.get("action"),
                    "delegated": True,
                }
            except Exception as e:
                log.error("Local execution of delegated command failed: %s", e)
                return {"handled": False, "error": str(e)}

        return {"handled": False, "error": "no local dispatcher configured"}
