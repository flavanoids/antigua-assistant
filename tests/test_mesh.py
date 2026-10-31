"""Comprehensive unit tests for Antigua Mesh: identity, auth, peers, sync, discovery, and routing."""

import io
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core.household_memory import HouseholdMemory
from antigua_core.mesh import mac_skills
from antigua_core.mesh.auth import MeshAuth
from antigua_core.mesh.discovery import MeshDiscovery
from antigua_core.mesh.http_api import MeshRoutes
from antigua_core.mesh.identity import MeshIdentity
from antigua_core.mesh.peers import Peer, PeerRegistry
from antigua_core.mesh.router import MeshSkillsRouter
from antigua_core.mesh.sync import MeshSync
from antigua_core.mesh.worker import MeshWorker
from antigua_core.stores import ListStore, MemoryStore


def test_identity_creation_and_load(tmp_path):
    node_file = tmp_path / "mesh_node.yaml"
    ident = MeshIdentity(path=node_file, name="Test Node")
    info = ident.info

    assert info.node_id.startswith("antigua-")
    assert info.name == "Test Node"
    assert "chat" in info.capabilities
    assert "memory" in info.capabilities
    assert ident.node_secret != ""
    assert node_file.exists()

    # Re-load existing
    ident2 = MeshIdentity(path=node_file)
    assert ident2.node_id == ident.node_id
    assert ident2.name == "Test Node"
    assert ident2.node_secret == ident.node_secret


def test_auth_request_signing_and_verification():
    secret = "test-shared-secret-key-12345"
    node_id = "antigua-mac-test"
    method = "POST"
    path = "/mesh/sync"
    body = b'{"hello": "world"}'

    headers = MeshAuth.sign_request(secret, node_id, method, path, body)
    assert headers["X-Antigua-Node-ID"] == node_id
    assert "X-Antigua-Timestamp" in headers
    assert "X-Antigua-Signature" in headers

    # Valid verification
    valid, reason = MeshAuth.verify_request(secret, method, path, body, headers)
    assert valid is True
    assert reason == "ok"

    # Replay attack prevention: exact duplicate signature must be rejected
    valid_replay, reason_replay = MeshAuth.verify_request(secret, method, path, body, headers)
    assert valid_replay is False
    assert "replay detected" in reason_replay

    # Tampered body with fresh signature
    tampered_body = b'{"hello": "tampered"}'
    fresh_headers = MeshAuth.sign_request(secret, node_id, method, path, body)
    valid, reason = MeshAuth.verify_request(secret, method, path, tampered_body, fresh_headers)
    assert valid is False
    assert "mismatch" in reason

    # Wrong secret
    valid, reason = MeshAuth.verify_request("wrong-secret", method, path, body, fresh_headers)
    assert valid is False

    # Timestamp drift / old request
    old_headers = MeshAuth.sign_request(secret, node_id, method, path, body)
    old_headers["X-Antigua-Timestamp"] = str(int(time.time()) - 400)
    old_headers["X-Antigua-Signature"] = MeshAuth.compute_signature(
        secret, method, path, old_headers["X-Antigua-Timestamp"], MeshAuth.hash_body(body),
        nonce=old_headers.get("X-Antigua-Nonce", "")
    )
    valid, reason = MeshAuth.verify_request(secret, method, path, body, old_headers)
    assert valid is False
    assert "drift" in reason


def test_pairing_challenge_and_pin_flow():
    auth = MeshAuth()
    session_id, pin = auth.create_pairing_session(
        initiator_node_id="peer-123",
        initiator_name="MacBook",
        initiator_endpoint="http://192.168.1.50:9393",
    )
    assert len(pin) == 6
    assert pin.isdigit()

    # Wrong PIN attempt
    valid, session, reason = auth.verify_pin(session_id, "000000" if pin != "000000" else "111111")
    assert valid is False
    assert "invalid PIN" in reason

    # Correct PIN
    valid, session, reason = auth.verify_pin(session_id, pin)
    assert valid is True
    assert session.initiator_node_id == "peer-123"
    assert session.initiator_name == "MacBook"

    # Session consumed — cannot be reused
    valid2, _, _ = auth.verify_pin(session_id, pin)
    assert valid2 is False


def test_peer_registry(tmp_path):
    peers_file = tmp_path / "mesh_peers.yaml"
    reg = PeerRegistry(path=peers_file)

    p1 = Peer(
        node_id="node-home",
        name="Antigua Home",
        endpoint="http://10.77.0.1:9393",
        shared_secret="secret1",
        platform="linux",
        capabilities=["govee_lights", "living_room_tv"],
        status="online",
    )
    p2 = Peer(
        node_id="node-mac",
        name="Antigua Mac",
        endpoint="http://192.168.1.100:9393",
        shared_secret="secret2",
        platform="mac",
        capabilities=["macos_shortcuts"],
        status="online",
    )
    reg.add_or_update(p1)
    reg.add_or_update(p2)

    assert len(reg.get_all()) == 2
    assert reg.get("node-home").name == "Antigua Home"

    # Capability search
    govee_peers = reg.find_by_capability("govee_lights")
    assert len(govee_peers) == 1
    assert govee_peers[0].node_id == "node-home"

    mac_peers = reg.find_by_capability("macos_shortcuts")
    assert len(mac_peers) == 1
    assert mac_peers[0].node_id == "node-mac"

    # Persistence check
    reg2 = PeerRegistry(path=peers_file)
    assert len(reg2.get_all()) == 2

    # Remove peer
    assert reg2.remove("node-mac") is True
    assert len(reg2.get_all()) == 1


def test_mesh_sync_memories_and_persona(tmp_path):
    # Setup Node A
    hm_a_path = tmp_path / "hm_a.yaml"
    hm_a = HouseholdMemory(path=hm_a_path)
    mem_a_path = tmp_path / "mem_a.json"
    mem_a = MemoryStore(path=mem_a_path)
    list_a_path = tmp_path / "list_a.json"
    list_a = ListStore(path=list_a_path)
    sync_a = MeshSync(hm=hm_a, memory_store=mem_a, list_store=list_a, node_id="node-a")

    # Seed Node A state
    hm_a.data()["facts"].append({"text": "The garage code is 4321"})
    hm_a.data()["preferences"]["sass"] = 1
    hm_a.data()["preferences"]["reply_language"] = "en"
    hm_a.data()["rules"].append("Always confirm before turning off the oven")
    hm_a._save()
    sync_a._pref_timestamps["sass"] = 100.0
    sync_a._pref_timestamps["reply_language"] = 100.0

    mem_a.replace([{"id": "m1", "text": "I parked on level 3", "timestamp": "2026-10-30T10:00:00"}])
    list_a.add("groceries", ["oat milk"])

    # Setup Node B (Mac)
    hm_b_path = tmp_path / "hm_b.yaml"
    hm_b = HouseholdMemory(path=hm_b_path)
    mem_b_path = tmp_path / "mem_b.json"
    mem_b = MemoryStore(path=mem_b_path)
    list_b_path = tmp_path / "list_b.json"
    list_b = ListStore(path=list_b_path)
    sync_b = MeshSync(hm=hm_b, memory_store=mem_b, list_store=list_b, node_id="node-b")

    # Node B has a newer sass preference (personality carry-over) and a new fact
    hm_b.data()["facts"].append({"text": "Sam prefers dark roast coffee"})
    hm_b.data()["preferences"]["sass"] = 3  # more sassy!
    hm_b.data()["preferences"]["call"] = {"Sam": "Captain"}
    hm_b._save()
    sync_b._pref_timestamps["sass"] = 200.0  # newer timestamp
    sync_b._pref_timestamps["call"] = 200.0

    list_b.add("groceries", ["sourdough bread"])
    list_b.add("hardware", ["screws"])

    # Node B exports its payload
    payload_b = sync_b.export_payload()

    # Node A applies payload from Node B
    summary = sync_a.apply_payload(payload_b)

    assert summary["facts_added"] == 1
    assert "sass" in summary["prefs_updated"]
    assert "call.Sam" in summary["prefs_updated"]
    assert "groceries" in summary["lists_updated"]
    assert "hardware" in summary["lists_updated"]

    # Verify Node A updated correctly
    a_data = hm_a.data()
    # Facts merged
    fact_texts = [f["text"] for f in a_data["facts"]]
    assert "The garage code is 4321" in fact_texts
    assert "Sam prefers dark roast coffee" in fact_texts

    # Personality updated with newer value from Mac
    assert a_data["preferences"]["sass"] == 3
    assert a_data["preferences"]["call"]["Sam"] == "Captain"
    # Unchanged preference kept
    assert a_data["preferences"]["reply_language"] == "en"

    # Lists merged
    all_lists = list_a.all_lists()
    assert "oat milk" in all_lists["groceries"]
    assert "sourdough bread" in all_lists["groceries"]
    assert "screws" in all_lists["hardware"]


def test_delta_sync_hashing_and_efficiency(tmp_path):
    hm = HouseholdMemory(path=tmp_path / "hm_delta.yaml")
    mem = MemoryStore(path=tmp_path / "mem_delta.json")
    lst = ListStore(path=tmp_path / "list_delta.json")
    sync = MeshSync(hm=hm, memory_store=mem, list_store=lst, node_id="node-delta")

    # 1. Compute initial hashes
    h1 = sync.compute_hashes()
    assert "facts" in h1
    assert "preferences" in h1
    assert "memories" in h1
    assert "lists" in h1

    # 2. When peer has exact same hashes, delta payload should indicate no changes
    unchanged_payload = sync.export_payload(peer_hashes=h1)
    assert unchanged_payload["changed"] is False
    assert "household_memory" not in unchanged_payload
    assert "memories" not in unchanged_payload

    # Applying an unchanged payload should be a no-op
    res = sync.apply_payload(unchanged_payload)
    assert res["changed"] is False
    assert res["facts_added"] == 0

    # 3. Modify only preferences
    hm.data()["preferences"]["sass"] = 2
    hm._save()
    sync._pref_timestamps["sass"] = 300.0

    # Delta payload should ONLY contain household_memory with preferences, NOT lists or memories
    delta_payload = sync.export_payload(peer_hashes=h1)
    assert delta_payload["changed"] is True
    assert "household_memory" in delta_payload
    assert "preferences" in delta_payload["household_memory"]
    assert "facts" not in delta_payload["household_memory"]
    assert "memories" not in delta_payload
    assert "lists" not in delta_payload


def test_mac_skills():
    # Test shortcut parsing
    assert mac_skills.parse_mac_shortcut_request("run shortcut Morning Routine") == "Morning Routine"
    assert mac_skills.parse_mac_shortcut_request("trigger the mac shortcut Focus Mode") == "Focus Mode"
    assert mac_skills.parse_mac_shortcut_request("turn on the lights") is None

    # Test reminder parsing
    assert mac_skills.parse_mac_reminder_request("add a mac reminder to buy oat milk") == "buy oat milk"
    assert mac_skills.parse_mac_reminder_request("remind me on my Mac to submit taxes") == "submit taxes"
    assert mac_skills.parse_mac_reminder_request("what is the weather") is None

    # Test runner execution with mock
    def mock_shortcut_runner(name):
        return True, f"Executed {name}"

    ok, reply = mac_skills.run_shortcut("Morning Routine", runner=mock_shortcut_runner)
    assert ok is True
    assert reply == "Executed Morning Routine"

    def mock_reminder_runner(item):
        return True, f"Reminder created: {item}"

    ok, reply = mac_skills.add_apple_reminder("buy coffee", runner=mock_reminder_runner)
    assert ok is True
    assert reply == "Reminder created: buy coffee"


def test_skills_mesh_router():
    # Node identity is Mac (lacks govee_lights)
    ident = MeshIdentity(name="Mac Node")
    ident._info.capabilities = ["chat", "llm", "memory", "macos_shortcuts"]

    peers = PeerRegistry(path=Path("/tmp/test_mesh_peers_nonexist.yaml"))
    home_peer = Peer(
        node_id="node-home",
        name="Antigua Home",
        endpoint="http://10.77.0.1:9393",
        shared_secret="secret123",
        capabilities=["govee_lights", "living_room_tv"],
        status="online",
    )
    peers._peers["node-home"] = home_peer

    router = MeshSkillsRouter(identity=ident, peers=peers)

    # Local skill check
    assert router.can_handle_locally("chat") is True
    assert router.can_handle_locally("macos_shortcut") is True
    assert router.can_handle_locally("govee") is False

    # Should delegate check
    assert router.should_delegate("govee") is True
    assert router.should_delegate("macos_shortcut") is False

    peer, cap = router.find_peer_for_route("govee")
    assert peer == home_peer
    assert cap == "govee_lights"

    # Test incoming delegation handler
    def mock_dispatcher(transcript, speaker, conv_id):
        return {"response": f"Lights turned on for {speaker}", "action": "govee_on"}

    server_router = MeshSkillsRouter(identity=ident, peers=peers, local_dispatcher=mock_dispatcher)
    res = server_router.handle_incoming_delegation({
        "transcript": "turn on lights",
        "speaker": "Alex",
        "conversation_id": "c1",
    })
    assert res["handled"] is True
    assert res["reply"] == "Lights turned on for Alex"
    assert res["action"] == "govee_on"


class MockServerHandler(MeshRoutes):
    def __init__(self, path, method, headers=None, body=b""):
        self.path = path
        self.method = method
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.response_status = None
        self.response_data = None

    def _json(self, code, data):
        self.response_status = code
        self.response_data = data


def test_http_api_routes(tmp_path):
    ident = MeshIdentity(path=tmp_path / "node.yaml", name="Mock Host")
    peers = PeerRegistry(path=tmp_path / "peers.yaml")
    auth = MeshAuth()
    hm = HouseholdMemory(path=tmp_path / "hm_http.yaml")
    mem_store = MemoryStore(path=tmp_path / "mem_http.json")
    list_store = ListStore(path=tmp_path / "list_http.json")
    sync = MeshSync(hm=hm, memory_store=mem_store, list_store=list_store, node_id=ident.node_id)
    router = MeshSkillsRouter(identity=ident, peers=peers)

    # 1. GET /mesh/info
    h1 = MockServerHandler("/mesh/info", "GET")
    h1.mesh_identity = ident
    h1.mesh_peers = peers
    h1.mesh_router = router
    assert h1.route_mesh("GET") is True
    assert h1.response_status == 200
    assert h1.response_data["name"] == "Mock Host"

    # 2. POST /mesh/pair/initiate
    init_body = json.dumps({
        "node_id": "node-remote",
        "name": "Remote Mac",
        "endpoint": "http://192.168.1.80:9393",
    }).encode("utf-8")
    h2 = MockServerHandler("/mesh/pair/initiate", "POST", {"Content-Length": str(len(init_body))}, init_body)
    h2.mesh_identity = ident
    h2.mesh_auth = auth
    assert h2.route_mesh("POST") is True
    assert h2.response_status == 200
    session_id = h2.response_data["session_id"]
    pin = h2.response_data["pin"]
    assert len(pin) == 6

    # 3. POST /mesh/pair/confirm
    confirm_body = json.dumps({"session_id": session_id, "pin": pin}).encode("utf-8")
    h3 = MockServerHandler("/mesh/pair/confirm", "POST", {"Content-Length": str(len(confirm_body))}, confirm_body)
    h3.mesh_identity = ident
    h3.mesh_peers = peers
    h3.mesh_auth = auth
    assert h3.route_mesh("POST") is True
    assert h3.response_status == 200
    shared_secret = h3.response_data["shared_secret"]
    assert shared_secret != ""
    assert len(peers.get_all()) == 1

    # 4. Authenticated POST /mesh/sync
    sync_payload = {
        "node_id": "node-remote",
        "timestamp": time.time(),
        "household_memory": {"facts": [{"text": "Sync fact"}]},
    }
    sync_body = json.dumps(sync_payload).encode("utf-8")
    auth_headers = MeshAuth.sign_request(shared_secret, "node-remote", "POST", "/mesh/sync", sync_body)
    auth_headers["Content-Length"] = str(len(sync_body))

    h4 = MockServerHandler("/mesh/sync", "POST", auth_headers, sync_body)
    h4.mesh_identity = ident
    h4.mesh_peers = peers
    h4.mesh_sync = sync
    assert h4.route_mesh("POST") is True
    assert h4.response_status == 200
    assert h4.response_data["ok"] is True
    assert h4.response_data["summary"]["facts_added"] == 1


def test_discovery_beacon_format():
    ident = MeshIdentity(name="Discovery Test Node")
    disc = MeshDiscovery(ident, http_port=9393)
    beacon_bytes = disc._beacon_payload()
    data = json.loads(beacon_bytes.decode("utf-8"))

    assert data["magic"] == "ANTIGUA_BEACON_V1"
    assert data["node_id"] == ident.node_id
    assert data["name"] == "Discovery Test Node"
    assert data["http_port"] == 9393


def test_mesh_worker_lifecycle(tmp_path):
    ident = MeshIdentity(path=tmp_path / "w_node.yaml", name="Worker Test")
    peers = PeerRegistry(path=tmp_path / "w_peers.yaml")
    sync = MeshSync(node_id=ident.node_id)
    worker = MeshWorker(identity=ident, peers=peers, sync=sync, heartbeat_interval=0.5)

    worker.start()
    assert worker._running is True

    # Test debounced mutation notification
    worker.notify_mutation()
    assert worker._push_timer is not None

    worker.stop()
    assert worker._running is False


def run():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        print("Running test_identity_creation_and_load ...")
        test_identity_creation_and_load(tmp_path)
        print("ok test_identity_creation_and_load")

        print("Running test_auth_request_signing_and_verification ...")
        test_auth_request_signing_and_verification()
        print("ok test_auth_request_signing_and_verification")

        print("Running test_pairing_challenge_and_pin_flow ...")
        test_pairing_challenge_and_pin_flow()
        print("ok test_pairing_challenge_and_pin_flow")

        print("Running test_peer_registry ...")
        test_peer_registry(tmp_path)
        print("ok test_peer_registry")

        print("Running test_mesh_sync_memories_and_persona ...")
        test_mesh_sync_memories_and_persona(tmp_path)
        print("ok test_mesh_sync_memories_and_persona")

        print("Running test_delta_sync_hashing_and_efficiency ...")
        test_delta_sync_hashing_and_efficiency(tmp_path)
        print("ok test_delta_sync_hashing_and_efficiency")

        print("Running test_mac_skills ...")
        test_mac_skills()
        print("ok test_mac_skills")

        print("Running test_skills_mesh_router ...")
        test_skills_mesh_router()
        print("ok test_skills_mesh_router")

        print("Running test_http_api_routes ...")
        test_http_api_routes(tmp_path)
        print("ok test_http_api_routes")

        print("Running test_discovery_beacon_format ...")
        test_discovery_beacon_format()
        print("ok test_discovery_beacon_format")

        print("Running test_mesh_worker_lifecycle ...")
        test_mesh_worker_lifecycle(tmp_path)
        print("ok test_mesh_worker_lifecycle")

    print("\nmesh: all 11 passed")


if __name__ == "__main__":
    run()
