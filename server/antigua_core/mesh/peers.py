"""Paired peers registry and persistence."""

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock

import yaml

from .. import settings

log = logging.getLogger("antigua_core.mesh.peers")


@dataclass
class Peer:
    node_id: str
    name: str
    endpoint: str
    shared_secret: str
    platform: str = "generic"
    capabilities: list[str] = field(default_factory=list)
    status: str = "offline"
    last_seen: float = 0.0
    last_sync: float = 0.0
    last_hashes: dict[str, str] = field(default_factory=dict)

    def to_dict(self, safe: bool = False) -> dict:
        d = asdict(self)
        if safe:
            d.pop("shared_secret", None)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Peer":
        return cls(
            node_id=str(data.get("node_id", "")),
            name=str(data.get("name", "Peer Node")),
            endpoint=str(data.get("endpoint", "")).rstrip("/"),
            shared_secret=str(data.get("shared_secret", "")),
            platform=str(data.get("platform", "generic")),
            capabilities=list(data.get("capabilities", [])),
            status=str(data.get("status", "offline")),
            last_seen=float(data.get("last_seen", 0.0)),
            last_sync=float(data.get("last_sync", 0.0)),
            last_hashes=dict(data.get("last_hashes", {})),
        )

    def has_capability(self, cap: str) -> bool:
        return cap in self.capabilities


class PeerRegistry:
    """Thread-safe registry for paired Antigua peers."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or (settings.DATA_DIR / "mesh_peers.yaml"))
        self._lock = RLock()
        self._peers: dict[str, Peer] = {}
        self.load()

    def load(self):
        with self._lock:
            self._peers.clear()
            if not self.path.exists():
                return
            try:
                raw = yaml.safe_load(self.path.read_text()) or {}
                if isinstance(raw, dict):
                    peers_list = raw.get("peers", [])
                    if isinstance(peers_list, list):
                        for p_data in peers_list:
                            if isinstance(p_data, dict) and "node_id" in p_data:
                                peer = Peer.from_dict(p_data)
                                self._peers[peer.node_id] = peer
                log.info("PeerRegistry: loaded %d paired peers from %s", len(self._peers), self.path)
            except Exception as e:
                log.warning("PeerRegistry: load failed (%s), starting fresh", e)

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": "1.0",
                "peers": [p.to_dict(safe=False) for p in self._peers.values()],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(yaml.safe_dump(data, sort_keys=False))
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)

    def add_or_update(self, peer: Peer):
        with self._lock:
            self._peers[peer.node_id] = peer
            self.save()
            log.info("PeerRegistry: saved peer %s (%s) at %s", peer.name, peer.node_id, peer.endpoint)

    def get(self, node_id: str) -> Peer | None:
        with self._lock:
            return self._peers.get(node_id)

    def remove(self, node_id: str) -> bool:
        with self._lock:
            if node_id in self._peers:
                p = self._peers.pop(node_id)
                self.save()
                log.info("PeerRegistry: removed peer %s (%s)", p.name, node_id)
                return True
            return False

    def list_all(self, safe: bool = False) -> list[dict]:
        with self._lock:
            return [p.to_dict(safe=safe) for p in self._peers.values()]

    def get_all(self) -> list[Peer]:
        with self._lock:
            return list(self._peers.values())

    def find_by_capability(self, cap: str) -> list[Peer]:
        with self._lock:
            return [p for p in self._peers.values() if p.has_capability(cap)]

    def update_status(self, node_id: str, status: str, capabilities: list[str] | None = None):
        with self._lock:
            peer = self._peers.get(node_id)
            if peer:
                peer.status = status
                peer.last_seen = time.time()
                if capabilities is not None:
                    peer.capabilities = list(capabilities)
                self.save()

    def update_sync_time(self, node_id: str):
        with self._lock:
            peer = self._peers.get(node_id)
            if peer:
                peer.last_sync = time.time()
                self.save()
