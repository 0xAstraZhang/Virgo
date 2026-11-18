"""MCP extension: connect configured stdio servers and expose their tools.

Install this file as ``~/.virgo/extensions/mcp.py`` (or ``.virgo/extensions/mcp.py``
in a trusted project) and install the optional SDK dependency::

    uv tool install 'virgo[mcp]'      # installed distribution
    uv sync --extra mcp               # source checkout

Servers are configured in ``~/.virgo/mcp.json`` or ``.virgo/mcp.json``, or under
the ``mcpServers`` setting, using the common format::

    {"mcpServers": {"github": {"command": "npx",
                               "args": ["-y", "@modelcontextprotocol/server-github"],
                               "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}}

Tools become ``mcp__<server>__<tool>``. ``/mcp`` shows connection state and
``/mcp reconnect`` reloads the configuration. The extension uses the official
Model Context Protocol Python SDK (``mcp``); it does not implement the protocol.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy
from pathlib import Path

from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import Implementation

from virgo import __version__
from virgo.agent import AgentTool, ToolResult
from virgo.ai.types import ImageContent, TextContent
from virgo.diagnostics import safe_text

CALL_TIMEOUT = 300.0
MAX_TOOL_PAGES = 100
_STDERR_LIMIT = 8192
_SECRET_NAME = re.compile(r"token|password|secret|credential|api[-_]?key|authorization", re.I)


class MCPConnectionError(Exception):
    pass


class MCPCallCancelled(Exception):
    pass


def _expand(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda match: os.environ.get(match.group(1), ""), value)


def _secrets(config: dict) -> tuple[str, ...]:
    """Values that must not reach diagnostics or the terminal."""
    values = [str(value) for value in (config.get("env") or {}).values()]
    args = [str(value) for value in config.get("args") or []]
    sensitive = []
    previous = ""
    for value in args:
        if _SECRET_NAME.search(previous):
            sensitive.append(value)
        name, separator, data = value.partition("=")
        if separator and _SECRET_NAME.search(name):
            sensitive.append(data)
        previous = value if value.startswith("-") else ""
    return tuple(_expand(value) for value in [*values, *sensitive])


class _Connection:
    """One stdio server, owned by a dedicated asyncio loop thread.

    Entering and leaving the SDK contexts happens in the same loop task; calls
    are submitted to that loop from tool threads. A retired connection keeps
    accepting its in-flight calls and closes when the last one returns.
    """

    def __init__(self, name: str, config: dict, cwd: Path):
        self.name = name
        self.config = config
        self.cwd = cwd
        self.tools: list = []
        self.error: Exception | None = None
        self.inflight = 0
        self.retired = False
        self._closed = False
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"mcp-{name}")
        self._thread.start()
        self._owner = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._done = threading.Event()
        self._session: ClientSession | None = None
        self._stderr_read, stderr_write = os.pipe()
        self._stderr = os.fdopen(stderr_write, "w", encoding="utf-8", errors="replace")
        self._stderr_buffer = bytearray()
        self._stderr_lock = threading.Lock()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self._stderr_read,), daemon=True, name=f"mcp-{name}-stderr"
        )
        self._stderr_thread.start()
        self._secrets = _secrets(config)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _drain_stderr(self, fd: int) -> None:
        """Keep only a bounded tail; a noisy server must not grow a file forever."""
        try:
            with os.fdopen(fd, "rb", buffering=0) as stream:
                while chunk := stream.read(4096):
                    with self._stderr_lock:
                        self._stderr_buffer.extend(chunk)
                        if len(self._stderr_buffer) > _STDERR_LIMIT:
                            del self._stderr_buffer[:-_STDERR_LIMIT]
        except (OSError, ValueError):
            pass

    async def _serve(self) -> None:
        stream = None
        session = None
        try:
            stream = stdio_client(self._params(), errlog=self._stderr)
            read, write = await stream.__aenter__()
            session = ClientSession(
                read, write, client_info=Implementation(name="virgo", version=__version__)
            )
            await session.__aenter__()
            await session.initialize()
            self._session = session
            self.tools = await self._list_tools(session)
            self._stop = asyncio.Event()
            self._ready.set()
            await self._stop.wait()
        except BaseException as error:  # the handshake failed or the server closed
            self.error = error
            self._ready.set()
        finally:
            if session is not None:
                try:
                    await session.__aexit__(None, None, None)
                except BaseException:
                    pass
            if stream is not None:
                try:
                    await stream.__aexit__(None, None, None)
                except BaseException:
                    pass
            self._done.set()

    def _params(self) -> StdioServerParameters:
        env = {
            **os.environ,
            **{key: _expand(str(value)) for key, value in (self.config.get("env") or {}).items()},
        }
        return StdioServerParameters(
            command=_expand(str(self.config["command"])),
            args=[_expand(str(value)) for value in self.config.get("args") or []],
            env=env,
            cwd=str(self.cwd),
        )

    @staticmethod
    async def _list_tools(session: ClientSession) -> list:
        tools: list = []
        cursor = None
        for _ in range(MAX_TOOL_PAGES):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_tools(params=params)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools
        raise MCPConnectionError("tools/list exceeded the pagination limit")

    def connect(self, timeout: float) -> None:
        self._owner = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        if not self._ready.wait(timeout):
            self.close()
            raise MCPConnectionError(f"handshake timed out after {timeout:g}s")
        if self.error is not None:
            stderr = self.stderr_tail()
            self.close()
            detail = safe_text(self.error, secrets=self._secrets)
            if stderr:
                detail += f"\n{stderr}"
            raise MCPConnectionError(detail)

    @property
    def ready(self) -> bool:
        """The handshake finished and the transport has not exited or closed."""
        return (
            self._session is not None
            and self.error is None
            and not self._closed
            and not self._done.is_set()
        )

    def stderr_tail(self) -> str:
        """The server's bounded stderr tail, with configured secrets redacted."""
        with self._stderr_lock:
            text = bytes(self._stderr_buffer).decode("utf-8", errors="replace")
        return safe_text(text, secrets=self._secrets) if text else ""

    def call(self, tool: str, arguments: dict, signal) -> types.CallToolResult:
        with self._lock:
            if self._closed:
                raise MCPConnectionError(f"{self.name}: connection closed")
            if self.retired:
                raise MCPConnectionError(
                    f"{self.name}: connection replaced; retry with the refreshed tool set"
                )
            self.inflight += 1
        try:
            assert self._session is not None
            future = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(tool, arguments), self._loop
            )
            deadline = time.monotonic() + CALL_TIMEOUT
            while True:
                if signal is not None and signal.is_set():
                    future.cancel()
                    raise MCPCallCancelled(f"{self.name}: cancelled {tool}")
                try:
                    return future.result(0.05)
                except FutureTimeout:
                    if time.monotonic() >= deadline:
                        future.cancel()
                        error = TimeoutError(
                            f"{self.name}: tool call timed out after {CALL_TIMEOUT:g}s"
                        )
                        with self._lock:
                            self.error = error
                        raise error from None
                except Exception as error:
                    # A closed transport surfaces only when a request touches it.
                    with self._lock:
                        if not self._closed:
                            self.error = error
                    raise
        finally:
            with self._lock:
                self.inflight -= 1
                close = self.retired and self.inflight == 0
            if close:
                self.close()

    def retire(self) -> None:
        with self._lock:
            self.retired = True
            close = self.inflight == 0
        if close:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._owner is not None:
            if self._stop is not None:
                self._loop.call_soon_threadsafe(self._stop.set)
            elif self._owner.cancel():
                self._done.set()  # Never started; no transport cleanup to await.
            # The future is marked cancelled before the coroutine unwinds. Wait
            # for the cleanup signal so the SDK exits its transports before the
            # loop that owns them stops.
            self._done.wait(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        try:
            self._loop.close()
        except RuntimeError:
            pass
        try:
            self._stderr.close()
        except (OSError, ValueError):
            pass
        self._stderr_thread.join(2)


def _norm(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


def tool_name(server: str, tool: str) -> str:
    return f"mcp__{_norm(server)}__{_norm(tool)}"[:64]


def _valid_schema(schema: dict, depth: int = 0) -> bool:
    """Reject shapes the harness's small argument validator cannot read."""
    if not isinstance(schema, dict) or depth > 32:
        return False
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not names or any(
            not isinstance(name, str)
            or name not in {"string", "integer", "number", "boolean", "object", "array", "null"}
            for name in names
        ):
            return False
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return False
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        return False
    if "enum" in schema and not isinstance(schema["enum"], list):
        return False
    for key in ("minLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            return False
    for key in ("minimum", "maximum"):
        if key in schema and (
            type(schema[key]) not in (int, float) or not math.isfinite(schema[key])
        ):
            return False
    if "items" in schema and not _valid_schema(schema["items"], depth + 1):
        return False
    return all(_valid_schema(child, depth + 1) for child in properties.values())


def _content(result: types.CallToolResult) -> list:
    content: list = []
    for item in result.content:
        if item.type == "text":
            content.append(TextContent(item.text))
        elif item.type == "image":
            content.append(ImageContent(item.data, item.mime_type))
        elif item.type == "audio":
            content.append(TextContent(f"[audio {item.mime_type}]"))
        elif item.type == "resource":
            resource = item.resource
            content.append(TextContent(getattr(resource, "text", None) or f"[resource {resource.uri}]"))
        elif item.type == "resource_link":
            content.append(TextContent(f"[resource {item.uri}]"))
        else:
            payload = item.model_dump(mode="json", by_alias=True, exclude_none=True)
            content.append(TextContent(json.dumps(payload, ensure_ascii=False)))
    if not content and result.structured_content is not None:
        content.append(TextContent(json.dumps(result.structured_content, ensure_ascii=False)))
    return content or [TextContent("(no content)")]


class _Manager:
    def __init__(self, api):
        self.api = api
        self.connections: dict[str, _Connection] = {}
        self.failures: dict[str, str] = {}
        self.configured: list[str] = []
        self.retired: list[_Connection] = []

    def load_config(self) -> dict:
        servers: dict = {}
        for path in self.api.paths.both("mcp.json"):
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                listed = data.get("mcpServers", {}) if isinstance(data, dict) else None
                if not isinstance(listed, dict):
                    raise ValueError("mcpServers must be an object")
                servers.update(listed)
            except ValueError as error:
                self.api.ui.print(f"[mcp: invalid {path}: {safe_text(error)}]", "error")
        from_settings = self.api.settings.get("mcpServers", {}) or {}
        if isinstance(from_settings, dict):
            servers.update(from_settings)
        else:
            self.api.ui.print("[mcp: invalid settings: mcpServers must be an object]", "error")
        return {
            name: config
            for name, config in servers.items()
            if isinstance(config, dict) and config.get("command") and not config.get("disabled")
        }

    def _connect(self, name: str, config: dict) -> _Connection:
        connection = _Connection(name, config, self.api.cwd)
        connection.connect(timeout=float(config.get("timeout", 30)))
        return connection

    def _connect_all(self, config: dict) -> tuple[dict[str, _Connection], dict[str, str]]:
        connections: dict[str, _Connection] = {}
        failures: dict[str, str] = {}
        if not config:
            return connections, failures

        def one(item):
            name, server = item
            try:
                return name, self._connect(name, server), None
            except Exception as error:
                self.api.report_error(error, phase="mcp.connect", context={"server": name}, notify=False)
                return name, None, safe_text(error)

        with ThreadPoolExecutor(max_workers=min(8, len(config))) as pool:
            for name, connection, failure in pool.map(one, config.items()):
                if connection is not None:
                    connections[name] = connection
                else:
                    failures[name] = failure or "connection failed"
        return connections, failures

    def _publish(self) -> None:
        entries = []
        seen: set[str] = set()
        for connection in self.connections.values():
            for spec in connection.tools:
                name = tool_name(connection.name, spec.name)
                if not spec.name or name in seen:
                    continue
                if (
                    not _valid_schema(spec.input_schema)
                    or spec.input_schema.get("type", "object") != "object"
                ):
                    self.api.ui.print(
                        f"[mcp: {connection.name}/{spec.name} has an unsupported input schema; skipped]",
                        "warn",
                    )
                    continue
                seen.add(name)
                schema = deepcopy(spec.input_schema or {"type": "object", "properties": {}})

                def execute(args, ctx, _connection=connection, _tool=spec.name, _name=name):
                    try:
                        result = _connection.call(_tool, args, ctx.signal)
                    except MCPCallCancelled as error:
                        return ToolResult.error(str(error))
                    except Exception as error:
                        return ToolResult.error(
                            f"{_connection.name}: {safe_text(error, secrets=_connection._secrets)}"
                        )
                    return ToolResult(
                        _content(result),
                        details={"mcp": True, "server": _connection.name, "tool": _name},
                        is_error=bool(result.is_error),
                    )

                description = (spec.description or spec.name).strip()
                annotations = getattr(spec, "annotations", None)
                read_only = getattr(annotations, "read_only_hint", None) is True
                entries.append(
                    (
                        AgentTool(
                            name,
                            f"[MCP {connection.name}] {description}",
                            schema,
                            execute,
                            side_effect="read" if read_only else "write",
                        ),
                        ("main", "subagent"),
                    )
                )
        self.api.replace_tools("mcp", entries)

    def start(self) -> None:
        config = self.load_config()
        self.configured = list(config)
        self.connections, self.failures = self._connect_all(config)
        self._publish()

    def reconnect(self) -> bool:
        config = self.load_config()
        candidates, failures = self._connect_all(config)
        if failures:
            for connection in candidates.values():
                connection.close()
            self.configured = list(config)
            self.failures = failures
            return False
        previous = self.connections
        self.connections = candidates
        self.configured = list(config)
        self.failures = {}
        self._publish()
        for connection in previous.values():
            self.retired.append(connection)
            connection.retire()
        return True

    def status(self) -> list[dict]:
        rows = []
        for name in dict.fromkeys([*self.configured, *self.connections, *self.failures]):
            connection = self.connections.get(name)
            if connection is not None and connection.ready:
                rows.append(
                    {"server": name, "state": "ready", "tools": len(connection.tools), "error": None}
                )
                continue
            error = self.failures.get(name)
            if error is None and connection is not None and connection.error is not None:
                error = safe_text(connection.error, secrets=connection._secrets)
            rows.append(
                {"server": name, "state": "unavailable", "tools": 0, "error": error or "not connected"}
            )
        return rows

    def status_tool(self, args: dict, ctx) -> ToolResult:
        rows = self.status()
        server = args.get("server")
        if server:
            rows = [row for row in rows if row["server"] == server]
            if not rows:
                return ToolResult.error(f"No configured MCP server named {server!r}.")
        return ToolResult.text(
            json.dumps({"servers": rows}, ensure_ascii=False), details={"mcp_status": rows}
        )

    def command(self, _session, args: str) -> None:
        if args.strip() == "reconnect":
            if self.reconnect():
                self.api.ui.print("MCP servers reconnected.")
            else:
                self.api.ui.print("MCP reconnection failed; previous connections retained.", "error")
                for name, error in self.failures.items():
                    self.api.ui.print(f"  {name}: error: {error}", "error")
        rows = self.status()
        if not rows:
            self.api.ui.print("No MCP servers configured (.virgo/mcp.json).")
            return
        for row in rows:
            state = f"error: {row['error']}" if row["error"] else f"{row['tools']} tools"
            self.api.ui.print(f"  {row['server']}: {state}")

    def close(self) -> None:
        for connection in [*self.connections.values(), *self.retired]:
            connection.close()
        self.connections.clear()
        self.retired.clear()


def setup(api) -> None:
    manager = _Manager(api)
    api.on_cleanup(manager.close)
    manager.start()
    api.register_tool(
        AgentTool(
            "mcp_status",
            "Read local MCP connection state and tool counts. Does not contact servers.",
            {
                "type": "object",
                "properties": {"server": {"type": "string"}},
                "additionalProperties": False,
            },
            manager.status_tool,
            side_effect="read",
        ),
        roles=("main", "subagent"),
    )
    api.register_command(
        "mcp",
        manager.command,
        "show MCP servers (/mcp reconnect)",
        argument_hint="[reconnect]",
    )
