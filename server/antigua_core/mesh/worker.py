"""Background mesh worker: adaptive heartbeats, exponential backoff, and debounced delta sync."""

import logging
import threading
import time

from .client import MeshClient
from .identity import MeshIdentity
from .peers import PeerRegistry
from .sync import MeshSync

log = logging.getLogger("antigua_core.mesh.worker")

DEFAULT_HEARTBEAT_INTERVAL = 60.0  # seconds when healthy
INITIAL_BACKOFF = 10.0              # seconds on first failure
MAX_BACKOFF = 300.0                 # 5 minutes max backoff for offline peers
DEBOUNCE_PUSH_DELAY = 2.0           # seconds to wait before pushing local mutations


class MeshWorker:
    """Autonomous background daemon for health monitoring and state synchronization."""

    def __init__(
        self,
        identity: MeshIdentity,
        peers: PeerRegistry,
        sync: MeshSync,
        client: MeshClient | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ):
        self.identity = identity
        self.peers = peers
        self.sync = sync
        self.client = client or MeshClient(identity, peers, sync)
        self.heartbeat_interval = heartbeat_interval

        self._running = False
        self._thread: threading.Thread | None = None
        self._wake_event = threading.Event()
        self._lock = threading.Lock()

        # Track per-peer backoff delays: {node_id: current_backoff_seconds}
        self._backoff: dict[str, float] = {}
        self._next_check: dict[str, float] = {}

        # Debounced mutation push tracking
        self._pending_push = False
        self._push_timer: threading.Timer | None = None

    def start(self):
        """Starts the background worker thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name="antigua-mesh-worker",
            )
            self._thread.start()
            log.info("MeshWorker: started background worker")

    def stop(self):
        """Signals the worker to terminate gracefully."""
        with self._lock:
            self._running = False
            if self._push_timer:
                self._push_timer.cancel()
            self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            log.info("MeshWorker: stopped background worker")

    def notify_mutation(self):
        """Notifies the worker that local state (memory, preferences, lists) has changed.

        Debounces the push by DEBOUNCE_PUSH_DELAY seconds so rapid updates
        are coalesced into a single network transmission.
        """
        with self._lock:
            if not self._running:
                return
            if self._push_timer:
                self._push_timer.cancel()

            self._push_timer = threading.Timer(DEBOUNCE_PUSH_DELAY, self._trigger_push)
            self._push_timer.daemon = True
            self._push_timer.start()

    def _trigger_push(self):
        with self._lock:
            self._pending_push = True
            self._wake_event.set()

    def _run_loop(self):
        while self._running:
            now = time.time()
            is_push = False

            with self._lock:
                if self._pending_push:
                    is_push = True
                    self._pending_push = False
                self._wake_event.clear()

            peers_to_check = []
            for peer in self.peers.get_all():
                due = self._next_check.get(peer.node_id, 0.0)
                if is_push or now >= due:
                    peers_to_check.append(peer)

            for peer in peers_to_check:
                if not self._running:
                    break
                self._process_peer(peer, force_sync=is_push)

            # Calculate sleep duration until next due peer or max 10s
            now = time.time()
            next_due_times = [
                self._next_check.get(p.node_id, now + self.heartbeat_interval)
                for p in self.peers.get_all()
            ]
            sleep_time = min(next_due_times) - now if next_due_times else self.heartbeat_interval
            sleep_time = max(0.5, min(sleep_time, 15.0))

            self._wake_event.wait(timeout=sleep_time)

    def _process_peer(self, peer, force_sync: bool = False):
        node_id = peer.node_id
        now = time.time()

        try:
            # Ping peer to check liveliness
            is_alive = self.client.ping(peer)
            if not is_alive:
                # Exponential backoff on failure
                cur_backoff = self._backoff.get(node_id, INITIAL_BACKOFF)
                next_backoff = min(cur_backoff * 1.5, MAX_BACKOFF)
                self._backoff[node_id] = next_backoff
                self._next_check[node_id] = now + next_backoff
                return

            # Peer is online: reset backoff
            self._backoff[node_id] = INITIAL_BACKOFF
            self._next_check[node_id] = now + self.heartbeat_interval

            # If forced push or peer hasn't synced recently, perform delta sync
            should_sync = force_sync or (now - peer.last_sync > self.heartbeat_interval)
            if should_sync:
                ok, summary = self.client.sync_peer(peer)
                if ok:
                    log.debug("MeshWorker: synced with %s: %s", peer.name, summary)
        except Exception as e:
            log.warning("MeshWorker error processing %s: %s", peer.name, e)
            self._backoff[node_id] = min(self._backoff.get(node_id, INITIAL_BACKOFF) * 2, MAX_BACKOFF)
            self._next_check[node_id] = now + self._backoff[node_id]
