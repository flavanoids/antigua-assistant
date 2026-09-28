"""Minimal synchronous MCP client (stdio transport) shared by both servers.

Antigua's servers are threaded, not asyncio, so rather than bridge the async
`mcp` SDK this speaks the JSON-RPC wire protocol directly: one long-lived
subprocess per configured server, a reader thread that routes replies to
waiting callers by request id, and a lazy respawn if the process dies.
Concurrent calls share one session (FastMCP servers handle requests
concurrently).

Servers come from server.yaml:

    mcp:
      env_file: server/config/mcp.env     # KEY=VALUE secrets, gitignored
      servers:
        roku:
          command: [server/mcp/venv/bin/mcp-remote-control]
          env: {HOST_IP: "192.168.1.50"}
          timeout: 8

Relative paths resolve against the repo root, so the same config works on
the primary and on the backup's mirrored checkout.
"""

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Thread

log = logging.getLogger("antigua_core.mcp")

PROTOCOL_VERSION = "2025-06-18"


class McpError(Exception):
    pass


@dataclass
class McpResult:
    ok: bool                 # False on transport/protocol error or isError
    text: str = ""           # concatenated text content
    data: object = None      # structuredContent (FastMCP: {"result": ...} for non-dict returns)

    def value(self):
        """The tool's return value: structured if the server sent it, else the text."""
        d = self.data
        if isinstance(d, dict) and set(d) == {"result"}:
            return d["result"]
        if d is not None:
            return d
        # FastMCP 1.x sends dict/list returns as JSON text with no structuredContent.
        try:
            return json.loads(self.text)
        except ValueError:
            return self.text


def read_env_file(path: Path) -> dict:
    env = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip("'\"")
    except OSError as e:
        log.warning("MCP env file %s unreadable: %s", path, e)
    return env


class McpServer:
    """One stdio MCP server, started on first use and restarted if it dies."""

    def __init__(self, name, command, env=None, cwd=None, timeout=10.0):
        self.name = name
        self.command = list(command)
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = timeout
        self.tools: dict[str, dict] = {}
        self._proc = None
        self._ready = False          # handshake done on the current process
        self._start_lock = Lock()    # one spawn + handshake at a time
        self._lock = Lock()          # guards writes and id allocation
        self._next_id = 1
        self._waiters: dict[int, list] = {}   # id -> [Event, reply]; per process

    # ── lifecycle ────────────────────────────────────────────────────────

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ready(self) -> bool:
        return self._ready and self.alive()

    def start(self):
        with self._start_lock:
            if self.ready():
                return
            if self._proc is not None:
                self._proc.kill()
            self._spawn()
            init = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "antigua", "version": "1.0"},
            })
            self._notify("notifications/initialized")
            info = init.get("serverInfo", {})
            self.tools = {t["name"]: t for t in self._request("tools/list", {}).get("tools", [])}
            self._ready = True
            log.info("MCP %s up: %s %s, tools=%s", self.name, info.get("name"),
                     info.get("version"), sorted(self.tools))

    def _spawn(self):
        self._ready = False
        # Fresh waiter table per process, so the old reader's EOF can't fail
        # requests made to its replacement.
        self._waiters = {}
        env = {**os.environ, **self.env}
        self._proc = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env, cwd=self.cwd,
        )
        proc = self._proc
        Thread(target=self._read_stdout, args=(proc, self._waiters), daemon=True,
               name=f"mcp-{self.name}-out").start()
        Thread(target=self._read_stderr, args=(proc,), daemon=True,
               name=f"mcp-{self.name}-err").start()

    def close(self):
        with self._start_lock:
            self._ready = False
            if self._proc is not None:
                self._proc.kill()
                self._proc = None

    # ── wire ─────────────────────────────────────────────────────────────

    def _read_stdout(self, proc, waiters):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            w = waiters.pop(msg.get("id"), None) if "id" in msg else None
            if w is not None:
                w[1] = msg
                w[0].set()
            # Server-initiated requests/notifications (logging, progress) are
            # ignored — none of the servers we run need a reply to proceed.
        # EOF: the process died. Fail everyone still waiting on it.
        log.warning("MCP %s exited (rc=%s)", self.name, proc.poll())
        for mid, w in list(waiters.items()):
            waiters.pop(mid, None)
            w[0].set()

    def _read_stderr(self, proc):
        for line in proc.stderr:
            log.debug("MCP %s: %s", self.name, line.rstrip())

    def _write(self, msg):
        with self._lock:
            if not self.alive():
                raise McpError(f"{self.name} is not running")
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()

    def _notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def _request(self, method, params, timeout=None):
        with self._lock:
            mid = self._next_id
            self._next_id += 1
            waiters = self._waiters
        waiter = [Event(), None]
        waiters[mid] = waiter
        try:
            self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        except (McpError, BrokenPipeError, OSError) as e:
            waiters.pop(mid, None)
            raise McpError(f"{self.name}: {e}") from e
        if not waiter[0].wait(timeout or self.timeout):
            waiters.pop(mid, None)
            raise McpError(f"{self.name}: {method} timed out")
        reply = waiter[1]
        if reply is None:
            raise McpError(f"{self.name}: server exited during {method}")
        if "error" in reply:
            raise McpError(f"{self.name}: {reply['error'].get('message', 'error')}")
        return reply.get("result", {})

    # ── public ───────────────────────────────────────────────────────────

    def call(self, tool: str, arguments: dict | None = None, timeout=None) -> McpResult:
        """Call a tool; never raises. A dead server is respawned once."""
        for attempt in (1, 2):
            try:
                if not self.ready():
                    self.start()
                res = self._request("tools/call",
                                    {"name": tool, "arguments": arguments or {}}, timeout)
                text = "\n".join(c.get("text", "") for c in res.get("content", [])
                                 if c.get("type") == "text")
                return McpResult(ok=not res.get("isError"), text=text,
                                 data=res.get("structuredContent"))
            except McpError as e:
                if attempt == 2 or self.ready():
                    log.warning("MCP %s.%s failed: %s", self.name, tool, e)
                    return McpResult(ok=False, text=str(e))
        return McpResult(ok=False, text="unreachable")


@dataclass
class McpHub:
    """The configured MCP servers, by name."""

    servers: dict[str, McpServer] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict | None, base_dir: Path) -> "McpHub":
        cfg = cfg or {}
        shared_env = {}
        if cfg.get("env_file"):
            shared_env = read_env_file(base_dir / cfg["env_file"])
        hub = cls()
        for name, s in (cfg.get("servers") or {}).items():
            if not s or s.get("enabled") is False:
                continue
            cmd = [str(base_dir / c) if i == 0 and not os.path.isabs(c) and "/" in c else c
                   for i, c in enumerate(s["command"])]
            env = {**shared_env, **{k: str(v) for k, v in (s.get("env") or {}).items()}}
            hub.servers[name] = McpServer(name, cmd, env=env, cwd=str(base_dir),
                                          timeout=float(s.get("timeout", 10)))
        return hub

    def get(self, name: str) -> McpServer | None:
        return self.servers.get(name)

    def call(self, server: str, tool: str, arguments: dict | None = None, timeout=None) -> McpResult:
        s = self.servers.get(server)
        if s is None:
            return McpResult(ok=False, text=f"MCP server '{server}' not configured")
        return s.call(tool, arguments, timeout)

    def warm(self):
        """Start every server in the background so the first command is fast."""
        def _start(s):
            try:
                s.start()
            except Exception as e:  # noqa: BLE001 — logged; call() retries later
                log.warning("MCP %s failed to start: %s", s.name, e)
        for s in self.servers.values():
            Thread(target=_start, args=(s,), daemon=True, name=f"mcp-{s.name}-warm").start()

    def close(self):
        for s in self.servers.values():
            s.close()

    def status(self) -> dict:
        return {n: {"ready": s.ready(), "tools": sorted(s.tools)} for n, s in self.servers.items()}
