"""Zero-configuration local network discovery for Antigua nodes via lightweight UDP broadcast."""

import json
import logging
import socket
import threading
import time
from typing import Callable

from .identity import MeshIdentity

log = logging.getLogger("antigua_core.mesh.discovery")

DEFAULT_DISCOVERY_PORT = 9394
MAGIC_HEADER = "ANTIGUA_BEACON_V1"


class MeshDiscovery:
    """Manages zero-conf peer discovery over the local subnet without external dependencies."""

    def __init__(
        self,
        identity: MeshIdentity,
        http_port: int = 9393,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
    ):
        self.identity = identity
        self.http_port = http_port
        self.discovery_port = discovery_port
        self._running = False
        self._threads: list[threading.Thread] = []
        self._discovered: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _beacon_payload(self) -> bytes:
        data = {
            "magic": MAGIC_HEADER,
            "node_id": self.identity.node_id,
            "name": self.identity.name,
            "platform": self.identity.info.platform,
            "version": self.identity.info.version,
            "http_port": self.http_port,
            "capabilities": self.identity.capabilities,
            "ts": time.time(),
        }
        return json.dumps(data).encode("utf-8")

    def broadcast_beacon(self, sock: socket.socket | None = None):
        """Sends a single discovery beacon to the local broadcast address."""
        close_sock = False
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            close_sock = True
        try:
            sock.sendto(self._beacon_payload(), ("<broadcast>", self.discovery_port))
        except OSError as e:
            log.debug("Broadcast beacon send error: %s", e)
        finally:
            if close_sock:
                sock.close()

    def start_broadcaster(self, interval: float = 30.0):
        """Runs periodic beacon broadcaster in the background."""
        if self._running:
            return
        self._running = True

        def _broadcast_loop():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                while self._running:
                    self.broadcast_beacon(sock)
                    time.sleep(interval)
            finally:
                sock.close()

        t = threading.Thread(target=_broadcast_loop, daemon=True, name="mesh-broadcaster")
        self._threads.append(t)
        t.start()

    def start_listener(self, on_discovered: Callable[[dict], None] | None = None):
        """Listens for beacons and tracks discovered nodes."""
        if self._running:
            return
        self._running = True

        def _listen_loop():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", self.discovery_port))
            except OSError as e:
                log.warning("Discovery listener bind failed on port %d: %s", self.discovery_port, e)
                sock.close()
                return

            try:
                while self._running:
                    try:
                        sock.settimeout(2.0)
                        data, addr = sock.recvfrom(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        break

                    try:
                        msg = json.loads(data.decode("utf-8"))
                        if msg.get("magic") != MAGIC_HEADER:
                            continue
                        node_id = msg.get("node_id")
                        if not node_id or node_id == self.identity.node_id:
                            continue  # Ignore self

                        ip = addr[0]
                        port = msg.get("http_port", 9393)
                        endpoint = f"http://{ip}:{port}"
                        info = {
                            "node_id": node_id,
                            "name": msg.get("name", "Unknown Node"),
                            "platform": msg.get("platform", "generic"),
                            "endpoint": endpoint,
                            "capabilities": msg.get("capabilities", []),
                            "last_seen": time.time(),
                        }
                        with self._lock:
                            is_new = node_id not in self._discovered
                            self._discovered[node_id] = info

                        if is_new and on_discovered:
                            on_discovered(info)
                    except Exception as e:
                        log.debug("Discovery message parse error: %s", e)
            finally:
                sock.close()

        t = threading.Thread(target=_listen_loop, daemon=True, name="mesh-listener")
        self._threads.append(t)
        t.start()

    def stop(self):
        self._running = False

    def get_discovered(self) -> list[dict]:
        with self._lock:
            # Filter entries seen within the last 120 seconds
            cutoff = time.time() - 120
            return [
                v for v in self._discovered.values()
                if v.get("last_seen", 0) > cutoff
            ]

    def discover_once(self, timeout: float = 2.0) -> list[dict]:
        """Sends a burst of beacons and collects responding nodes on the LAN."""
        discovered = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            # Bind to listen for replies or incoming beacons
            try:
                sock.bind(("", 0))  # Ephemeral port
            except OSError:
                pass

            # Send beacon to discovery port
            sock.sendto(self._beacon_payload(), ("<broadcast>", self.discovery_port))
            start = time.time()
            sock.settimeout(0.5)

            while time.time() - start < timeout:
                try:
                    data, addr = sock.recvfrom(4096)
                    msg = json.loads(data.decode("utf-8"))
                    if msg.get("magic") == MAGIC_HEADER and msg.get("node_id") != self.identity.node_id:
                        ip = addr[0]
                        port = msg.get("http_port", 9393)
                        discovered[msg["node_id"]] = {
                            "node_id": msg["node_id"],
                            "name": msg.get("name", "Antigua Node"),
                            "platform": msg.get("platform", "generic"),
                            "endpoint": f"http://{ip}:{port}",
                            "capabilities": msg.get("capabilities", []),
                        }
                except socket.timeout:
                    # Retransmit once midway
                    if time.time() - start < timeout / 2:
                        try:
                            sock.sendto(self._beacon_payload(), ("<broadcast>", self.discovery_port))
                        except OSError:
                            pass
                except Exception:
                    break
        finally:
            sock.close()

        return list(discovered.values())
