# Antigua Mesh & Federation

The **Antigua Mesh** allows autonomous Antigua nodes—such as a single-device macOS workstation running `antigua-mac` and a home server running `Antigua`—to discover each other, securely pair, synchronize state, and enhance each other's capabilities without cloud dependencies.

```mermaid
flowchart LR
    subgraph Mac ["Antigua-Mac (Local Device)"]
        direction TB
        M_Core["Antigua Core<br/>(Whisper Metal / Ollama / Kokoro)"]
        M_Memory["Household Memory & Persona<br/>household_memory.yaml"]
        M_Skills["Mac Skills<br/>Shortcuts · Apple Reminders · System"]
        M_Mesh["Mesh Node & Delta Sync"]
    end

    subgraph Home ["Antigua-Home (Home Server)"]
        direction TB
        H_Core["Antigua Core<br/>(Whisper Vulkan / Qwen / Kokoro)"]
        H_Memory["Household Memory & Persona<br/>household_memory.yaml"]
        H_Skills["Home Skills<br/>Govee · Apple TV · AC · Security"]
        H_Mesh["Mesh Node & Delta Sync"]
    end

    M_Mesh <-->|LAN / WireGuard<br/>Mutual HMAC-SHA256 Auth| H_Mesh
    M_Mesh -.->|Delta Sync Memories & Persona| H_Mesh
    M_Mesh -.->|Delegate Home Skills (Lights, TV)| H_Skills
    H_Mesh -.->|Delegate Mac Skills (Shortcuts, Reminders)| M_Skills
```

---

## Key Principles

### 1. Security & Privacy by Default
* **Zero Trust Handshake**: Pairing requires a time-limited (5-minute), 6-digit numeric PIN challenge with rate limiting (max 5 attempts before session termination).
* **Cryptographic Request Signing**: Every request is authenticated using HMAC-SHA256 over canonical request attributes (`timestamp + "\n" + nonce + "\n" + method + "\n" + path + "\n" + body_hash`).
* **Replay Protection**:
  * 5-minute maximum timestamp drift window.
  * In-memory sliding signature cache rejecting any duplicate signatures within the drift window.
* **Filesystem Hardening**: Credentials and paired peer configurations (`mesh_node.yaml`, `mesh_peers.yaml`) are created with strict `0600` permissions and stored in the git-ignored `data/` directory.
* **Privacy Boundary**:
  * Machine-local credentials (Home Assistant tokens, Spotify secrets, local paths) never leave the host.
  * Preferences are strictly validated against allowlists; incoming facts and rules pass prompt-injection guards.

### 2. Network & System Resource Efficiency
* **Cryptographic Section Hashing**: Computes SHA-256 digests for data partitions:
  * `facts`: Normalized facts content.
  * `preferences`: Timestamped persona and behavior keys (`sass`, `reply_language`, `answer_length`, `call`).
  * `rules`: User-approved rules.
  * `learned`: Learned recipes and routines.
  * `memories`: Notepad entries in `MemoryStore`.
  * `lists`: Items across named lists in `ListStore`.
* **Zero-Bandwidth Incremental Delta Sync**:
  * During periodic syncs, nodes exchange section hashes.
  * If hashes match, a `{"changed": false}` response is returned—**0 bytes** of state transferred and 0 disk writes performed.
  * Only partitions with differing hashes are serialized and transmitted.
* **Adaptive Heartbeats & Exponential Backoff**:
  * Connected peers are checked every 60 seconds.
  * Offline or unreachable peers back off exponentially (10s → 15s → 22s → ... up to 300s max) to conserve CPU cycles, thread wakeups, and battery on laptops.
* **Debounced Mutation Push**:
  * When a voice turn records a memory or changes preferences locally, `settings.mesh_notify_mutation()` schedules a debounced push (2.0s delay).
  * Rapid back-to-back voice turns coalesce into a single outbound sync.

### 3. Distributed Skill Sharing ("Enhance Each Other")
* **Heterogeneous Capabilities**:
  * `antigua-mac` advertises platform capabilities: `["macos_shortcuts", "macos_reminders", "macos_calendar", "macos_system"]`.
  * `Antigua-Home` advertises IoT capabilities: `["govee_lights", "living_room_tv", "climate_ac", "security_cams"]`.
* **Transparent Intent Delegation**:
  * Asking Antigua-Mac: *"Turn off the living room lights"* or *"Show me the front door camera"*:
    1. Mac recognizes `govee_lights` is not present locally.
    2. `MeshSkillsRouter` delegates the intent to Antigua-Home via signed `POST /mesh/delegate`.
    3. Home executes the deterministic skill and returns the response.
    4. Mac speaks the confirmation natively through its local voice.
  * Asking Antigua-Home: *"Run Mac shortcut Work Mode"*:
    1. Home delegates to Mac when online.
    2. Mac runs `shortcuts run "Work Mode"` and confirms completion.
  * If the managing peer is offline, Antigua gracefully explains:
    > *"Living room lights are managed by Antigua Home, which is currently unreachable."*

---

## Zero-Conf Local Network Discovery

Antigua nodes broadcast lightweight UDP beacons on port `9394` (`ANTIGUA_BEACON_V1`). You can discover any Antigua node on the same local network without inspecting router DHCP tables or typing IP addresses:

```bash
python3 -m antigua_core.mesh.cli discover
```

Example output:
```text
Searching for Antigua nodes on the local network...

Discovered 1 Antigua node(s):
  • Living Room Antigua (antigua-linux-84a6f7cf)
    Platform:     linux
    Endpoint:     http://192.168.1.150:9393
    Capabilities: chat, llm, memory, lists, timers, persona, govee_lights, living_room_tv
    To pair:      python3 -m antigua_core.mesh.cli pair http://192.168.1.150:9393
```

---

## Mesh HTTP API (`MeshRoutes`)

All endpoints are hosted directly on the server's HTTP port (default `9393`):

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| `GET` | `/mesh/info` | Public | Node identity, platform, and advertised capabilities |
| `POST` | `/mesh/pair/initiate` | Public | Generates ephemeral 6-digit PIN and starts pairing session |
| `POST` | `/mesh/pair/confirm` | Public (PIN) | Verifies PIN and exchanges mutual long-lived HMAC secret |
| `GET` | `/mesh/peers` | Public | Lists all paired peers and status (safe view without secrets) |
| `DELETE` | `/mesh/peers/<id>` | Local/Signed | Unpairs a peer and revokes mutual secrets |
| `POST` | `/mesh/sync` | Signed | Bidirectional delta sync of facts, persona, notepad, and lists |
| `POST` | `/mesh/delegate` | Signed | Remote intent delegation RPC |

---

## CLI Reference

Manage the mesh directly from the command line using `antigua_core.mesh.cli`:

### Check Local Node Status
```bash
python3 -m antigua_core.mesh.cli status
```

### Discover Nodes on LAN
```bash
python3 -m antigua_core.mesh.cli discover [--timeout 2.0]
```

### Pair with Another Node
```bash
python3 -m antigua_core.mesh.cli pair http://192.168.1.150:9393
```

### List Paired Peers
```bash
python3 -m antigua_core.mesh.cli peers
```

### Trigger Manual Sync
```bash
python3 -m antigua_core.mesh.cli sync [node_id]
```

### Unpair a Node
```bash
python3 -m antigua_core.mesh.cli unpair <node_id>
```
