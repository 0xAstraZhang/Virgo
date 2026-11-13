"""MCP: connect stdio Model Context Protocol servers and expose their tools as ``mcp__<server>__<tool>``.

Configure in ``.virgo/mcp.json`` (or ``~/.virgo/mcp.json``, or settings ``mcpServers``) using the
common format::

    {"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                               "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}}

Servers are connected at session start (in parallel); their tools are part of the frozen tool set.
MCP tools need permission by default; allow them with rules like ``mcp__github__*``.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import __version__
from ..agent.types import AgentTool, ToolContext, ToolResult
from ..ai.types import ImageContent, TextContent
from ..diagnostics import capture_error, safe_text
from ..harness.extensions import ROLES_ALL, ExtensionAPI

PROTOCOL_VERSION = "2025-06-18"
STDERR_LIMIT = 8192


class MCPError(Exception):
    def __init__(self, message: str, *, phase="mcp.request", method=None, request_id=None, rpc_code=None):
        super().__init__(message)
        self.phase = phase
        self.method = method
        self.request_id = request_id
        self.rpc_code = rpc_code


def _expand(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


class MCPClient:
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict | None = None,
        cwd: Path | None = None,
    ):
        self.name = name
        self.command = command
        self.args = [_expand(str(a)) for a in args or []]
        self.env = {**os.environ, **{k: _expand(str(v)) for k, v in (env or {}).items()}}
        # Stderr can echo configured credentials. Do not copy env or command
        # arguments into a diagnostic, and redact their explicit secret values.
        self._secrets = tuple(_expand(str(v)) for v in (env or {}).values())
        sensitive_args = []
        previous = ""
        for value in self.args:
            if re.search(r"token|password|secret|credential|api[-_]?key|authorization", previous, re.I):
                sensitive_args.append(value)
            name, sep, data = value.partition("=")
            if sep and re.search(r"token|password|secret|credential|api[-_]?key|authorization", name, re.I):
                sensitive_args.append(data)
            previous = value if value.startswith("-") else ""
        self._secrets += tuple(sensitive_args)
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self.server_info: dict = {}
        self.error: str | None = None
        self.error_details: dict | None = None
        self._next = 0
        self._pending: dict[int, dict] = {}
        self._waiting: set[int] = set()
        self._cv = threading.Condition()
        self._write_lock = threading.Lock()
        self._reader_done = threading.Event()
        self._stderr_done = threading.Event()
        self._reader_failure: Exception | None = None
        self._protocol_error: str | None = None
        self._stderr = bytearray()
        self._stderr_truncated = False
        self._closing = False
        self._stderr_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._request_local = threading.local()

    # -- transport -----------------------------------------------------------
    def start(self) -> None:
        self.proc = subprocess.Popen(
            [_expand(self.command), *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=self.env,
            cwd=str(self.cwd) if self.cwd else None,
        )
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True, name=f"mcp-{self.name}-stderr")
        self._reader_thread = threading.Thread(target=self._reader, daemon=True, name=f"mcp-{self.name}")
        self._stderr_thread.start()
        self._reader_thread.start()

    def _read_stderr(self) -> None:
        try:
            assert self.proc and self.proc.stderr
            while chunk := self.proc.stderr.read(4096):
                with self._cv:
                    self._stderr.extend(chunk)
                    if len(self._stderr) > STDERR_LIMIT:
                        del self._stderr[:-STDERR_LIMIT]
                        self._stderr_truncated = True
        except (OSError, ValueError):
            pass  # Closing a pipe must not replace the server's original error.
        finally:
            self._stderr_done.set()

    def diagnostic(self, error: Exception | str, *, phase: str | None = None, context=None) -> dict:
        """Capture transport evidence without arguments, environment or protocol payloads."""
        with self._cv:
            tail = self._stderr.decode("utf-8", errors="replace")
            truncated = self._stderr_truncated
            protocol_error = self._protocol_error
        metadata = {
            "server": self.name,
            "method": getattr(error, "method", None),
            "request_id": getattr(error, "request_id", None),
            "rpc_code": getattr(error, "rpc_code", None),
            "exit_code": self.proc.poll() if self.proc is not None else None,
            **(context or {}),
        }
        if tail:
            metadata["stderr_tail"] = safe_text(tail, secrets=self._secrets)
            metadata["stderr_truncated"] = truncated
        if protocol_error is not None:
            metadata["protocol_error"] = safe_text(protocol_error, secrets=self._secrets)
        return capture_error(error, phase=phase or getattr(error, "phase", "mcp.request"),
                             context=metadata, secrets=self._secrets)

    def _reader(self) -> None:
        failure = None
        try:
            assert self.proc and self.proc.stdout
            # Keep reads efficient while the main thread owns only the raw pipe;
            # shutdown must not wait for a buffered reader's lock.
            output = io.BufferedReader(self.proc.stdout) if isinstance(self.proc.stdout, io.RawIOBase) else self.proc.stdout
            for line in output:
                try:
                    msg = json.loads(line)
                except (ValueError, UnicodeError) as error:
                    # JSONDecodeError retains its complete input in .doc; keep
                    # only the bounded diagnostic text for ignored stdout noise.
                    self._protocol_error = safe_text(error, secrets=self._secrets)
                    continue
                if not isinstance(msg, dict):
                    self._protocol_error = "Server wrote a non-object JSON message."
                    continue
                rid = msg.get("id")
                if "id" in msg and ("result" in msg or "error" in msg):
                    with self._cv:
                        if type(rid) is int and rid in self._waiting:
                            self._pending[rid] = msg
                            self._cv.notify_all()
                elif "id" in msg and "method" in msg:  # server -> client request: we support none
                    self._send(
                        {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not supported"}}
                    )
        except Exception as error:  # a failed reader must wake all pending requests
            failure = error
        finally:
            with self._cv:
                self._reader_failure = failure
                if not self._closing:
                    self.error = safe_text(failure or "server closed stdout", secrets=self._secrets)
                self._reader_done.set()
                self._cv.notify_all()

    def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        with self._write_lock:
            payload = memoryview((json.dumps(obj) + "\n").encode())
            while payload:
                written = self.proc.stdin.write(payload)
                if not written:
                    raise BrokenPipeError("MCP server input pipe stopped accepting data")
                payload = payload[written:]
            self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        with self._cv:
            self._next += 1
            rid = self._next
            self._waiting.add(rid)
        self._request_local.id = rid
        try:
            if self._reader_done.is_set() or self._closing:
                raise MCPError(f"{self.name}: {self.error or 'connection closed'} for {method}",
                               phase="mcp.read", method=method, request_id=rid) from self._reader_failure
            try:
                self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            except Exception as error:
                raise MCPError(f"{self.name}: cannot write to server ({safe_text(error, secrets=self._secrets)})",
                               phase="mcp.write", method=method, request_id=rid) from error
            with self._cv:
                ok = self._cv.wait_for(
                    lambda: rid in self._pending or self._reader_done.is_set() or self._closing
                    or self.proc is not None and self.proc.poll() is not None,
                    timeout,
                )
                msg = self._pending.pop(rid, None)
        finally:
            with self._cv:
                self._waiting.discard(rid)
                self._pending.pop(rid, None)
        if msg is None:
            raise MCPError(
                f"{self.name}: {'timeout' if not ok else self.error or 'connection closed'} for {method}",
                phase="mcp.timeout" if not ok else "mcp.read", method=method, request_id=rid,
            ) from self._reader_failure
        if "error" in msg:
            error = msg["error"]
            message = error.get("message") if isinstance(error, dict) else error
            code = error.get("code") if isinstance(error, dict) else None
            raise MCPError(f"{self.name}: {safe_text(message or 'server returned an error', secrets=self._secrets)}",
                           phase="mcp.response", method=method, request_id=rid, rpc_code=code)
        if not isinstance(msg["result"], dict):
            raise MCPError(f"{self.name}: invalid result for {method}",
                           phase="mcp.protocol", method=method, request_id=rid)
        return msg["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})
        except Exception as error:
            raise MCPError(f"{self.name}: cannot send notification {method}",
                           phase="mcp.write", method=method) from error

    # -- protocol ------------------------------------------------------------
    def connect(self, timeout: float = 30.0) -> None:
        self.start()
        res = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "virgo", "version": __version__},
            },
            timeout,
        )
        self.server_info = res.get("serverInfo", {})
        self.notify("notifications/initialized")
        tools, cursor = [], None
        while True:
            page = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout)
            listed = page.get("tools", [])
            if not isinstance(listed, list):
                raise MCPError(f"{self.name}: invalid tools/list result", phase="mcp.protocol",
                               method="tools/list", request_id=self._request_local.id)
            tools.extend(t for t in listed if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        self.tools = tools

    def call(self, tool: str, arguments: dict, timeout: float = 300.0) -> dict:
        return self.request("tools/call", {"name": tool, "arguments": arguments}, timeout)

    def close(self) -> None:
        with self._cv:
            self._closing = True
            self._cv.notify_all()
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        # A child can inherit a pipe, so neither drain thread is joined without
        # a bound. Unbuffered pipes can be closed without waiting for a read lock.
        self._stderr_done.wait(0.2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


def _norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)


def tool_name(server: str, tool: str) -> str:
    return f"mcp__{_norm(server)}__{_norm(tool)}"[:64]


def to_result(res: dict) -> ToolResult:
    content: list = []
    for c in res.get("content", []):
        if c.get("type") == "text":
            content.append(TextContent(c.get("text", "")))
        elif c.get("type") == "image":
            content.append(ImageContent(c.get("data", ""), c.get("mimeType", "image/png")))
        elif c.get("type") == "resource":
            r = c.get("resource", {})
            content.append(TextContent(r.get("text") or f"[resource {r.get('uri', '')}]"))
        else:
            content.append(TextContent(json.dumps(c)))
    if not content and "structuredContent" in res:
        content.append(TextContent(json.dumps(res["structuredContent"], ensure_ascii=False)))
    return ToolResult(
        content or [TextContent("(no content)")], details={"mcp": True}, is_error=bool(res.get("isError"))
    )


def load_config(api: ExtensionAPI) -> dict:
    servers: dict = {}
    for f in api.paths.both("mcp.json"):
        if f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                listed = data.get("mcpServers", {}) if isinstance(data, dict) else None
                if not isinstance(listed, dict):
                    raise ValueError("mcpServers must be an object")
                servers.update(listed)
            except ValueError as e:  # includes JSONDecodeError
                api.session.report_error(e, phase="mcp.config", context={"path": str(f)}, notify=False)
                api.ui.print(f"[mcp: invalid {f}: {safe_text(e)}]", "error")
    from_settings = api.settings.get("mcpServers", {}) or {}
    if isinstance(from_settings, dict):
        servers.update(from_settings)
    else:
        api.session.report_error("mcpServers must be an object", phase="mcp.config", notify=False)
        api.ui.print("[mcp: invalid settings: mcpServers must be an object]", "error")
    return {
        k: v for k, v in servers.items() if isinstance(v, dict) and v.get("command") and not v.get("disabled")
    }


def setup(api: ExtensionAPI) -> None:
    clients: dict[str, MCPClient] = {}
    api.services["mcp"] = clients
    registered: set[str] = set()  # tool names owned by the current connections

    def register(client: MCPClient) -> None:
        seen = {t.name for t in api.session.tools_for("main")}
        for spec in client.tools:
            name = tool_name(client.name, spec["name"])
            if name in seen:
                api.ui.print(f"[mcp: skipping duplicate tool name {name}]", "warn")
                continue
            schema = spec.get("inputSchema") or {"type": "object", "properties": {}}

            def execute(args: dict, ctx: ToolContext, _c=client, _t=spec["name"]) -> ToolResult:
                runtime = ctx.review_source.runtime if ctx.review_source is not None else None
                owner = (
                    runtime.store if runtime.store is not None else runtime.accounting_store
                ) if runtime is not None else api.session.store
                metadata = {
                    **(runtime.diagnostic_context() if runtime is not None else {}),
                    "agent": ctx.agent, "server": _c.name, "method": "tools/call", "mcp_tool": _t,
                    "tool_name": tool_name(_c.name, _t), "tool_call_id": ctx.call_id,
                }

                def report(error, phase=None):
                    details = _c.diagnostic(error, phase=phase, context=metadata)
                    return api.session.report_error(details, phase=details["phase"], store=owner, notify=False)

                try:
                    result = to_result(_c.call(_t, args))
                    if result.is_error:
                        metadata["request_id"] = getattr(_c._request_local, "id", None)
                        result.details["error"] = report("MCP server reported a tool error.", phase="mcp.tool")
                    return result
                except Exception as error:
                    details = report(error, phase=getattr(error, "phase", "mcp.tool"))
                    return ToolResult.error(safe_text(error, secrets=_c._secrets), details={"mcp": True, "error": details})

            desc = (spec.get("description") or spec["name"]).strip()
            api.register_tool(
                AgentTool(
                    name,
                    f"[MCP {client.name}] {desc}",
                    schema,
                    execute,
                ),
                roles=ROLES_ALL,
            )
            seen.add(name)
            registered.add(name)

    def connect_all(config: dict) -> None:
        owner = api.session.store

        def one(item):
            name, conf = item
            c = MCPClient(name, conf["command"], conf.get("args"), conf.get("env"), api.session.cwd)
            try:
                c.connect(timeout=float(conf.get("timeout", 30)))
            except Exception as e:  # noqa: BLE001 - one broken server must not stop the session
                c.error = safe_text(e, secrets=c._secrets) or type(e).__name__
                c.close()
                c.error_details = c.diagnostic(e, phase=getattr(e, "phase", "mcp.connect" if c.proc else "mcp.start"))
            return c

        if not config:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(config))) as pool:
            for c in pool.map(one, config.items()):
                clients[c.name] = c
                if c.error:
                    c.error_details = api.session.report_error(
                        c.error_details or c.diagnostic(c.error, phase="mcp.read"), phase="mcp.connect", store=owner,
                    )
                else:
                    register(c)

    connect_all(load_config(api))

    def cmd(session, args):
        if args.strip() == "reconnect":
            for c in clients.values():
                c.close()
            clients.clear()
            # The old tools are bound to the clients just closed; keeping them would both shadow
            # the new connection's tools as duplicates and fail on every call.
            for name in registered:
                session.unregister_tool(name)
            registered.clear()
            connect_all(load_config(api))
            session.main.tools[:] = session.tools_for("main")
            session.main.rebuild_system()
            api.ui.print("MCP servers reconnected (tool set changed: the prompt cache restarts).")
        if not clients:
            api.ui.print("No MCP servers configured (.virgo/mcp.json).")
            return
        for c in clients.values():
            state = f"error: {c.error}" if c.error else f"{len(c.tools)} tools"
            api.ui.print(f"  {c.name}: {state}")

    api.register_command("mcp", cmd, "show MCP servers (/mcp reconnect)", argument_hint="[reconnect]")
    api.on("session_shutdown", lambda s: [c.close() for c in clients.values()])
