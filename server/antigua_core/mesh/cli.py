"""Command-line interface for managing Antigua Mesh nodes, pairing, and synchronization."""

import argparse
import sys
import time

from .client import MeshClient
from .identity import MeshIdentity
from .peers import PeerRegistry
from .sync import MeshSync


def cmd_status(identity: MeshIdentity, peers: PeerRegistry):
    info = identity.info
    print("Antigua Node Status")
    print("-------------------")
    print(f"Node ID:       {info.node_id}")
    print(f"Name:          {info.name}")
    print(f"Platform:      {info.platform}")
    print(f"Version:       {info.version}")
    print(f"Capabilities:  {', '.join(info.capabilities)}")
    print(f"Paired Peers:  {len(peers.get_all())}")


def cmd_peers(peers: PeerRegistry):
    all_p = peers.get_all()
    if not all_p:
        print("No paired peers found. Use 'pair <remote_url>' to connect to another Antigua node.")
        return

    print(f"Paired Antigua Peers ({len(all_p)})")
    print(f"{'Name':<22} {'Node ID':<26} {'Status':<10} {'Endpoint':<25}")
    print("-" * 85)
    for p in all_p:
        print(f"{p.name:<22} {p.node_id:<26} {p.status:<10} {p.endpoint:<25}")
        if p.capabilities:
            print(f"  └─ Capabilities: {', '.join(p.capabilities)}")
        if p.last_sync > 0:
            ago = int(time.time() - p.last_sync)
            print(f"  └─ Last synced:  {ago}s ago")


def cmd_pair(identity: MeshIdentity, peers: PeerRegistry, sync: MeshSync, args):
    remote_url = args.url.rstrip("/")
    my_endpoint = args.my_endpoint or f"http://{identity.node_id}.local:9393"

    print(f"Initiating pairing with {remote_url} ...")
    client = MeshClient(identity, peers, sync)

    def on_pin(pin):
        print("\n==========================================")
        print(f"   ANTIGUA PAIRING PIN:  {pin}           ")
        print("==========================================")
        print("Verifying PIN with remote node ...\n")

    success, peer, err = client.pair_with_remote_node(
        remote_url=remote_url,
        my_endpoint=my_endpoint,
        pin_callback=on_pin,
    )

    if success and peer:
        print(f"✓ Successfully paired with {peer.name} ({peer.node_id})!")
        print(f"Capabilities: {', '.join(peer.capabilities)}")
        print("\nTriggering initial state synchronization ...")
        sync_ok, res = client.sync_peer(peer)
        if sync_ok:
            print("✓ State synchronized successfully.")
            print(f"Sync summary: {res.get('local_summary', {})}")
        else:
            print(f"Initial sync warning: {res.get('error')}")
    else:
        print(f"✗ Pairing failed: {err}")
        sys.exit(1)


def cmd_sync(identity: MeshIdentity, peers: PeerRegistry, sync: MeshSync, args):
    client = MeshClient(identity, peers, sync)
    if args.node_id:
        peer = peers.get(args.node_id)
        if not peer:
            print(f"Error: peer '{args.node_id}' not found.")
            sys.exit(1)
        print(f"Syncing with {peer.name} ...")
        ok, res = client.sync_peer(peer)
        if ok:
            print(f"✓ Sync complete: {res}")
        else:
            print(f"✗ Sync failed: {res.get('error')}")
    else:
        print("Syncing with all paired peers ...")
        results = client.sync_all()
        for nid, r in results.items():
            st = "✓" if r["success"] else "✗"
            print(f"{st} {r['name']} ({nid}): {r['summary']}")


def cmd_unpair(peers: PeerRegistry, args):
    if peers.remove(args.node_id):
        print(f"✓ Removed peer {args.node_id}")
    else:
        print(f"Error: peer {args.node_id} not found.")
        sys.exit(1)


def cmd_discover(identity: MeshIdentity, args):
    from .discovery import MeshDiscovery
    print("Searching for Antigua nodes on the local network...")
    disc = MeshDiscovery(identity)
    found = disc.discover_once(timeout=args.timeout)
    if not found:
        print("No other Antigua nodes discovered on this subnet.")
        return
    print(f"\nDiscovered {len(found)} Antigua node(s):")
    for node in found:
        print(f"  • {node['name']} ({node['node_id']})")
        print(f"    Platform:     {node['platform']}")
        print(f"    Endpoint:     {node['endpoint']}")
        print(f"    Capabilities: {', '.join(node.get('capabilities', []))}")
        print(f"    To pair:      python3 -m antigua_core.mesh.cli pair {node['endpoint']}\n")


def main():
    parser = argparse.ArgumentParser(description="Antigua Mesh CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show local node identity and mesh status")
    sub.add_parser("peers", help="List all paired peers")

    disc_p = sub.add_parser("discover", help="Scan local network for Antigua nodes")
    disc_p.add_argument("--timeout", type=float, default=2.0, help="Scan timeout in seconds")

    pair_p = sub.add_parser("pair", help="Pair with a remote Antigua node")
    pair_p.add_argument("url", help="Remote Antigua node URL (e.g. http://192.168.1.150:9393)")
    pair_p.add_argument("--my-endpoint", help="Our endpoint to register on remote node")

    sync_p = sub.add_parser("sync", help="Trigger state sync with peers")
    sync_p.add_argument("node_id", nargs="?", help="Optional specific peer node ID")

    unpair_p = sub.add_parser("unpair", help="Unpair a peer node")
    unpair_p.add_argument("node_id", help="Node ID of the peer to remove")

    args = parser.parse_args()

    identity = MeshIdentity()
    peers = PeerRegistry()
    sync = MeshSync(node_id=identity.node_id)

    if args.cmd == "status":
        cmd_status(identity, peers)
    elif args.cmd == "peers":
        cmd_peers(peers)
    elif args.cmd == "discover":
        cmd_discover(identity, args)
    elif args.cmd == "pair":
        cmd_pair(identity, peers, sync, args)
    elif args.cmd == "sync":
        cmd_sync(identity, peers, sync, args)
    elif args.cmd == "unpair":
        cmd_unpair(peers, args)


if __name__ == "__main__":
    main()
