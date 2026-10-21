"""Is the internet up, and if not, why: a background prober that checks the
connection one layer at a time, so a failure says where it broke.

  link    this box's network port has no carrier (cable, switch)
  router  the default gateway doesn't answer a ping
  isp     the router answers but no public address does (the provider)
  dns     public addresses answer but names don't resolve
  web     names resolve but an HTTPS fetch fails

Two failed rounds in a row mark it offline and one passing round marks it
back online, so a single dropped packet isn't an outage. Outages (start,
end, cause) are kept in DATA_DIR/net_outages.json for "when did the
internet go out?". Read-only: it only ever looks.
"""

import json
import logging
import socket
import subprocess
import threading
import time
from pathlib import Path

import requests

from . import settings

log = logging.getLogger("antigua_core")

_PUBLIC_IPS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
_DNS_NAMES = ("api.open-meteo.com", "www.google.com")
_WEB_URL = "https://www.gstatic.com/generate_204"
_INTERVAL_UP = 30
_INTERVAL_DOWN = 10     # notice the return quickly
_SLOW_S = 3.0           # an HTTPS round trip slower than this is "degraded"
_KEEP = 100             # outages remembered

CAUSES = {
    "link": "this server's network connection is unplugged or the switch is off",
    "router": "the router isn't answering",
    "isp": "the router's fine but nothing past it answers, so it's the internet provider",
    "dns": "the connection works but name lookups are failing",
    "web": "name lookups work but websites aren't loading",
}


def default_route() -> tuple[str | None, str | None]:
    """(interface, gateway IP) of the default route, from /proc/net/route."""
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            f = line.split()
            if len(f) > 2 and f[1] == "00000000":
                return f[0], socket.inet_ntoa(bytes.fromhex(f[2])[::-1])
    except OSError:
        pass
    return None, None


def _carrier(iface: str) -> bool:
    try:
        return Path(f"/sys/class/net/{iface}/carrier").read_text().strip() == "1"
    except OSError:
        return False


def _ping(host: str) -> bool:
    try:
        return subprocess.run(["ping", "-c1", "-W1", "-q", host], capture_output=True,
                              timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _tcp(host: str, port: int = 443, timeout: float = 2) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _resolves(name: str, timeout: float = 3) -> bool:
    """getaddrinfo can't time out on its own; give it a thread and walk away."""
    ok = []
    th = threading.Thread(target=lambda: ok.append(bool(socket.getaddrinfo(name, 443))),
                          daemon=True)
    th.start()
    th.join(timeout)
    return bool(ok and ok[0])


def probe() -> dict:
    """One round of checks → {"ok", "cause", "web_s", "gateway_ms", ...}."""
    iface, gw = default_route()
    r = {"ok": False, "cause": None, "iface": iface, "gateway": gw, "web_s": None, "at": time.time()}
    if not iface or not _carrier(iface):
        r["cause"] = "link"
        return r
    if gw:
        t = time.monotonic()
        if not _ping(gw):
            r["cause"] = "router"
            return r
        r["gateway_ms"] = round((time.monotonic() - t) * 1000)
    if not any(_tcp(ip) for ip in _PUBLIC_IPS):
        r["cause"] = "isp"
        return r
    if not any(_resolves(n) for n in _DNS_NAMES):
        r["cause"] = "dns"
        return r
    t = time.monotonic()
    try:
        requests.get(_WEB_URL, timeout=6).raise_for_status()
    except requests.RequestException:
        r["cause"] = "web"
        return r
    r["web_s"] = round(time.monotonic() - t, 2)
    r["ok"] = True
    return r


class NetStatus:
    def __init__(self, path: Path | None = None, probe_fn=probe):
        self.path = path or settings.DATA_DIR / "net_outages.json"
        self._probe = probe_fn
        self.listeners: list = []          # fn(state, cause, outage) on online ↔ offline
        self.state = "unknown"             # online | degraded | offline | unknown
        self.cause: str | None = None
        self.since = time.time()
        self.last: dict = {}
        self._fails = 0
        self._lock = threading.Lock()
        self.outages: list[dict] = self._load()
        if self.outages and self.outages[-1].get("end") is None:
            self.state, self.cause = "offline", self.outages[-1]["cause"]
            self.since = self.outages[-1]["start"]

    def _load(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())[-_KEEP:]
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.outages[-_KEEP:]))
            tmp.replace(self.path)
        except OSError as e:
            log.warning("Net: saving outages: %s", e)

    @property
    def online(self) -> bool:
        """False only once an outage is confirmed; unknown counts as online."""
        return self.state != "offline"

    def check(self) -> None:
        """One probe round, and the state change it causes, if any."""
        r = self._probe()
        with self._lock:
            self.last = r
            if r["ok"]:
                self._fails = 0
                new = "degraded" if (r["web_s"] or 0) > _SLOW_S else "online"
                if self.state == "offline":
                    out = self.outages[-1] if self.outages else None
                    if out and out.get("end") is None:
                        out["end"] = r["at"]
                        self._save()
                    log.info("Net: back online after %.0f min (%s)",
                             (r["at"] - self.since) / 60, self.cause)
                    self._set(new, None, r["at"], out)
                elif new != self.state:
                    if self.state != "unknown" or new == "degraded":
                        log.info("Net: %s (web %.1fs)", new, r["web_s"])
                    self.state, self.since = new, r["at"]
                return
            self._fails += 1
            if self.state == "offline":
                if r["cause"] != self.cause:      # e.g. router came back, provider still down
                    log.info("Net: still offline, now %s", r["cause"])
                    self.cause = r["cause"]
                    self.outages[-1]["cause"] = r["cause"]
                    self._save()
                return
            if self._fails >= 2:
                start = r["at"] - _INTERVAL_DOWN
                out = {"start": start, "end": None, "cause": r["cause"]}
                self.outages.append(out)
                self._save()
                log.warning("Net: offline (%s): %s", r["cause"], CAUSES[r["cause"]])
                self._set("offline", r["cause"], start, out)

    def _set(self, state, cause, since, outage) -> None:
        self.state, self.cause, self.since = state, cause, since
        for fn in self.listeners:
            try:
                fn(state, cause, outage)
            except Exception:
                log.exception("Net: listener failed")

    def _run(self) -> None:
        while True:
            try:
                self.check()
            except Exception:
                log.exception("Net: probe failed")
            time.sleep(_INTERVAL_DOWN if self._fails else _INTERVAL_UP)

    def start(self) -> "NetStatus":
        threading.Thread(target=self._run, daemon=True, name="netstatus").start()
        return self

    def snapshot(self) -> dict:
        return {"state": self.state, "cause": self.cause, "since": self.since,
                "web_s": self.last.get("web_s"), "gateway_ms": self.last.get("gateway_ms")}


# The one the server starts; skills ask it before reaching for the internet.
monitor: NetStatus | None = None


def online() -> bool:
    return monitor is None or monitor.online
