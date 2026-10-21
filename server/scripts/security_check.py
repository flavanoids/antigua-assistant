#!/usr/bin/env python3
"""Weekly security drift check, reported to Telegram only when something changes.
Run from root's crontab on the primary:
  0 9 * * 1 /usr/bin/python3 <repo>/server/scripts/security_check.py >> <repo>/logs/security_check.log 2>&1

The 2026-10-16 review found drift, not exotic bugs: a forgotten tunnel
hostname publishing the dashboard, CI red for a week, Ollama eight minor
versions behind. Each check below watches for one of those:
  deps     — pip-audit on every Antigua venv (via `uvx pip-audit`).
  ollama, music, searxng — installed vs latest release (searxng: pin age).
  tunnels  — the public hostnames on every cloudflared (a new one is news),
             and none of them may reach the PinedaDisplay dashboard (:8090).
  guards   — antigua_guard on all three hosts, pineda_web_guard (with
             cloudflared in @tunnel_units) on the primary and backup.
  ports    — a TCP port that newly listens beyond localhost on the primary.
  ci       — main is green in both repos.

A finding is reported when it first appears or its text changes, and once
more when it clears. Nothing is sent when nothing changed. Uses the same
`hermes send` channel as health_alerts.py. `--dry-run` prints without sending
or saving state.
"""
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE_FILE = REPO / "data" / "security_check_state.json"
VENVS = ["venv", "kitchen-mic/bridge/venv", "server/mcp/venv", "server/mcp/venv-atv"]
PINEDA_PORT = 8090
SEARXNG_MAX_AGE_DAYS = 60


def _hosts() -> dict:
    """ssh targets by role, from the git-ignored network/hosts.conf (the same
    table wg-mesh.sh uses): the backup guards 9394, the satellite 1883."""
    roles = {}
    try:
        lines = (REPO / "network" / "hosts.conf").read_text().splitlines()
    except OSError:
        return roles
    for line in lines:
        f = line.split()
        if len(f) < 5 or f[0].startswith("#"):
            continue
        ports = f[4].split(",")
        target = None if f[1] == "local" else f[1]
        if "9394" in ports:
            roles["backup"] = target
        if "1883" in ports:
            roles["satellite"] = target
    return roles


def _ci_repos() -> list[str]:
    """security_check.repos in server.yaml (git-ignored), else this repo's origin."""
    try:
        import yaml
        cfg = yaml.safe_load((REPO / "server" / "config" / "server.yaml").read_text()) or {}
        repos = (cfg.get("security_check") or {}).get("repos")
        if repos:
            return list(repos)
    except (OSError, ImportError, ValueError):
        pass
    url = sh(f"git -C {REPO} remote get-url origin").strip()
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    return [m.group(1)] if m else []


HOSTS = _hosts()
BACKUP_SSH = HOSTS.get("backup")
SATELLITE_SSH = HOSTS.get("satellite")


def sh(cmd, timeout=120, host=None) -> str:
    """Run a shell command (on `host` over ssh if given); stdout, or "" on failure."""
    if host:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, cmd]
    try:
        r = subprocess.run(cmd, shell=host is None, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "antigua-security-check"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


# ── checks: each returns {key: message} ──────────────────────────────────────

def check_deps() -> dict:
    found = {}
    for v in VENVS:
        py = REPO / v / "bin" / "python"
        if not py.exists():
            continue
        req = sh(f"uv pip freeze --python {py} 2>/dev/null | grep -vE '^(-e |#)| @ ' | sed 's/+cpu//'")
        if not req.strip():
            continue
        tmp = STATE_FILE.parent / "tmp" / f"audit_{v.replace('/', '_')}.txt"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(req)
        out = sh(f"uvx pip-audit -r {tmp} --no-deps --disable-pip --progress-spinner off -f json 2>/dev/null",
                 timeout=600)
        tmp.unlink(missing_ok=True)
        try:
            deps = json.loads(out)["dependencies"]
        except (ValueError, KeyError):
            found[f"deps:{v}"] = "pip-audit didn't run"
            continue
        for d in deps:
            for vuln in d.get("vulns", []):
                ids = [vuln["id"], *vuln.get("aliases", [])]
                cve = next((i for i in ids if i.startswith("CVE-")), vuln["id"])
                fix = ", ".join(vuln.get("fix_versions") or []) or "no fix yet"
                found[f"deps:{v}:{d['name']}:{cve}"] = f"{v}: {d['name']} {d['version']} has {cve} (fixed in {fix})"
    return found


def check_versions() -> dict:
    found = {}
    try:
        have = version_tuple(sh("ollama --version"))
        latest = get_json("https://api.github.com/repos/ollama/ollama/releases/latest")["tag_name"]
        if have and have[:2] < version_tuple(latest)[:2]:
            found["ollama"] = f"Ollama {'.'.join(map(str, have))} is behind {latest}"
    except (OSError, ValueError, KeyError):
        found["ollama"] = "couldn't compare the Ollama version"
    try:
        image = sh("docker inspect antigua-music-assistant --format '{{.Config.Image}}'").strip()
        latest = get_json("https://api.github.com/repos/music-assistant/server/releases/latest")["tag_name"]
        have = image.rsplit(":", 1)[-1]
        if version_tuple(have)[:2] < version_tuple(latest)[:2]:
            found["music"] = f"Music Assistant {have} is behind {latest}"
    except (OSError, ValueError, KeyError):
        found["music"] = "couldn't compare the Music Assistant version"
    m = re.search(r"searxng/searxng:(\d{4})\.(\d{1,2})\.(\d{1,2})",
                  (REPO / "searxng" / "docker-compose.yml").read_text())
    if m:
        from datetime import date
        age = (date.today() - date(*map(int, m.groups()))).days
        if age > SEARXNG_MAX_AGE_DAYS:
            found["searxng"] = f"SearXNG pin is {age} days old; bump it (docs/deployment.md step 6)"
    else:
        found["searxng"] = "SearXNG isn't pinned to a dated image"
    return found


def _ingress(host=None) -> list[dict]:
    try:
        return json.loads(sh("curl -s -m5 localhost:20241/config", host=host))["config"]["ingress"]
    except (ValueError, KeyError, TypeError):
        return []


def check_tunnels(state: dict) -> dict:
    found = {}
    names = []
    for label, host in (("primary", None), ("backup", BACKUP_SSH)):
        if label == "backup" and not host:
            continue
        rules = _ingress(host)
        if not rules:
            found[f"tunnel:{label}"] = f"couldn't read the {label}'s cloudflared ingress"
        for r in rules:
            if not r.get("hostname"):
                continue
            names.append(r["hostname"])
            if re.search(rf":{PINEDA_PORT}\b", r.get("service", "")):
                found[f"tunnel:{r['hostname']}"] = (
                    f"{r['hostname']} routes to :{PINEDA_PORT} (PinedaDisplay); it must stay LAN-only")
    names.sort()
    known = state.get("_tunnels")
    if known is not None and names != known:
        added, removed = sorted(set(names) - set(known)), sorted(set(known) - set(names))
        found["tunnels:changed"] = "public hostnames changed: " + "; ".join(
            filter(None, [f"added {', '.join(added)}" if added else "",
                          f"removed {', '.join(removed)}" if removed else ""]))
    state["_tunnels_now"] = names
    return found


def check_guards() -> dict:
    found = {}
    for label, host in (("primary", None), ("satellite", SATELLITE_SSH), ("backup", BACKUP_SSH)):
        if label != "primary" and not host:
            continue
        sudo = "" if host is None or host.startswith("root@") else "sudo "
        # /usr/sbin/nft: cron's PATH and remote non-login shells lack /usr/sbin
        out = sh(f"{sudo}/usr/sbin/nft list tables", host=host)
        if "antigua_guard" not in out:
            found[f"guard:{label}"] = f"antigua_guard isn't loaded on the {label}"
        if label != "satellite":
            if "pineda_web_guard" not in out:
                found[f"pguard:{label}"] = f"pineda_web_guard isn't loaded on the {label}"
            elif "cloudflared.service" not in sh("/usr/sbin/nft list set inet pineda_web_guard tunnel_units", host=host):
                found[f"pguard:{label}"] = f"cloudflared isn't in pineda_web_guard's tunnel set on the {label}"
    return found


def check_ports(state: dict) -> dict:
    ports = set()
    for line in sh("ss -tlnpH").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        addr = parts[3]
        if addr.startswith(("127.", "[::1]", "10.0.3.1")):
            continue
        proc = re.search(r'users:\(\("([^"]+)"', line)
        ports.add(f"{addr.rsplit(':', 1)[-1]}/{proc.group(1) if proc else '?'}")
    known = state.get("_ports")
    state["_ports_now"] = sorted(ports)
    new = sorted(ports - set(known or []))
    if known is not None and new:
        return {"ports:new": "new TCP listener(s) beyond localhost on the primary: " + ", ".join(new)}
    return {}


def check_ci() -> dict:
    found = {}
    for repo in _ci_repos():
        out = sh(f"gh run list -R {repo} -b main -w CI -L 1 --json conclusion,headSha -q '.[0]'")
        try:
            run = json.loads(out)
        except ValueError:
            found[f"ci:{repo}"] = f"couldn't read CI for {repo}"
            continue
        if run.get("conclusion") not in ("success", "", None):
            found[f"ci:{repo}"] = f"{repo} main CI is {run['conclusion']} at {run['headSha'][:7]}"
    return found


# ── reporting ────────────────────────────────────────────────────────────────

def changes(previous: dict, current: dict) -> list[str]:
    """Lines worth sending: findings that are new or whose text changed, then
    ones that cleared. Keys starting with "_" are baselines, not findings."""
    prev = {k: v for k, v in previous.items() if not k.startswith("_")}
    lines = [f"⚠️ {msg}" for key, msg in sorted(current.items()) if prev.get(key) != msg]
    lines += [f"✅ resolved: {msg}" for key, msg in sorted(prev.items()) if key not in current]
    return lines


def send(text) -> bool:
    try:
        subprocess.run(["/root/.local/bin/hermes", "send", "-q", "-t", "telegram", text],
                       timeout=60, check=True, capture_output=True)
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return False


def main():
    dry = "--dry-run" in sys.argv
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    scratch = dict(state)
    current = {}
    for check in (check_deps, check_versions, check_guards, check_ci):
        current.update(check())
    current.update(check_tunnels(scratch))
    current.update(check_ports(scratch))

    lines = changes(state, current)
    for key, msg in sorted(current.items()):
        print(f"{key}: {msg}")
    if dry:
        print("\n".join(["--- would send:", *lines]) if lines else "--- nothing new")
        return
    if lines and not send("🔒 Antigua weekly security check\n" + "\n".join(lines)):
        return  # keep the old state so it's sent next week
    new_state = dict(current)
    new_state["_tunnels"] = scratch["_tunnels_now"]
    new_state["_ports"] = scratch["_ports_now"]
    STATE_FILE.write_text(json.dumps(new_state, indent=1))


if __name__ == "__main__":
    main()
