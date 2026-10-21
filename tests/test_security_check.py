#!/usr/bin/env python3
"""server/scripts/security_check.py: what gets reported (only changes), the
dashboard-tunnel rule, hostname drift and new listeners. No network: the
shell and ingress readers are stubbed.

Run: python3 tests/test_security_check.py   (also works under pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server" / "scripts"))

import security_check as sc  # noqa: E402


def test_only_changes_are_reported():
    prev = {"a": "x old", "b": "y", "_ports": ["22/sshd"]}
    cur = {"a": "x new", "b": "y", "c": "z"}
    assert sc.changes(prev, cur) == ["⚠️ x new", "⚠️ z"]
    assert sc.changes(cur, {"b": "y"}) == ["✅ resolved: x new", "✅ resolved: z"]
    assert sc.changes(cur, cur) == []


def test_a_tunnel_to_the_dashboard_is_flagged_and_hostnames_drift():
    sc.BACKUP_SSH = "root@backup-box"  # hosts.conf is git-ignored, so not in CI
    rules = {None: [{"hostname": "ssh01.example", "service": "ssh://localhost:22"},
                    {"hostname": "cal.example", "service": "http://localhost:8090"},
                    {"service": "http_status:404"}],
             sc.BACKUP_SSH: [{"hostname": "media.example", "service": "http://localhost:8096"}]}
    sc._ingress = lambda host=None: rules[host]
    state = {"_tunnels": ["media.example", "ssh01.example"]}
    found = sc.check_tunnels(state)
    assert "8090" in found["tunnel:cal.example"], found
    assert found["tunnels:changed"] == "public hostnames changed: added cal.example", found
    assert state["_tunnels_now"] == ["cal.example", "media.example", "ssh01.example"]
    assert "tunnels:changed" not in sc.check_tunnels({})  # first run sets the baseline


def test_hosts_come_from_hosts_conf():
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "network").mkdir()
    (d / "network" / "hosts.conf").write_text(
        "# name target lan wg ports units users\n"
        "primary local 192.168.1.10 10.77.0.1 9393,11434 antigua-server root,pineda\n"
        "satellite pi 192.168.1.11 10.77.0.2 1883 antigua-satellite root,pi\n"
        "backup root@backup-box 192.168.1.12 10.77.0.3 9394 antigua-fallback\n")
    sc.REPO, real = d, sc.REPO
    try:
        assert sc._hosts() == {"satellite": "pi", "backup": "root@backup-box"}
    finally:
        sc.REPO = real


def test_new_lan_listeners():
    ss = ("LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:((\"sshd\",pid=1,fd=3))\n"
          "LISTEN 0 4096 127.0.0.1:5500 0.0.0.0:* users:((\"python3\",pid=2,fd=3))\n"
          "LISTEN 0 4096 *:9999 *:* users:((\"nc\",pid=3,fd=3))\n")
    sc.sh = lambda cmd, timeout=120, host=None: ss
    state = {"_ports": ["22/sshd"]}
    assert sc.check_ports(state) == {"ports:new": "new TCP listener(s) beyond localhost on the primary: 9999/nc"}
    assert state["_ports_now"] == ["22/sshd", "9999/nc"]  # localhost-only 5500 ignored
    assert sc.check_ports({}) == {}  # first run sets the baseline


def main():
    test_only_changes_are_reported()
    test_a_tunnel_to_the_dashboard_is_flagged_and_hostnames_drift()
    test_hosts_come_from_hosts_conf()
    test_new_lan_listeners()
    print("PASS — security check suite")


if __name__ == "__main__":
    main()
