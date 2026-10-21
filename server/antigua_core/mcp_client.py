"""Minimal synchronous MCP clients shared by both servers: stdio (Roku,
Govee) and stateless Streamable HTTP (pineda-web's /mcp).

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

        pineda:                           # MCP 2026-07-28 over HTTP (HttpMcpServer)
          urls: [http://127.0.0.1:8090/mcp/, http://standby.lan:8090/mcp/]
          token_file: /etc/antigua/pineda-mcp.env   # PINEDA_MCP_TOKEN=..., root-only

Relative paths resolve against the repo root, so the same config works on
the primary and on the backup's mirrored checkout.

Every tool call, on either transport, is written to the tool-call log
(ToolLog: data/tool_calls.db) with the voice turn that made it — begin_turn()
— so "take it down" or "who was that?" can see what was just done, and
pineda-web's own audit joins on the turn's trace id.
"""

import contextvars
import json
import logging
import os
import queue
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Thread

import httpx

log = logging.getLogger("antigua_core.mcp")

PROTOCOL_VERSION = "2025-06-18"     # stdio servers (handshake)
HTTP_PROTOCOL_VERSION = "2026-07-28"  # HTTP servers (stateless)


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
        start = time.perf_counter()
        result = self._call(tool, arguments, timeout)
        TOOL_LOG.add(self.name, tool, arguments, result, start)
        return result

    def _call(self, tool, arguments, timeout) -> McpResult:
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


# ── the voice turn a call belongs to, and the tool-call log ──────────────────

_turn: contextvars.ContextVar[dict | None] = contextvars.ContextVar("mcp_turn", default=None)


def begin_turn(transcript: str = "", speaker: str | None = None,
               conversation_id: str = "", path: str = "voice") -> str:
    """Mark the calls this thread makes from now on as this turn's: a new
    W3C trace id, sent to HTTP servers in _meta.traceparent and logged."""
    trace = os.urandom(16).hex()
    _turn.set({"trace": trace, "transcript": transcript[:300], "speaker": speaker or "",
               "conversation": conversation_id, "path": path})
    return trace


def current_turn() -> dict:
    return _turn.get() or {}


def traceparent() -> str:
    trace = current_turn().get("trace") or os.urandom(16).hex()
    return f"00-{trace}-{os.urandom(8).hex()}-01"


class ToolLog:
    """One row per tool call: when, which turn (trace id, transcript,
    speaker), the server, tool, arguments, ok, error and a clipped result,
    and how long it took. Rows hold transcripts, so they're kept 7 days like
    the mic captures and logs, pruned at startup and hourly after that."""

    KEEP_S = 7 * 86400

    def __init__(self):
        self._path: Path | None = None
        self._rows: queue.SimpleQueue = queue.SimpleQueue()
        self._pruned = 0.0

    def open(self, path: Path) -> None:
        self._path = path
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS calls (ts REAL, trace TEXT, path TEXT,"
                       " conversation TEXT, speaker TEXT, transcript TEXT, server TEXT,"
                       " tool TEXT, args TEXT, ok INTEGER, error TEXT, result TEXT, ms REAL)")
            db.execute("CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts)")
            self._prune(db)
        Thread(target=self._write, daemon=True, name="tool-log").start()

    def _write(self):
        """Rows land from here, in batches: a commit's fsync (~8 ms) would
        otherwise sit on every reply."""
        while True:
            rows = [self._rows.get()]
            while not self._rows.empty() and len(rows) < 200:
                rows.append(self._rows.get_nowait())
            try:
                with self._db() as db:
                    db.executemany("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                    if time.time() - self._pruned > 3600:
                        self._prune(db)
            except sqlite3.Error as e:
                log.warning("Tool log: %s", e)

    def _prune(self, db) -> None:
        db.execute("DELETE FROM calls WHERE ts < ?", (time.time() - self.KEEP_S,))
        self._pruned = time.time()

    def _db(self):
        return sqlite3.connect(self._path, timeout=2)

    def add(self, server: str, tool: str, arguments, result: "McpResult", start: float) -> None:
        if self._path is None:
            return
        turn = current_turn()
        value = result.value() if result.ok else None
        error = None if result.ok else (
            (result.data or {}).get("error") if isinstance(result.data, dict) else None
        ) or result.text[:200]
        row = (time.time(), turn.get("trace", ""), turn.get("path", "background"),
               turn.get("conversation", ""), turn.get("speaker", ""), turn.get("transcript", ""),
               server, tool, json.dumps(arguments or {}, default=str)[:4000], int(result.ok),
               error, json.dumps(value, default=str)[:4000] if value is not None else None,
               round((time.perf_counter() - start) * 1000, 1))
        self._rows.put(row)

    def recent(self, since: float = 0, limit: int = 50, conversation: str | None = None) -> list[dict]:
        if self._path is None:
            return []
        q = "SELECT * FROM calls WHERE ts >= ?"
        args: list = [since]
        if conversation:
            q += " AND conversation = ?"
            args.append(conversation)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._db() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(q, args)]


TOOL_LOG = ToolLog()


# ── stateless Streamable HTTP (MCP 2026-07-28) ──────────────────────────────


class HttpMcpServer:
    """One MCP server reached over HTTP: no handshake and no session, each
    call one POST with the protocol version, client info and trace id in
    _meta. Kept-alive connections to each of `urls`, tried in order: the
    next one only when the first can't be reached — or, for a tool whose
    annotations say it's safe to repeat (read-only or idempotent), when it
    failed partway."""

    def __init__(self, name, urls, token=None, timeout=5.0, connect_timeout=0.5):
        self.name = name
        self.urls = [u if u.endswith("/") else u + "/" for u in urls]
        self.timeout = timeout
        self.tools: dict[str, dict] = {}
        self._token = token
        self._listed_until = 0.0
        self._next_id = 1
        self._lock = Lock()
        self._discovered: dict = {}
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=connect_timeout),
                                  limits=httpx.Limits(max_keepalive_connections=4))

    # ── what McpHub expects ──

    def ready(self) -> bool:
        return bool(self._discovered)

    def start(self):
        """Discover (protocol check, health) and list the tools."""
        self._discovered = self._rpc("server/discover", {}, None)[0]
        self._list(force=True)
        info = self._discovered.get("_meta", {}).get("io.modelcontextprotocol/serverInfo", {})
        log.info("MCP %s up: %s %s, tools=%s", self.name, info.get("name"),
                 info.get("version"), sorted(self.tools))

    def close(self):
        self._http.close()

    # ── calls ──

    def call(self, tool: str, arguments: dict | None = None, timeout=None,
             on_progress=None) -> McpResult:
        """Call a tool; never raises."""
        start = time.perf_counter()
        try:
            res, _url = self._rpc("tools/call", {"name": tool, "arguments": arguments or {}},
                                  tool, timeout=timeout, on_progress=on_progress,
                                  repeatable=self._repeatable(tool))
            text = "\n".join(c.get("text", "") for c in res.get("content", [])
                             if c.get("type") == "text")
            result = McpResult(ok=not res.get("isError"), text=text,
                               data=res.get("structuredContent"))
        except McpError as e:
            log.warning("MCP %s.%s failed: %s", self.name, tool, e)
            result = McpResult(ok=False, text=str(e))
        TOOL_LOG.add(self.name, tool, arguments, result, start)
        return result

    def list_tools(self) -> list[dict]:
        """The tools, cached for the server's ttlMs."""
        try:
            self._list()
        except McpError as e:
            log.warning("MCP %s tools/list: %s", self.name, e)
        return list(self.tools.values())

    def _list(self, force=False):
        if not force and time.time() < self._listed_until:
            return
        res, _ = self._rpc("tools/list", {}, None)
        self.tools = {t["name"]: t for t in res.get("tools", [])}
        self._listed_until = time.time() + res.get("ttlMs", 0) / 1000

    def _repeatable(self, tool: str) -> bool:
        a = (self.tools.get(tool) or {}).get("annotations") or {}
        return bool(a.get("readOnlyHint") or a.get("idempotentHint"))

    # ── wire ──

    def _rpc(self, method, params, name, timeout=None, on_progress=None, repeatable=True):
        with self._lock:
            mid = self._next_id
            self._next_id += 1
        meta = {
            "io.modelcontextprotocol/protocolVersion": HTTP_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "antigua", "version": "1.0"},
            "traceparent": traceparent(),
        }
        if on_progress is not None:
            meta["progressToken"] = mid
        body = {"jsonrpc": "2.0", "id": mid, "method": method,
                "params": {**params, "_meta": meta}}
        headers = {"Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": HTTP_PROTOCOL_VERSION, "Mcp-Method": method}
        if name:
            headers["Mcp-Name"] = name
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        last = None
        for url in self.urls:
            try:
                return self._post(url, body, headers, timeout, on_progress), url
            except httpx.ConnectError as e:      # never reached it: always safe to try the next
                last = e
            except (httpx.TransportError, _ServerDown) as e:
                last = e
                if not repeatable:
                    break
        raise McpError(f"{self.name}: {last}")

    def _post(self, url, body, headers, timeout, on_progress):
        t = httpx.Timeout(timeout or self.timeout, connect=self._http.timeout.connect)
        with self._http.stream("POST", url, json=body, headers=headers, timeout=t) as r:
            if r.status_code >= 500:
                raise _ServerDown(f"HTTP {r.status_code}")
            if r.headers.get("content-type", "").startswith("text/event-stream"):
                msg = self._from_sse(r, body["id"], on_progress)
            else:
                r.read()
                try:
                    msg = r.json()
                except ValueError:
                    raise McpError(f"{self.name}: HTTP {r.status_code}") from None
            if r.status_code in (401, 403):
                raise McpError(f"{self.name}: {msg.get('detail', r.status_code)}")
        if "error" in msg:
            raise McpError(f"{self.name}: {msg['error'].get('message', 'error')}")
        return msg.get("result", {})

    @staticmethod
    def _from_sse(r, mid, on_progress):
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                msg = json.loads(line[5:])
            except ValueError:
                continue
            if msg.get("id") == mid and ("result" in msg or "error" in msg):
                return msg
            if msg.get("method") == "notifications/progress" and on_progress is not None:
                p = msg.get("params", {})
                try:
                    on_progress(p.get("progress"), p.get("total"), p.get("message"))
                except Exception:  # noqa: BLE001 — a progress callback never fails the call
                    log.exception("MCP progress callback")
        raise _ServerDown("stream ended without a reply")


class _ServerDown(Exception):
    pass


@dataclass
class McpHub:
    """The configured MCP servers, by name."""

    servers: dict[str, "McpServer | HttpMcpServer"] = field(default_factory=dict)

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
            if s.get("urls"):
                token = None
                if s.get("token_file"):
                    token = next(iter(read_env_file(Path(s["token_file"])).values()), None)
                hub.servers[name] = HttpMcpServer(name, s["urls"], token,
                                                  timeout=float(s.get("timeout", 5)))
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
