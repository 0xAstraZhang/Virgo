"""MCP stdio transport: JSON-RPC requests, the handshake and bounded diagnostics."""

from __future__ import annotations

import io
import json
import math
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from ... import __version__
from ...agent.types import ToolResult
from ...ai.types import ImageContent, TextContent
from ...diagnostics import capture_error, safe_text

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
        self._cancellations: deque[int] = deque()
        self._cancel_thread: threading.Thread | None = None
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

    def _queue_cancellation(self, rid: int) -> None:
        # A server can stop reading stdin. Never make local cancellation wait
        # for a notification write, and use at most one writer per connection.
        with self._cv:
            if self._closing:
                return
            self._cancellations.append(rid)
            if self._cancel_thread is None:
                self._cancel_thread = threading.Thread(
                    target=self._send_cancellations, daemon=True, name=f"mcp-{self.name}-cancel"
                )
                self._cancel_thread.start()

    def _send_cancellations(self) -> None:
        while True:
            with self._cv:
                if self._closing or not self._cancellations:
                    self._cancellations.clear()
                    self._cancel_thread = None
                    return
                rid = self._cancellations.popleft()
            try:
                self.notify("notifications/cancelled", {"requestId": rid})
            except MCPError:
                pass  # Best effort; local cancellation never proves remote rollback.

    def request(
        self, method: str, params: dict | None = None, timeout: float = 60.0, *, signal=None
    ) -> dict:
        with self._cv:
            self._next += 1
            rid = self._next
            self._waiting.add(rid)
        self._request_local.id = rid
        self._request_local.dispatch = "not_started"
        self._request_local.outcome = "not_started"

        def check_cancelled():
            if signal is not None and signal.is_set():
                dispatch = self._request_local.dispatch
                if dispatch == "sent":
                    self._queue_cancellation(rid)
                raise MCPError(
                    f"{self.name}: cancelled {method}", phase="mcp.cancelled",
                    method=method, request_id=rid, dispatch=dispatch,
                    outcome=self._request_local.outcome,
                )

        try:
            check_cancelled()
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
            deadline = time.monotonic() + timeout
            with self._cv:
                while True:
                    check_cancelled()
                    ok = (
                        rid in self._pending
                        or self._reader_done.is_set()
                        or self._closing
                        or self.proc is not None
                        and self.proc.poll() is not None
                    )
                    remaining = deadline - time.monotonic()
                    if ok or remaining <= 0:
                        break
                    self._cv.wait(min(0.05, remaining) if signal is not None else remaining)
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

    def call(self, tool: str, arguments: dict, timeout: float = 300.0, *, signal=None) -> dict:
        return self.request("tools/call", {"name": tool, "arguments": arguments}, timeout, signal=signal)

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

