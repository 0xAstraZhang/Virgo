"""MCP: connect stdio Model Context Protocol servers and expose their tools as ``mcp__<server>__<tool>``.

Configure in ``.virgo/mcp.json`` (or ``~/.virgo/mcp.json``, or settings ``mcpServers``) using the
common format::

    {"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                               "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}}

Servers are connected at session start (in parallel). Recovered connections enter
runtime snapshots at a safe boundary, even when their schemas are unchanged.
MCP calls and reconnection need permission by default; allow calls with rules like
``mcp__github__*``. Reconnection never replays a failed call.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import subprocess
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..agent.types import AgentTool, ToolContext, ToolResult
from ..ai.types import ImageContent, TextContent
from ..diagnostics import capture_error, safe_text
from ..harness.extensions import ROLES_ALL, ExtensionAPI

PROTOCOL_VERSION = "2025-06-18"
STDERR_LIMIT = 8192
MAX_RECONNECT_ATTEMPTS = 2
MAX_TOOL_PAGES = 100


class MCPError(Exception):
    def __init__(
        self,
        message: str,
        *,
        phase="mcp.request",
        method=None,
        request_id=None,
        rpc_code=None,
        dispatch="not_started",
        outcome="not_started",
    ):
        super().__init__(message)
        self.phase = phase
        self.method = method
        self.request_id = request_id
        self.rpc_code = rpc_code
        self.dispatch = dispatch
        self.outcome = outcome


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
        self.generation = 1

    def transport(self) -> dict:
        """Thread-local transport facts; a missing response never proves no effect."""
        dispatch = getattr(self._request_local, "dispatch", "not_started")
        return {
            "protocol": "mcp",
            "server": self.name,
            "generation": self.generation,
            "dispatch": dispatch,
            "outcome": getattr(self._request_local, "outcome", "not_started"),
            "side_effects": "none" if dispatch == "not_started" else "unknown",
        }

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
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True, name=f"mcp-{self.name}-stderr"
        )
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
            "generation": self.generation,
            "method": getattr(error, "method", None),
            "request_id": getattr(error, "request_id", None),
            "rpc_code": getattr(error, "rpc_code", None),
            "exit_code": self.proc.poll() if self.proc is not None else None,
            "dispatch": getattr(error, "dispatch", self.transport()["dispatch"]),
            "outcome": getattr(error, "outcome", self.transport()["outcome"]),
            "side_effects": "none"
            if getattr(error, "dispatch", self.transport()["dispatch"]) == "not_started"
            else "unknown",
            **(context or {}),
        }
        if tail:
            metadata["stderr_tail"] = safe_text(tail, secrets=self._secrets)
            metadata["stderr_truncated"] = truncated
        if protocol_error is not None:
            metadata["protocol_error"] = safe_text(protocol_error, secrets=self._secrets)
        return capture_error(
            error,
            phase=phase or getattr(error, "phase", "mcp.request"),
            context=metadata,
            secrets=self._secrets,
        )

    def _reader(self) -> None:
        failure = None
        try:
            assert self.proc and self.proc.stdout
            # Keep reads efficient while the main thread owns only the raw pipe;
            # shutdown must not wait for a buffered reader's lock.
            output = (
                io.BufferedReader(self.proc.stdout)
                if isinstance(self.proc.stdout, io.RawIOBase)
                else self.proc.stdout
            )
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
        self._request_local.dispatch = "not_started"
        self._request_local.outcome = "not_started"
        try:
            if self._reader_done.is_set() or self._closing:
                raise MCPError(
                    f"{self.name}: {self.error or 'connection closed'} for {method}",
                    phase="mcp.read",
                    method=method,
                    request_id=rid,
                ) from self._reader_failure
            try:
                # A write exception can occur after the peer received the request.
                # Do not guess that a broken pipe means nothing was dispatched.
                self._request_local.dispatch = "sending"
                self._request_local.outcome = "unknown"
                self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            except Exception as error:
                raise MCPError(
                    f"{self.name}: cannot write to server ({safe_text(error, secrets=self._secrets)})",
                    phase="mcp.write",
                    method=method,
                    request_id=rid,
                    dispatch="sending",
                    outcome="unknown",
                ) from error
            self._request_local.dispatch = "sent"
            with self._cv:
                ok = self._cv.wait_for(
                    lambda: (
                        rid in self._pending
                        or self._reader_done.is_set()
                        or self._closing
                        or self.proc is not None
                        and self.proc.poll() is not None
                    ),
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
                phase="mcp.timeout" if not ok else "mcp.read",
                method=method,
                request_id=rid,
                dispatch="sent",
                outcome="unknown",
            ) from self._reader_failure
        self._request_local.dispatch = "response_received"
        if "error" in msg:
            self._request_local.outcome = "returned_error"
            error = msg["error"]
            message = error.get("message") if isinstance(error, dict) else error
            code = error.get("code") if isinstance(error, dict) else None
            raise MCPError(
                f"{self.name}: {safe_text(message or 'server returned an error', secrets=self._secrets)}",
                phase="mcp.response",
                method=method,
                request_id=rid,
                rpc_code=code,
                dispatch="response_received",
                outcome="returned_error",
            )
        if not isinstance(msg.get("result"), dict):
            raise MCPError(
                f"{self.name}: invalid result for {method}",
                phase="mcp.protocol",
                method=method,
                request_id=rid,
                dispatch="response_received",
                outcome="unknown",
            )
        self._request_local.outcome = "returned_error" if msg["result"].get("isError") else "returned_success"
        return msg["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})
        except Exception as error:
            raise MCPError(
                f"{self.name}: cannot send notification {method}", phase="mcp.write", method=method
            ) from error

    # -- protocol ------------------------------------------------------------
    def connect(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout

        def remaining(method):
            left = deadline - time.monotonic()
            if left <= 0:
                raise MCPError(
                    f"{self.name}: handshake deadline exceeded", phase="mcp.timeout", method=method
                )
            return left

        self.start()
        res = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "virgo", "version": __version__},
            },
            remaining("initialize"),
        )
        self.server_info = res.get("serverInfo", {})
        self.notify("notifications/initialized")
        tools, cursor, cursors = [], None, set()
        for _ in range(MAX_TOOL_PAGES):
            page = self.request("tools/list", {"cursor": cursor} if cursor else {}, remaining("tools/list"))
            listed = page.get("tools", [])
            if not isinstance(listed, list):
                raise MCPError(
                    f"{self.name}: invalid tools/list result",
                    phase="mcp.protocol",
                    method="tools/list",
                    request_id=self._request_local.id,
                )
            tools.extend(
                t for t in listed if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"]
            )
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in cursors:
                raise MCPError(
                    f"{self.name}: invalid or repeated tools/list cursor",
                    phase="mcp.protocol",
                    method="tools/list",
                    request_id=self._request_local.id,
                    dispatch="response_received",
                    outcome="unknown",
                )
            cursors.add(cursor)
        else:
            raise MCPError(
                f"{self.name}: tools/list exceeded the pagination limit",
                phase="mcp.protocol",
                method="tools/list",
                request_id=self._request_local.id,
                dispatch="response_received",
                outcome="unknown",
            )
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
                api.report_error(e, phase="mcp.config", context={"path": str(f)}, notify=False)
                api.ui.print(
                    api.t("features.mcp.invalid_config", path=f, error=safe_text(e)), "error"
                )
    from_settings = api.settings.get("mcpServers", {}) or {}
    if isinstance(from_settings, dict):
        servers.update(from_settings)
    else:
        api.report_error("mcpServers must be an object", phase="mcp.config", notify=False)
        api.ui.print(api.t("features.mcp.invalid_settings"), "error")
    return {
        k: v for k, v in servers.items() if isinstance(v, dict) and v.get("command") and not v.get("disabled")
    }


def _schema_shape(schema: dict, depth=0) -> None:
    """Validate only shapes used by Virgo's small argument validator."""
    if not isinstance(schema, dict) or depth > 32:
        raise ValueError("inputSchema must contain bounded object schemas")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not types or any(
            not isinstance(value, str)
            or value
            not in {
                "string",
                "integer",
                "number",
                "boolean",
                "object",
                "array",
                "null",
            }
            for value in types
        ):
            raise ValueError("inputSchema type must contain supported type names")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("inputSchema properties must be an object")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
        raise ValueError("inputSchema required must be a list of names")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise ValueError("inputSchema enum must be a list")
    for key in ("minLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError(f"inputSchema {key} must be a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise ValueError(f"inputSchema {key} must be a finite number")
    for child in properties.values():
        _schema_shape(child, depth + 1)
    if "items" in schema:
        _schema_shape(schema["items"], depth + 1)


def _schemas(client: MCPClient) -> dict[str, str]:
    result, names = {}, set()
    try:
        for spec in client.tools:
            if not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not spec["name"]:
                raise ValueError("each tool must have a nonempty name")
            name = tool_name(client.name, spec["name"])
            if name in names:
                raise ValueError("tool names collide after namespacing")
            names.add(name)
            if spec.get("description") is not None and not isinstance(spec["description"], str):
                raise ValueError("tool description must be text")
            schema = spec.get("inputSchema")
            if schema is not None:
                _schema_shape(schema)
                if schema.get("type", "object") != "object":
                    raise ValueError("a tool's inputSchema must describe an object")
            result[spec["name"]] = json.dumps(
                {
                    "description": spec.get("description") or spec["name"],
                    "inputSchema": spec.get("inputSchema") or {"type": "object", "properties": {}},
                },
                sort_keys=True,
                ensure_ascii=False,
            )
    except (TypeError, ValueError) as error:
        raise MCPError(
            f"{client.name}: invalid tools/list schema ({error})",
            phase="mcp.schema",
            method="tools/list",
            request_id=getattr(client._request_local, "id", None),
            dispatch="response_received",
            outcome="unknown",
        ) from error
    return result


@dataclass
class _Connection:
    name: str
    config: dict
    client: MCPClient
    generation: int = 1
    inflight: int = 0
    reconnecting: bool = False
    retired: bool = False
    attempts_without_progress: int = 0
    pending: MCPClient | None = None
    pending_context: ToolContext | None = None
    recovery_error: dict | None = None
    registered: set[str] = field(default_factory=set)
    wrappers: dict[str, AgentTool] = field(default_factory=dict)
    schemas: dict[str, str] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class MCPConnections:
    """Generation-bound dispatch handles, with bounded, explicit recovery.

    Reconnection never replays a tools/call. Recovered clients are published only
    by refresh(), which the harness calls between completed tool batches and the
    next model request. An active response keeps its old schema and transport;
    stale direct handles are rejected once their owning response has ended.
    """

    def __init__(self, api: ExtensionAPI):
        self.api = api
        self.clients: dict[str, MCPClient] = {}
        self.connections: dict[str, _Connection] = {}
        self._registry_lock = threading.RLock()
        self._revision = 0
        self._seen = weakref.WeakKeyDictionary()
        self._generations: dict[str, int] = {}
        self._retired: list[_Connection] = []
        self._closed = False

    @staticmethod
    def _holds_snapshot(slot: _Connection, runtime, remote: str | None = None) -> bool:
        return runtime is not None and runtime.agent.running and any(
            slot.wrappers.get(tool.name) is tool
            and (remote is None or tool.name == tool_name(slot.name, remote))
            for tool in runtime.tools
        )

    def _retire(self, slot: _Connection) -> None:
        """Called under registry/slot locks after a replacement is published."""
        slot.retired = True
        slot.pending = slot.pending_context = None
        self._retired.append(slot)

    def reap(self, *, boundary=None) -> None:
        """Retain only old generations still used by another active response."""
        closing = []
        with self._registry_lock:
            for slot in tuple(self._retired):
                with slot.lock:
                    if slot.inflight or any(
                        runtime is not boundary and self._holds_snapshot(slot, runtime)
                        for runtime in self._seen
                    ):
                        continue
                    self._retired.remove(slot)
                    # Revoke the lease before closing outside the lock. A stale
                    # direct handle cannot borrow a runtime's later active turn.
                    slot.wrappers.clear()
                    closing.append(slot.client)
        for client in closing:
            client.close()

    def on_agent_event(self, event, runtime) -> None:
        if event.type in {"turn_end", "agent_end"}:
            # These events also cover refusal, error and cancellation without a
            # later model request. No tool from this response can run afterward.
            self.reap(boundary=runtime)

    def _client(self, name: str, conf: dict, generation: int = 1, *, cwd=None) -> MCPClient:
        client = MCPClient(
            name, conf["command"], conf.get("args"), conf.get("env"), cwd or self.api.cwd
        )
        client.generation = generation
        return client

    def _report(self, client: MCPClient, error, ctx: ToolContext, *, phase=None, context=None) -> dict:
        runtime = self.api.runtime_for_context(ctx)
        owner = (
            (runtime.store if runtime.store is not None else runtime.accounting_store)
            if runtime is not None
            else self.api.current_store()
        )
        metadata = {
            **(runtime.diagnostic_context() if runtime is not None else {}),
            "agent": ctx.agent,
            "tool_call_id": ctx.call_id,
            **(context or {}),
        }
        details = client.diagnostic(error, phase=phase, context=metadata)
        details = self.api.report_error(details, phase=details["phase"], store=owner, notify=False)
        # Direct extension callers need the same scope as the normal loop.
        access = getattr(runtime, "diagnostic_access", None)
        if access is not None:
            access.remember(details)
        return details

    @staticmethod
    def _blocked(slot: _Connection, reason: str) -> ToolResult:
        return ToolResult.error(
            reason,
            details={
                "mcp": True,
                "transport": {
                    "protocol": "mcp",
                    "server": slot.name,
                    "generation": slot.generation,
                    "dispatch": "not_started",
                    "outcome": "not_started",
                    "side_effects": "none",
                },
            },
        )

    def _execute(
        self, slot: _Connection, remote: str, schema: str, args: dict, ctx: ToolContext
    ) -> ToolResult:
        with slot.lock:
            if self._closed:
                return self._blocked(slot, ctx.t("features.mcp.retired"))
            runtime = self.api.runtime_for_context(ctx)
            leased = self._holds_snapshot(slot, runtime, remote)
            if slot.retired and not leased:
                return self._blocked(slot, ctx.t("features.mcp.schema_changed"))
            if (slot.reconnecting or slot.pending is not None) and not leased:
                return self._blocked(slot, ctx.t("features.mcp.recovering"))
            if slot.schemas.get(remote) != schema:
                return self._blocked(slot, ctx.t("features.mcp.schema_changed"))
            client = slot.client
            slot.inflight += 1
        try:
            metadata = {
                "server": slot.name,
                "method": "tools/call",
                "mcp_tool": remote,
                "tool_name": tool_name(slot.name, remote),
                "generation": client.generation,
            }
            try:
                result = to_result(client.call(remote, args))
                if result.is_error:
                    metadata["request_id"] = getattr(client._request_local, "id", None)
                    result.details["error"] = self._report(
                        client,
                        "MCP server reported a tool error.",
                        ctx,
                        phase="mcp.tool",
                        context=metadata,
                    )
                else:
                    with slot.lock:
                        slot.attempts_without_progress = 0
                        slot.recovery_error = None
            except Exception as error:
                details = self._report(
                    client, error, ctx, phase=getattr(error, "phase", "mcp.tool"), context=metadata
                )
                result = ToolResult.error(
                    safe_text(error, secrets=client._secrets), {"mcp": True, "error": details}
                )
            result.details["transport"] = client.transport()
            if result.is_error:
                with slot.lock:
                    client.error_details = result.details["error"]
            return result
        finally:
            with slot.lock:
                slot.inflight -= 1

    def _prepare_tools(self, slot: _Connection, client: MCPClient, schemas: dict) -> list[AgentTool]:
        seen = {
            tool.name
            for role in ROLES_ALL
            for tool in self.api.tools_for(role)
            if slot.wrappers.get(tool.name) is not tool
        }
        prepared = []
        for spec in client.tools:
            name = tool_name(slot.name, spec["name"])
            if name in seen:
                raise MCPError(
                    f"{slot.name}: tool name {name} conflicts with an active tool",
                    phase="mcp.schema",
                    method="tools/list",
                    dispatch="response_received",
                    outcome="unknown",
                )
            schema = deepcopy(spec.get("inputSchema") or {"type": "object", "properties": {}})

            def execute(args, ctx, _slot=slot, _tool=spec["name"], _schema=schemas[spec["name"]]):
                return self._execute(_slot, _tool, _schema, args, ctx)

            desc = (spec.get("description") or spec["name"]).strip()
            prepared.append(AgentTool(name, f"[MCP {slot.name}] {desc}", schema, execute))
            seen.add(name)
        return prepared

    def _publish(
        self, slot: _Connection, client: MCPClient, schemas: dict, prepared: list[AgentTool]
    ) -> None:
        # Publish only our owned namespace. The harness takes an immutable tool
        # snapshot for each request/batch; no plugin writes its registry directly.
        tools = [
            tool for name, connection in self.connections.items() if name != slot.name
            for tool in connection.wrappers.values()
        ]
        self.api.replace_tools("servers", [(tool, ROLES_ALL) for tool in [*tools, *prepared]])
        slot.wrappers = {tool.name: tool for tool in prepared}
        slot.registered = set(slot.wrappers)
        slot.client = client
        slot.generation = client.generation
        slot.schemas = schemas
        self.connections[slot.name] = slot
        self.clients[slot.name] = client
        self._generations[slot.name] = client.generation

    def connect_all(self, config: dict) -> None:
        owner = self.api.current_store()

        def one(item):
            name, conf = item
            client = self._client(name, conf, self._generations.get(name, 0) + 1)
            schemas = {}
            try:
                client.connect(timeout=float(conf.get("timeout", 30)))
                schemas = _schemas(client)
            except Exception as error:
                client.error = safe_text(error, secrets=client._secrets) or type(error).__name__
                client.close()
                client.error_details = client.diagnostic(
                    error,
                    phase=getattr(error, "phase", "mcp.connect" if client.proc else "mcp.start"),
                )
            return name, conf, client, schemas

        if config:
            with ThreadPoolExecutor(max_workers=min(8, len(config))) as pool:
                for name, conf, client, schemas in pool.map(one, config.items()):
                    slot = _Connection(
                        name, deepcopy(conf), client, generation=client.generation, schemas=schemas
                    )
                    self.connections[name] = slot
                    self.clients[name] = client
                    self._generations[name] = client.generation
                    if client.error:
                        client.error_details = self.api.report_error(
                            client.error_details,
                            phase="mcp.connect",
                            store=owner,
                        )
                    else:
                        try:
                            prepared = self._prepare_tools(slot, client, schemas)
                            self._publish(slot, client, schemas, prepared)
                        except Exception as error:
                            client.error = safe_text(error, secrets=client._secrets)
                            client.close()
                            client.error_details = self.api.report_error(
                                client.diagnostic(error, phase="mcp.schema"),
                                phase="mcp.schema",
                                store=owner,
                            )

    def status(self, args: dict, ctx: ToolContext) -> ToolResult:
        server = args.get("server")
        runtime = self.api.runtime_for_context(ctx)
        if runtime is None:
            return ToolResult.error(ctx.t("features.mcp.status_runtime"))
        access = getattr(runtime, "diagnostic_access", None)
        with self._registry_lock:
            slots = (
                ([self.connections[server]] if server in self.connections else [])
                if server
                else list(self.connections.values())
            )
        if server and not slots:
            return ToolResult.error(ctx.t("features.mcp.no_server"))
        rows = []
        for slot in slots:
            with slot.lock:
                client = slot.client
                ready = not client.error and not client._reader_done.is_set() and not client._closing
                row = {
                    "server": slot.name,
                    "generation": slot.generation,
                    "state": "reconnecting"
                    if slot.reconnecting
                    else "refresh_required"
                    if slot.pending
                    else "ready"
                    if ready
                    else "unavailable",
                    "inflight": slot.inflight,
                    "tools": len(slot.schemas),
                    "reconnect_attempts_remaining": max(
                        0, MAX_RECONNECT_ATTEMPTS - slot.attempts_without_progress
                    ),
                }
                error = slot.recovery_error or client.error_details
                visible = access.get(error["error_id"]) if access is not None and error else None
                if visible:
                    row["error_id"] = visible["error_id"]
                rows.append(row)
        return ToolResult.text(
            json.dumps({"servers": rows}, ensure_ascii=False), details={"mcp_status": rows}
        )

    def reconnect(self, args: dict, ctx: ToolContext) -> ToolResult:
        runtime = self.api.runtime_for_context(ctx)
        if runtime is None:
            return ToolResult.error(ctx.t("features.mcp.recovery_runtime"))
        access = getattr(runtime, "diagnostic_access", None)
        error = access.get(args["error_id"]) if access is not None else None
        if error is None:
            return ToolResult.error(ctx.t("features.mcp.evidence_missing"))
        server = args["server"]
        context = error.get("context", {})
        if context.get("server") != server or not str(error.get("phase", "")).startswith("mcp."):
            return ToolResult.error(ctx.t("features.mcp.evidence_mismatch"))
        with self._registry_lock:
            slot = self.connections.get(server)
        if slot is None:
            return ToolResult.error(ctx.t("features.mcp.no_server"))
        with slot.lock:
            if slot.retired or context.get("generation") != slot.generation:
                return self._blocked(slot, ctx.t("features.mcp.old_connection"))
            if slot.reconnecting or slot.pending is not None:
                return self._blocked(slot, ctx.t("features.mcp.pending"))
            if slot.inflight:
                return self._blocked(slot, ctx.t("features.mcp.inflight"))
            if ctx.signal.is_set():
                return self._blocked(slot, ctx.t("features.mcp.cancelled"))
            if slot.attempts_without_progress >= MAX_RECONNECT_ATTEMPTS:
                return self._blocked(slot, ctx.t("features.mcp.budget"))
            slot.reconnecting = True
            slot.attempts_without_progress += 1
            generation = slot.generation
            conf = slot.config
        candidate = None
        try:
            candidate = self._client(server, conf, generation + 1, cwd=slot.client.cwd)
            candidate.connect(timeout=float(conf.get("timeout", 30)))
            schemas = _schemas(candidate)
            # Build every closure and check active-name conflicts before an old
            # connection or registered tool can be replaced.
            with self._registry_lock:
                self._prepare_tools(slot, candidate, schemas)
            if ctx.signal.is_set():
                candidate.close()
                return self._blocked(slot, ctx.t("features.mcp.cancelled_retained"))
            with slot.lock:
                if slot.retired:
                    candidate.close()
                    return self._blocked(slot, ctx.t("features.mcp.removed"))
                # Zero active calls does not mean zero response leases: a model
                # may still be generating, or a batch may be between calls.
                # Publish a new slot at the next boundary for every recovery.
                slot.pending = candidate
                slot.pending_context = ctx
                slot.recovery_error = None
            state = {
                "server": server,
                "generation": candidate.generation,
                "state": "refresh_required",
                "refresh_required": True,
                "replayed_calls": 0,
            }
            return ToolResult.text(
                json.dumps(state) + ctx.t("features.mcp.no_replay"),
                details={"mcp_recovery": state},
            )
        except Exception as failure:
            if candidate is not None:
                candidate.close()
            diagnostic_client = candidate or slot.client
            transport = (
                candidate.transport()
                if candidate is not None
                else {
                    "protocol": "mcp",
                    "server": server,
                    "generation": generation + 1,
                    "dispatch": "not_started",
                    "outcome": "not_started",
                    "side_effects": "none",
                }
            )
            if isinstance(failure, MCPError):
                transport.update(
                    dispatch=failure.dispatch,
                    outcome=failure.outcome,
                    side_effects="none" if failure.dispatch == "not_started" else "unknown",
                )
            details = self._report(
                diagnostic_client,
                failure,
                ctx,
                phase=getattr(failure, "phase", "mcp.reconnect"),
                context={
                    **transport,
                    "server": server,
                    "generation": generation,
                    "candidate_generation": generation + 1,
                    "tool_name": "mcp_reconnect",
                    "related_error_id": error["error_id"],
                },
            )
            with slot.lock:
                slot.recovery_error = details
            return ToolResult.error(
                ctx.t("features.mcp.failed"),
                details={
                    "mcp": True,
                    "error": details,
                    "transport": transport,
                },
            )
        finally:
            with slot.lock:
                slot.reconnecting = False

    def refresh(self, runtime) -> bool:
        """Publish pending clients at a safe boundary; the harness refreshes its own runtime."""
        old_clients = []
        with self._registry_lock:
            for slot in list(self.connections.values()):
                with slot.lock:
                    candidate = slot.pending
                    if candidate is None or slot.reconnecting or slot.inflight or slot.retired:
                        continue
                    try:
                        schemas = _schemas(candidate)
                        replacement = _Connection(
                            slot.name, deepcopy(slot.config), candidate, generation=candidate.generation,
                            schemas=schemas, wrappers=dict(slot.wrappers),
                            attempts_without_progress=slot.attempts_without_progress,
                        )
                        prepared = self._prepare_tools(replacement, candidate, schemas)
                        self._publish(replacement, candidate, schemas, prepared)
                    except Exception as error:
                        ctx = slot.pending_context
                        slot.pending = slot.pending_context = None
                        old_clients.append(candidate)
                        if ctx is not None:
                            details = self._report(
                                candidate,
                                error,
                                ctx,
                                phase="mcp.schema",
                                context={
                                    "server": slot.name,
                                    "generation": slot.generation,
                                    "candidate_generation": candidate.generation,
                                    "tool_name": "mcp_reconnect",
                                },
                            )
                            slot.recovery_error = details
                            owner = self.api.runtime_for_context(ctx)
                            if owner is not None:
                                owner.recovery.safely("queue", details)
                        continue
                    self._retire(slot)
                    self._revision += 1
            changed = self._seen.get(runtime, 0) != self._revision
            self._seen[runtime] = self._revision
        for old in old_clients:
            old.close()
        self.reap(boundary=runtime)
        return changed

    def reload(self, config: dict) -> None:
        """Validate all replacements before publishing or closing any healthy connection."""
        with self._registry_lock:
            slots = list(self.connections.values())
            candidates = []
            replacements = {}
            locked = []
            try:
                for name, conf in config.items():
                    client = self._client(name, conf, self._generations.get(name, 0) + 1)
                    candidates.append(client)
                    client.connect(timeout=float(conf.get("timeout", 30)))
                    schemas = _schemas(client)
                    previous = self.connections.get(name)
                    slot = _Connection(name, deepcopy(conf), client, generation=client.generation,
                                       schemas=schemas,
                                       wrappers=dict(previous.wrappers) if previous is not None else {})
                    prepared = self._prepare_tools(slot, client, schemas)
                    slot.wrappers = {tool.name: tool for tool in prepared}
                    slot.registered = set(slot.wrappers)
                    replacements[name] = slot
                # A child may have started a call during the handshakes. Reserve
                # all old handles and fail without changing anything if so.
                for slot in slots:
                    slot.lock.acquire()
                    locked.append(slot)
                if any(slot.inflight or slot.reconnecting or slot.pending is not None for slot in slots):
                    raise ValueError(self.api.t("features.mcp.inflight"))
                self.api.replace_tools("servers", [
                    (tool, ROLES_ALL) for slot in replacements.values() for tool in slot.wrappers.values()
                ])
                for slot in slots:
                    self._retire(slot)
                self.connections = replacements
                self.clients.clear()
                self.clients.update({name: slot.client for name, slot in replacements.items()})
                self._generations.update({name: slot.generation for name, slot in replacements.items()})
                self._revision += 1
            except BaseException:
                for client in candidates:
                    client.close()
                raise
            finally:
                for slot in reversed(locked):
                    slot.lock.release()
        self.reap()

    def close(self) -> None:
        closing = []
        with self._registry_lock:
            self._closed = True
            for slot in [*self.connections.values(), *self._retired]:
                with slot.lock:
                    slot.retired = True
                    closing.extend(client for client in (slot.client, slot.pending) if client is not None)
            self._retired.clear()
        for client in closing:
            client.close()


def setup(api: ExtensionAPI) -> None:
    manager = MCPConnections(api)
    api.services["mcp"] = manager.clients
    api.on_cleanup(manager.close)
    api.on("runtime_request", manager.refresh)
    api.on("agent_event", manager.on_agent_event)
    manager.connect_all(load_config(api))
    api.register_tool(
        AgentTool(
            "mcp_status",
            "Read local MCP connection state, generation, pending calls and accessible diagnostic IDs. Does not contact servers.",
            {"type": "object", "properties": {"server": {"type": "string"}}, "additionalProperties": False},
            manager.status,
            read_only=True,
            i18n_key="tools.mcp_status",
        ),
        roles=ROLES_ALL,
    )
    api.register_tool(
        AgentTool(
            "mcp_reconnect",
            "Reconnect one configured MCP server using an accessible diagnostic ID. Starts its configured process; never replays failed calls. "
            "Only use after checking the failure and previous operation's outcome; in-flight calls and repeated recovery without progress block this action.",
            {
                "type": "object",
                "properties": {"server": {"type": "string"}, "error_id": {"type": "string"}},
                "required": ["server", "error_id"],
                "additionalProperties": False,
            },
            manager.reconnect,
            i18n_key="tools.mcp_reconnect",
        ),
        roles=ROLES_ALL,
    )

    def cmd(_session, args):
        if args.strip() == "reconnect":
            manager.reload(load_config(api))
            api.ui.print(api.t("features.mcp.reconnected"))
        if not manager.clients:
            api.ui.print(api.t("features.mcp.empty"))
            return
        for client in manager.clients.values():
            state = (
                api.t("features.mcp.error", error=client.error)
                if client.error
                else api.t("features.mcp.tools", count=len(client.tools))
            )
            api.ui.print(f"  {client.name}: {state}")

    api.register_command(
        "mcp",
        cmd,
        "show MCP servers (/mcp reconnect)",
        argument_hint="[reconnect]",
        description_key="features.commands.mcp",
    )
