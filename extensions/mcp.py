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
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from copy import deepcopy

import anyio
from mcp import ClientSession, types
from mcp.client.stdio import get_default_environment
from mcp.shared.message import SessionMessage
from mcp.types import Implementation
from pydantic import TypeAdapter

from virgo import __version__
from virgo.agent import AgentTool, ToolResult
from virgo.ai.types import ImageContent, TextContent
from virgo.diagnostics import safe_text
from virgo.execution import ProcessRequest, cleanup_process

CALL_TIMEOUT = 300.0
MAX_TOOL_PAGES = 100
_STDERR_LIMIT = 8192
_MESSAGE_LIMIT = 8 * 1024 * 1024
_JSONRPC = TypeAdapter(types.JSONRPCMessage)
_SECRET_NAME = re.compile(r"token|password|secret|credential|api[-_]?key|authorization", re.I)


class MCPConnectionError(Exception):
    pass


class MCPCallCancelled(Exception):
    pass


def _expand(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda match: os.environ.get(match.group(1), ""), value)


def _same_directory(left, right) -> bool:
    try:
        return left.resolve() == right.resolve()
    except (OSError, RuntimeError):
        return False


def _validate_config(config: dict) -> tuple[str, list, dict]:
    command = config.get("command")
    if not isinstance(command, str) or not command:
        raise MCPConnectionError("MCP server command must be a nonempty string")
    args = config.get("args")
    if args is None:
        args = []
    elif not isinstance(args, list):
        raise MCPConnectionError("MCP server args must be a list")
    env = config.get("env")
    if env is None:
        env = {}
    elif not isinstance(env, dict) or any(not isinstance(key, str) for key in env):
        raise MCPConnectionError("MCP server env must be an object with string keys")
    return command, args, env


def _secrets(config: dict) -> tuple[str, ...]:
    """Values that must not reach diagnostics or the terminal."""
    raw_env = config.get("env")
    values = [str(value) for value in raw_env.values()] if isinstance(raw_env, dict) else []
    raw_args = config.get("args")
    args = [str(value) for value in raw_args] if isinstance(raw_args, list) else []
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
    """One immutable execution boundary and one SDK session, never shared by tasks."""

    def __init__(self, name: str, config: dict, context, signal=None, *, on_closed=None):
        self.name, self.config, self.context = name, config, context
        self.cwd = context.workspace
        self.tools: list = []
        self.error: BaseException | None = None
        self.inflight = 0
        self.retired = False
        self._closed = False
        self._on_closed = on_closed
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._process = None
        self._process_cleaned = False
        self._launch_signal = signal
        self._cancel = threading.Event()
        self._ready = threading.Event()
        self._done = threading.Event()
        self._stop = asyncio.Event()
        self._session: ClientSession | None = None
        self._owner = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"mcp-{name}")
        self._thread.start()
        self._stderr_read, stderr_write = os.pipe()
        self._stderr = os.fdopen(stderr_write, "wb", buffering=0)
        self._stderr_buffer = bytearray()
        self._stderr_lock = threading.Lock()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self._stderr_read,), daemon=True, name=f"mcp-{name}-stderr"
        )
        self._stderr_thread.start()
        self._secrets = _secrets(config)

    def is_set(self):
        return self._cancel.is_set() or self._launch_signal is not None and self._launch_signal.is_set()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        self._loop.run_until_complete(self._loop.shutdown_default_executor())

    def _drain_stderr(self, fd: int) -> None:
        """Keep only a bounded tail; server diagnostics never enter protocol stdout."""
        try:
            with os.fdopen(fd, "rb", buffering=0) as stream:
                while chunk := stream.read(4096):
                    with self._stderr_lock:
                        self._stderr_buffer.extend(chunk)
                        del self._stderr_buffer[:-_STDERR_LIMIT]
        except (OSError, ValueError):
            pass

    def _request(self) -> ProcessRequest:
        env = get_default_environment()
        raw_command, raw_args, raw_env = _validate_config(self.config)
        command = _expand(raw_command)
        # POSIX resolves executable names inside the selected process boundary
        # and cwd. Windows requires an exact executable for its suspended launch.
        if os.name == "nt" and not os.path.isabs(command):
            raise MCPConnectionError("Windows MCP servers require an absolute native .exe path")
        return ProcessRequest(
            (command, *(_expand(str(value)) for value in raw_args)),
            self.cwd, env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            env_overrides={key: _expand(str(value)) for key, value in raw_env.items()},
        )

    def _terminate(self):
        with self._process_lock:
            if self._process is not None and not self._process_cleaned:
                cleanup_process(self._process)
                self._process_cleaned = True

    @asynccontextmanager
    async def _transport(self):
        """Bounded newline framing; SDK owns JSON-RPC and MCP session semantics."""
        received, read = anyio.create_memory_object_stream(0)
        write, outgoing = anyio.create_memory_object_stream(0)
        process = self._process
        assert process is not None and process.stdin is not None and process.stdout is not None

        async def receive():
            pending = bytearray()
            reader = getattr(process.stdout, "read1", process.stdout.read)
            try:
                async with received:
                    while chunk := await asyncio.to_thread(reader, 65536):
                        pending.extend(chunk)
                        while (end := pending.find(b"\n")) >= 0:
                            if end > _MESSAGE_LIMIT:
                                raise MCPConnectionError("MCP response exceeded the message limit")
                            line = bytes(pending[:end])
                            del pending[:end + 1]
                            if line:
                                await received.send(SessionMessage(_JSONRPC.validate_json(line)))
                        if len(pending) > _MESSAGE_LIMIT:
                            raise MCPConnectionError("MCP response exceeded the message limit")
                    if pending:
                        raise MCPConnectionError("MCP server closed with an incomplete message")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.error = self.error or error
            finally:
                if not self._closed:
                    self.error = self.error or MCPConnectionError("MCP server closed its transport")
                self._stop.set()

        def send_bytes(data):
            view = memoryview(data)
            while view:
                written = process.stdin.write(view)
                if not written:
                    raise MCPConnectionError("MCP stdin closed during dispatch; effects are unknown")
                view = view[written:]
            process.stdin.flush()

        async def send():
            try:
                async with outgoing:
                    async for item in outgoing:
                        data = (item.message.model_dump_json(by_alias=True, exclude_unset=True) + "\n").encode()
                        if len(data) > _MESSAGE_LIMIT:
                            raise MCPConnectionError("MCP request exceeded the message limit")
                        await asyncio.to_thread(send_bytes, data)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.error = self.error or error
                self._stop.set()
                await received.aclose()

        tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
        try:
            yield read, write
        finally:
            # Drain the executor-owned tree before closing pipes; a blocked
            # writer or an inherited descendant pipe cannot hold shutdown open.
            try:
                await asyncio.to_thread(self._terminate)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for stream in (read, write, received, outgoing):
                    await stream.aclose()
                if self._process_cleaned:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()

    async def _serve(self) -> None:
        try:
            # Linux parent-death signals are tied to the creating thread.
            # Discovery's temporary pool must not own a persistent server.
            with self._process_lock:
                if self.is_set():
                    raise MCPCallCancelled("MCP connection cancelled before process start")
                self._process = self.context.run_process(self._request(), cancellation=self)
            async with self._transport() as (read, write):
                async with ClientSession(
                    read, write, client_info=Implementation(name="virgo", version=__version__)
                ) as session:
                    await session.initialize()
                    self._session = session
                    self.tools = await self._list_tools(session)
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as error:
            self.error = self.error or error
        finally:
            self._ready.set()
            self._done.set()

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
        try:
            with self._lock:
                if self._closed:
                    raise MCPCallCancelled("MCP connection cancelled during process start")
                self._owner = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
            deadline = time.monotonic() + timeout
            while not self._ready.wait(0.05):
                if self.is_set():
                    raise MCPCallCancelled("MCP connection cancelled during initialization; effects may already exist")
                if time.monotonic() >= deadline:
                    raise MCPConnectionError(f"handshake timed out after {timeout:g}s")
            if self._closed:
                raise MCPCallCancelled("MCP connection closed during initialization")
            if self.error is not None and not self.tools:
                detail = safe_text(self.error, secrets=self._secrets)
                stderr = self.stderr_tail()
                raise MCPConnectionError(detail + (f"\n{stderr}" if stderr else ""))
        except BaseException:
            self.close()
            raise
        finally:
            self._launch_signal = None

    @property
    def ready(self) -> bool:
        return (self._session is not None and self.error is None
                and not self._closed and not self._done.is_set())

    @property
    def boundary(self):
        return getattr(self._process, "_virgo_boundary", None) or {
            **self.context.describe(), "payload_started": self._process is not None,
            "side_effects": "possible" if self._process is not None else "none",
            "cleanup": "executor_completed" if self._process_cleaned else "pending",
        }

    def stderr_tail(self) -> str:
        with self._stderr_lock:
            text = bytes(self._stderr_buffer).decode("utf-8", errors="replace")
        return safe_text(text, secrets=self._secrets) if text else ""

    def call(self, tool: str, arguments: dict, signal) -> types.CallToolResult:
        if signal is not None and signal.is_set():
            raise MCPCallCancelled(f"{self.name}: cancelled before {tool} dispatch")
        with self._lock:
            if self.retired or not self.ready:
                raise MCPConnectionError(f"{self.name}: connection unavailable; use /mcp reconnect")
            self.inflight += 1
        try:
            assert self._session is not None
            future = asyncio.run_coroutine_threadsafe(self._session.call_tool(tool, arguments), self._loop)
            deadline = time.monotonic() + CALL_TIMEOUT
            while True:
                if signal is not None and signal.is_set():
                    future.cancel()
                    self.error = MCPCallCancelled(f"{self.name}: cancelled {tool}; effects may already exist")
                    self.close()
                    raise self.error
                try:
                    return future.result(0.05)
                except FutureTimeout:
                    if time.monotonic() >= deadline:
                        future.cancel()
                        self.error = TimeoutError(f"{self.name}: tool call timed out after {CALL_TIMEOUT:g}s")
                        self.close()
                        raise self.error from None
                except Exception as error:
                    self.error = self.error or error
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
        with self._close_lock:
            self._close()

    def _close(self) -> None:
        with self._lock:
            if self._closed and self._done.is_set() and not self._thread.is_alive():
                return
            self._closed = True
            self._cancel.set()
        self._terminate()
        if self._owner is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
            if not self._done.wait(15):
                raise MCPConnectionError(f"{self.name}: cleanup incomplete; execution resources retained")
        else:
            self._done.set()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        if self._thread.is_alive():
            raise MCPConnectionError(f"{self.name}: pipe cleanup incomplete")
        self._loop.close()
        self._stderr.close()
        self._stderr_thread.join(2)
        if self._on_closed is not None:
            self._on_closed(self)


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


class _Server:
    """A configuration generation and schemas; process ownership stays per runtime."""

    def __init__(self, name, config, runtime, connection):
        self.name, self.config = name, config
        self.tools = connection.tools
        self.connections = {runtime: connection}
        self.retired = False
        self.lock = threading.RLock()

    def retire(self):
        with self.lock:
            self.retired = True
            connections = tuple(self.connections.values())
        for connection in connections:
            connection.retire()


class _Manager:
    def __init__(self, api):
        self.api = api
        self.servers: dict[str, _Server] = {}
        self.failures: dict[str, str] = {}
        self.configured: list[str] = []
        self.retired: list[_Server] = []
        self._owned: list[_Connection] = []
        self._ownership_lock = threading.Lock()
        self._closed = False
        self.started = False

    def load_config(self) -> dict:
        servers: dict = {}
        trusted = (self.api.session.trust_project_extensions
                   or self.api.settings.project_extensions_trusted)
        user_file = self.api.paths.user / "mcp.json"
        project_file = self.api.paths.project / "mcp.json"
        paths = [user_file]
        if not _same_directory(self.api.paths.project, self.api.paths.user):
            paths.append(project_file)
        for path in paths:
            if not path.is_file():
                continue
            if path == project_file and not trusted:
                self.api.ui.print("[mcp: ignored project MCP configuration without explicit project trust]", "warn")
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                listed = data.get("mcpServers", {}) if isinstance(data, dict) else None
                if not isinstance(listed, dict):
                    raise ValueError("mcpServers must be an object")
                servers.update(listed)
            except ValueError as error:
                self.api.ui.print(f"[mcp: invalid {path}: {safe_text(error)}]", "error")
        configured = self.api.settings.get("mcpServers", {}) or {}
        if isinstance(configured, dict):
            for name, config in configured.items():
                prefix = f"mcpServers.{name}"
                if not trusted and any(
                    source == "project" and (key == prefix or key.startswith(prefix + "."))
                    for key, source in self.api.settings.sources.items()
                ):
                    self.api.ui.print("[mcp: ignored project MCP configuration without explicit project trust]", "warn")
                    continue
                servers[name] = config
        else:
            self.api.ui.print("[mcp: invalid settings: mcpServers must be an object]", "error")
        return {name: deepcopy(config) for name, config in servers.items()
                if isinstance(config, dict) and config.get("command") and not config.get("disabled")}

    def _release_owned(self, connection: _Connection) -> None:
        with self._ownership_lock:
            if connection in self._owned:
                self._owned.remove(connection)

    def _connect(self, name: str, config: dict, context, signal=None) -> _Connection:
        _validate_config(config)
        raw_timeout = config.get("timeout", 30)
        if isinstance(raw_timeout, bool):
            raise MCPConnectionError("MCP connection timeout must be a positive finite number")
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError) as error:
            raise MCPConnectionError("MCP connection timeout must be a positive finite number") from error
        if not math.isfinite(timeout) or timeout <= 0:
            raise MCPConnectionError("MCP connection timeout must be a positive finite number")
        with self._ownership_lock:
            if self._closed:
                raise MCPConnectionError("MCP manager has shut down")
            connection = _Connection(name, config, context, signal, on_closed=self._release_owned)
            self._owned.append(connection)
        connection.connect(timeout=timeout)
        return connection

    def _connect_all(self, config: dict) -> tuple[dict[str, _Server], dict[str, str]]:
        servers: dict[str, _Server] = {}
        failures: dict[str, str] = {}
        if not config:
            return servers, failures
        runtime = self.api.current_runtime()
        context = runtime.execution_context

        def one(item):
            name, server = item
            try:
                connection = self._connect(name, server, context, runtime.agent.signal)
                return name, _Server(name, server, runtime, connection), None
            except Exception as error:
                detail = safe_text(error, secrets=_secrets(server))
                self.api.report_error(MCPConnectionError(detail), phase="mcp.connect",
                                      context={"server": name}, notify=False)
                return name, None, detail

        with ThreadPoolExecutor(max_workers=min(8, len(config))) as pool:
            for name, server, failure in pool.map(one, config.items()):
                if server is not None:
                    servers[name] = server
                else:
                    failures[name] = failure or "connection failed"
        return servers, failures

    def _connection(self, server, ctx):
        runtime = self.api.runtime_for_context(ctx)
        if runtime is None:
            raise MCPConnectionError("MCP execution requires the originating runtime")
        if ctx.signal is not None and ctx.signal.is_set():
            raise MCPCallCancelled("MCP operation cancelled before process start")
        context = self.api.services["execution"].for_context(ctx)
        transient = context is not runtime.execution_context
        if transient:
            with server.lock:
                if server.retired:
                    raise MCPConnectionError("MCP configuration replaced; use the refreshed tool set")
                name, config = server.name, server.config
            connection = self._connect(name, config, context, ctx.signal)
            with server.lock:
                retired = server.retired
            if retired:
                connection.close()
                raise MCPConnectionError("MCP configuration replaced; use the refreshed tool set")
            return connection, True
        with server.lock:
            if server.retired:
                raise MCPConnectionError("MCP configuration replaced; use the refreshed tool set")
            connection = server.connections.get(runtime)
            if connection is not None and connection.context != context:
                connection.retire()
                connection = None
            if connection is None:
                connection = self._connect(server.name, server.config, context, ctx.signal)
                server.connections[runtime] = connection
            return connection, False

    def _execute(self, server, tool, name, args, ctx):
        connection = None
        transient = False
        try:
            connection, transient = self._connection(server, ctx)
            try:
                result = connection.call(tool, args, ctx.signal)
            finally:
                if transient:
                    connection.close()
            return ToolResult(
                _content(result), is_error=bool(result.is_error),
                details={"mcp": True, "server": server.name, "tool": name,
                         "boundary": connection.boundary, "temporary_grant": transient},
            )
        except Exception as error:
            details = {"mcp": True, "server": server.name, "tool": name,
                       "status": "mcp_execution_failed", "automatic_replay": False}
            if connection is not None:
                details["boundary"] = connection.boundary
            secrets = connection._secrets if connection is not None else _secrets(server.config)
            message = f"{server.name}: {safe_text(error, secrets=secrets)}"
            if connection is not None and (stderr := connection.stderr_tail()):
                message += f"\nMCP server stderr:\n{stderr}"
            return ToolResult.error(message, details)

    def _publish(self) -> None:
        entries = []
        seen: set[str] = set()
        for server in self.servers.values():
            for spec in server.tools:
                name = tool_name(server.name, spec.name)
                if not spec.name or name in seen:
                    continue
                if not _valid_schema(spec.input_schema) or spec.input_schema.get("type", "object") != "object":
                    self.api.ui.print(f"[mcp: {server.name}/{spec.name} has an unsupported input schema; skipped]", "warn")
                    continue
                seen.add(name)
                schema = deepcopy(spec.input_schema or {"type": "object", "properties": {}})

                def execute(args, ctx, _server=server, _tool=spec.name, _name=name):
                    return self._execute(_server, _tool, _name, args, ctx)

                annotations = getattr(spec, "annotations", None)
                entries.append((AgentTool(
                    name, f"[MCP {server.name}] {(spec.description or spec.name).strip()}", schema, execute,
                    side_effect="read" if getattr(annotations, "read_only_hint", None) is True else "write",
                    execution_scope="process",
                ), ("main", "subagent")))
        self.api.replace_tools("mcp", entries)

    def start(self, _session) -> None:
        if self.started:
            return
        self.started = True
        config = self.load_config()
        self.configured = list(config)
        self.servers, self.failures = self._connect_all(config)
        self._publish()

    def reconnect(self) -> bool:
        config = self.load_config()
        candidates, failures = self._connect_all(config)
        if failures:
            for server in candidates.values():
                server.retire()
            self.configured = list(config)
            self.failures = failures
            return False
        previous = self.servers
        self.servers = candidates
        self.configured = list(config)
        self.failures = {}
        self._publish()
        for server in previous.values():
            self.retired.append(server)
            server.retire()
        return True

    def release_runtime(self, runtime):
        for server in [*self.servers.values(), *self.retired]:
            with server.lock:
                connection = server.connections.pop(runtime, None)
            if connection is not None:
                connection.retire()

    def retire_obsolete(self, runtime, *_args):
        for server in self.servers.values():
            with server.lock:
                connection = server.connections.get(runtime)
                if connection is None or connection.context == runtime.execution_context:
                    continue
                server.connections.pop(runtime)
            connection.retire()

    def agent_event(self, event, runtime):
        if event.type == "agent_start":
            self.retire_obsolete(runtime)
        elif event.type == "agent_end" and runtime.agent.signal.is_set():
            for server in self.servers.values():
                with server.lock:
                    connection = server.connections.get(runtime)
                if connection is not None:
                    connection.retire()

    def status(self, runtime=None) -> list[dict]:
        runtime = runtime or self.api.current_runtime()
        rows = []
        for name in dict.fromkeys([*self.configured, *self.servers, *self.failures]):
            server = self.servers.get(name)
            connection = server.connections.get(runtime) if server is not None else None
            row = {"server": name, "state": "idle", "tools": len(server.tools) if server else 0,
                   "error": None, "adapter": "trusted-host-extension", "transport": "sandbox-executor"}
            if connection is not None:
                row.update(state="ready" if connection.ready else "unavailable", execution=connection.boundary)
                if connection.error is not None:
                    row["error"] = safe_text(connection.error, secrets=connection._secrets)
            elif server is None:
                row.update(state="unavailable", error=self.failures.get(name, "not connected"))
            rows.append(row)
        return rows

    def status_tool(self, args: dict, ctx) -> ToolResult:
        rows = self.status(self.api.runtime_for_context(ctx))
        server = args.get("server")
        if server:
            rows = [row for row in rows if row["server"] == server]
            if not rows:
                return ToolResult.error(f"No configured MCP server named {server!r}.")
        return ToolResult.text(json.dumps({"servers": rows}, ensure_ascii=False), details={"mcp_status": rows})

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
            state = f"error: {row['error']}" if row["error"] else f"{row['tools']} tools ({row['state']})"
            self.api.ui.print(f"  {row['server']}: {state}")

    def close(self) -> None:
        errors = []
        with self._ownership_lock:
            self._closed = True
            connections = tuple(self._owned)
        for connection in connections:
            try:
                connection.close()
            except Exception as error:
                errors.append(safe_text(error, secrets=connection._secrets))
            else:
                self._release_owned(connection)
        self.servers.clear()
        self.retired.clear()
        if errors:
            raise MCPConnectionError("MCP cleanup failed: " + "; ".join(errors))


def setup(api) -> None:
    manager = _Manager(api)
    api.on_cleanup(manager.close)
    api.on("session_start", manager.start)
    api.on("runtime_closed", manager.release_runtime)
    api.on("permission_mode_changed", manager.retire_obsolete)
    api.on("agent_event", manager.agent_event)
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
