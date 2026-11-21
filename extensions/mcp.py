"""MCP extension: connect configured stdio servers on Host and expose their tools.

Install this file as ``~/.virgo/extensions/mcp.py`` (or ``.virgo/extensions/mcp.py``
in a trusted project) and install the optional SDK dependency::

    uv tool install 'virgo[mcp]'      # installed distribution
    uv sync --extra mcp               # source checkout

Servers are configured in ``~/.virgo/mcp.json`` (or ``.virgo/mcp.json`` in a
trusted project), or under the ``mcpServers`` setting, using the common format::

    {"mcpServers": {"github": {"command": "npx",
                               "args": ["-y", "@modelcontextprotocol/server-github"],
                               "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}}

MCP servers are trusted local Host integrations (Principle R4): they run on
Host via ``HostContext.unrestricted`` + ``SandboxExecutor`` with one shared
connection per server across main and subagent runtimes, while mutating MCP
tools remain subject to permission approvals bound to the server's config
fingerprint. Tools become ``mcp__<server>__<tool>``. ``/mcp`` shows connection
state and ``/mcp reconnect`` reloads the configuration.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path

import anyio
from mcp import ClientSession, types
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from mcp.types import Implementation
from pydantic import TypeAdapter

from virgo import __version__
from virgo.agent import AgentTool, ToolResult
from virgo.ai.types import ImageContent, TextContent
from virgo.ai.validate import valid_schema
from virgo.diagnostics import safe_text
from virgo.execution import HostContext, ProcessRequest, cleanup_process
from virgo.harness.settings import same_directory

CALL_TIMEOUT = 300.0
STARTUP_WAIT = 2.0
MAX_TOOL_PAGES = 100
_STDERR_LIMIT = 8192
_MESSAGE_LIMIT = 8 * 1024 * 1024
_JSONRPC = TypeAdapter(types.JSONRPCMessage)
_SECRET_NAME = re.compile(r"token|password|secret|credential|api[-_]?key|authorization", re.I)


class MCPConnectionError(Exception):
    def __init__(self, message: str, *, stderr: str = "", boundary: dict | None = None):
        super().__init__(message)
        self.stderr = stderr
        self.boundary = boundary


class MCPCallCancelled(Exception):
    pass


def _expand(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda match: os.environ.get(match.group(1), ""), value)


def _validate_tool_patterns(value, field: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise MCPConnectionError(f"MCP server {field} must be a list of nonempty strings")
    return tuple(item.strip() for item in value)


def _validate_options(config: dict) -> tuple[float, tuple[str, ...] | None, tuple[str, ...]]:
    raw_call_timeout = config.get("callTimeout", CALL_TIMEOUT)
    if (
        isinstance(raw_call_timeout, bool)
        or not isinstance(raw_call_timeout, (int, float))
        or not math.isfinite(raw_call_timeout)
        or raw_call_timeout <= 0
    ):
        raise MCPConnectionError("MCP server callTimeout must be a positive number")
    include_tools = _validate_tool_patterns(config.get("includeTools"), "includeTools")
    exclude_tools = _validate_tool_patterns(config.get("excludeTools"), "excludeTools") or ()
    return float(raw_call_timeout), include_tools, exclude_tools


def _validate_config(config: dict) -> tuple[str, list[str], dict[str, str], float]:
    """Validate and expand MCP server config strictly (S4-07)."""
    if not isinstance(config, dict):
        raise MCPConnectionError("MCP server configuration must be a JSON object")
    raw_command = config.get("command")
    if not isinstance(raw_command, str) or not raw_command.strip():
        raise MCPConnectionError("MCP server command must be a nonempty string")
    command = _expand(raw_command).strip()
    if not command:
        raise MCPConnectionError("MCP server command expanded to an empty string")

    raw_args = config.get("args")
    if raw_args is None:
        args: list[str] = []
    elif not isinstance(raw_args, list) or any(not isinstance(item, str) for item in raw_args):
        raise MCPConnectionError("MCP server args must be a list of strings")
    else:
        args = [_expand(item) for item in raw_args]

    raw_env = config.get("env")
    if raw_env is None:
        env: dict[str, str] = {}
    elif not isinstance(raw_env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in raw_env.items()
    ):
        raise MCPConnectionError("MCP server env must be an object mapping string keys to string values")
    else:
        env = {key: _expand(value) for key, value in raw_env.items()}

    raw_timeout = config.get("timeout", 30.0)
    if (
        isinstance(raw_timeout, bool)
        or not isinstance(raw_timeout, (int, float))
        or not math.isfinite(raw_timeout)
        or raw_timeout <= 0
    ):
        raise MCPConnectionError("MCP server timeout must be a positive number")
    _validate_options(config)
    return command, args, env, float(raw_timeout)


def _config_fingerprint(command: str, args: list[str], env: dict[str, str]) -> str:
    """Hash expanded command, args, and env overrides without persisting secrets (S4-02)."""
    payload = json.dumps(
        {"command": command, "args": args, "env": sorted(env.items())},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _resolve_host_argv(
    command: str,
    args: list[str],
    env: dict[str, str],
    *,
    platform_nt: bool = os.name == "nt",
    which=shutil.which,
) -> tuple[str, ...]:
    """Resolve Host command argv, supporting PATH lookup and .cmd/.bat on native Windows (S4-08)."""
    if not platform_nt:
        return (command, *args)
    path_value = next((v for k, v in env.items() if k.upper() == "PATH"), None)
    resolved = which(command, path=path_value) or command
    suffix = Path(resolved).suffix.lower()
    if suffix in {".cmd", ".bat"}:
        comspec = (
            next((v for k, v in env.items() if k.upper() == "COMSPEC"), None)
            or os.environ.get("COMSPEC")
            or "cmd.exe"
        )
        # 'call' prevents cmd.exe from stripping outer quotes when both the script path and args have spaces.
        return (comspec, "/d", "/c", "call", resolved, *args)
    return (resolved, *args)


def _secrets(config: dict) -> tuple[str, ...]:
    """Values that must not reach diagnostics or the terminal."""
    raw_env = config.get("env")
    values = [value for value in raw_env.values() if isinstance(value, str)] if isinstance(raw_env, dict) else []
    raw_args = config.get("args")
    args = [value for value in raw_args if isinstance(value, str)] if isinstance(raw_args, list) else []
    sensitive = []
    previous = ""
    for value in args:
        if _SECRET_NAME.search(previous):
            sensitive.append(value)
        name, separator, data = value.partition("=")
        if separator and _SECRET_NAME.search(name):
            sensitive.append(data)
        previous = value if value.startswith("-") else ""
    return tuple(expanded for value in [*values, *sensitive] if (expanded := _expand(value)))


def _host_base_env() -> dict[str, str]:
    """Host environment excluding exported shell functions."""
    return {key: value for key, value in os.environ.items() if not value.startswith("()")}


class _Connection:
    """One shared Host stdio MCP server owned by a dedicated asyncio loop thread.

    Launched through ``HostContext.unrestricted`` + ``SandboxExecutor`` so
    process-tree cleanup, cancellation, and Windows Job Objects are uniform
    across Virgo (S4-01).
    """

    def __init__(self, name: str, config: dict, context: HostContext, *, signal=None):
        self.name = name
        self.config = config
        self.context = context
        self.cwd = context.workspace
        self._secrets = _secrets(config)
        self._command, self._args, self._env, self.timeout = _validate_config(config)
        self.call_timeout, self.include_tools, self.exclude_tools = _validate_options(config)
        self.fingerprint = _config_fingerprint(self._command, self._args, self._env)

        self.tools: list = []
        self.error: BaseException | None = None
        self.inflight = 0
        self.retired = False
        self._closed = False
        self._lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._stderr_close_lock = threading.Lock()
        self._process = None
        self._process_cleaned = False
        self._launch_signal = signal
        self._cancel = threading.Event()
        self._ready = threading.Event()
        self._done = threading.Event()
        self._stop: asyncio.Event | None = None
        self._owner = None
        self._session: ClientSession | None = None

        # S4-07: Initialize stderr pipe and reader thread BEFORE starting the event loop thread.
        self._stderr_read, stderr_write = os.pipe()
        self._stderr = os.fdopen(stderr_write, "wb", buffering=0)
        self._stderr_closed = False
        self._stderr_buffer = bytearray()
        self._stderr_lock = threading.Lock()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self._stderr_read,), daemon=True, name=f"mcp-{name}-stderr"
        )
        self._stderr_thread.start()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"mcp-{name}")
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except RuntimeError:
                pass

    def _drain_stderr(self, fd: int) -> None:
        """Keep only a bounded tail; a noisy server must not grow memory forever."""
        try:
            with os.fdopen(fd, "rb", buffering=0) as stream:
                while chunk := stream.read(4096):
                    with self._stderr_lock:
                        self._stderr_buffer.extend(chunk)
                        if len(self._stderr_buffer) > _STDERR_LIMIT:
                            del self._stderr_buffer[:-_STDERR_LIMIT]
        except (OSError, ValueError):
            pass

    def _close_stderr_writer(self) -> None:
        with self._stderr_close_lock:
            if self._stderr_closed:
                return
            self._stderr_closed = True
            try:
                self._stderr.close()
            except (OSError, ValueError):
                pass

    def _request(self) -> ProcessRequest:
        base_env = _host_base_env()
        merged_for_lookup = {**base_env, **self._env}
        argv = _resolve_host_argv(self._command, self._args, merged_for_lookup)
        return ProcessRequest(
            argv=argv,
            cwd=self.cwd,
            env=base_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env_overrides=self._env,
        )

    def _terminate(self) -> None:
        with self._process_lock:
            process = self._process
            if process is None or self._process_cleaned:
                return
            self._process_cleaned = True
        try:
            cleanup_process(process)
        finally:
            for stream in (
                getattr(process, "stdin", None),
                getattr(process, "stdout", None),
                getattr(process, "stderr", None),
            ):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

    @asynccontextmanager
    async def _transport(self):
        """Bounded newline JSON-RPC framing bridged over SandboxExecutor pipes."""
        received, read = anyio.create_memory_object_stream(0)
        write, outgoing = anyio.create_memory_object_stream(0)
        process = self._process
        assert process is not None and process.stdin is not None and process.stdout is not None
        stop_event = self._stop
        assert stop_event is not None

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
                            line = bytes(pending[:end]).rstrip(b"\r")
                            del pending[: end + 1]
                            if line:
                                await received.send(SessionMessage(_JSONRPC.validate_json(line)))
                        if len(pending) > _MESSAGE_LIMIT:
                            raise MCPConnectionError("MCP response exceeded the message limit")
                    if pending:
                        raise MCPConnectionError("MCP server closed with an incomplete message")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                with self._lock:
                    if self.error is None:
                        self.error = error
            finally:
                with self._lock:
                    if not self._closed and self.error is None:
                        self.error = MCPConnectionError("MCP server closed its transport")
                stop_event.set()

        def send_bytes(data: bytes) -> None:
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
                        data = (item.message.model_dump_json(by_alias=True, exclude_unset=True) + "\n").encode("utf-8")
                        if len(data) > _MESSAGE_LIMIT:
                            raise MCPConnectionError("MCP request exceeded the message limit")
                        await asyncio.to_thread(send_bytes, data)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                with self._lock:
                    if self.error is None:
                        self.error = error
                stop_event.set()
                await received.aclose()

        tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
        try:
            yield read, write
        finally:
            try:
                await asyncio.to_thread(self._terminate)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for stream in (read, write, received, outgoing):
                    await stream.aclose()

    async def _serve(self) -> None:
        session = None
        try:
            self._stop = asyncio.Event()
            request = self._request()
            process = await asyncio.to_thread(self.context.run_process, request, self._cancel)
            with self._process_lock:
                self._process = process
            if self._cancel.is_set() or self._closed:
                raise MCPCallCancelled(f"{self.name}: connection cancelled during startup")
            async with self._transport() as (read, write):
                session = ClientSession(
                    read, write, client_info=Implementation(name="virgo", version=__version__)
                )
                async with session:
                    await session.initialize()
                    self._session = session
                    self.tools = await self._list_tools(session)
                    if self._stop.is_set() or self.error is not None:
                        raise self.error or MCPConnectionError("MCP server terminated during initialization")
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as error:
            with self._lock:
                if self.error is None:
                    self.error = error
            self._ready.set()
        finally:
            self._terminate()
            self._done.set()
            # S4-05: Automatically reap stderr pipe and event loop thread when server exits spontaneously.
            self._close_stderr_writer()
            self._loop.call_soon(self._loop.stop)

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

    def _cancelled(self) -> bool:
        return self._cancel.is_set() or (self._launch_signal is not None and self._launch_signal.is_set())

    def connect(self, timeout: float | None = None) -> None:
        wait_timeout = self.timeout if timeout is None else timeout
        try:
            with self._lock:
                if self._closed or self._cancelled():
                    raise MCPCallCancelled(f"{self.name}: connection cancelled before start")
                self._owner = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
            deadline = time.monotonic() + wait_timeout
            while not self._ready.wait(0.05):
                if self._cancelled():
                    self._cancel.set()
                    raise MCPCallCancelled(f"{self.name}: connection cancelled during initialization")
                if time.monotonic() >= deadline:
                    self._cancel.set()
                    raise MCPConnectionError(
                        f"handshake timed out after {wait_timeout:g}s",
                        stderr=self.stderr_tail(),
                        boundary=self.boundary,
                    )
            if self._closed or self._cancelled():
                raise MCPCallCancelled(f"{self.name}: connection cancelled during initialization")
            # S4-05: Never accept a server that errored or exited even if tools were already listed.
            exited = False
            with self._process_lock:
                if self._process is None or self._process.poll() is not None:
                    exited = True
            if self.error is not None or self._done.is_set() or exited:
                cause = self.error or MCPConnectionError("MCP server exited during initialization")
                self.close()
                stderr = self.stderr_tail()
                detail = safe_text(cause, secrets=self._secrets)
                if stderr:
                    detail += f"\n{stderr}"
                raise MCPConnectionError(detail, stderr=stderr, boundary=self.boundary)
        except BaseException:
            self.close()
            raise
        finally:
            self._launch_signal = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def ready(self) -> bool:
        """The handshake finished and the transport has not exited or closed."""
        if self._session is None or self.error is not None or self._closed or self._done.is_set():
            return False
        with self._process_lock:
            if self._process is None:
                return False
            try:
                return self._process.poll() is None
            except Exception:
                return False

    @property
    def boundary(self) -> dict:
        with self._process_lock:
            process = self._process
            cleaned = self._process_cleaned
        return getattr(process, "_virgo_boundary", None) or {
            **self.context.describe(),
            "payload_started": process is not None,
            "side_effects": "possible" if process is not None else "none",
            "cleanup": "executor_completed" if cleaned else "pending",
        }

    def stderr_tail(self) -> str:
        """The server's bounded stderr tail, with configured secrets redacted (S4-06)."""
        if self._done.is_set() and threading.current_thread() is not self._stderr_thread:
            self._stderr_thread.join(0.5)
        with self._stderr_lock:
            text = bytes(self._stderr_buffer).decode("utf-8", errors="replace")
        return safe_text(text, secrets=self._secrets) if text else ""

    def _is_transport_failure(self, error: BaseException) -> bool:
        """Distinguish JSON-RPC application errors from broken transports (S4-03)."""
        if self._done.is_set() or self._closed:
            return True
        with self._process_lock:
            process = self._process
            if process is None:
                return True
            try:
                if process.poll() is not None:
                    return True
            except Exception:
                return True
        if isinstance(error, MCPError):
            err_data = getattr(error, "error", None)
            code = getattr(err_data, "code", None)
            return code == types.CONNECTION_CLOSED
        return isinstance(
            error,
            (
                MCPConnectionError,
                TimeoutError,
                BrokenPipeError,
                ConnectionError,
                EOFError,
                OSError,
                anyio.ClosedResourceError,
                anyio.BrokenResourceError,
                anyio.EndOfStream,
            ),
        )

    def call(self, tool: str, arguments: dict, signal) -> types.CallToolResult:
        if signal is not None and signal.is_set():
            raise MCPCallCancelled(f"{self.name}: cancelled {tool}")
        with self._lock:
            if self._closed or self._done.is_set():
                raise MCPConnectionError(
                    f"{self.name}: connection closed. Run /mcp reconnect to restart it.",
                    stderr=self.stderr_tail(),
                    boundary=self.boundary,
                )
            if self.retired:
                raise MCPConnectionError(
                    f"{self.name}: connection replaced; retry with the refreshed tool set",
                    boundary=self.boundary,
                )
            if self.error is not None:
                raise MCPConnectionError(
                    f"{self.name}: {safe_text(self.error, secrets=self._secrets)}. Run /mcp reconnect to restart it.",
                    stderr=self.stderr_tail(),
                    boundary=self.boundary,
                )
            self.inflight += 1
        transport_failed = False
        try:
            assert self._session is not None
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._session.call_tool(tool, arguments), self._loop
                )
            except RuntimeError as error:
                transport_failed = True
                with self._lock:
                    if not self._closed:
                        self.error = error
                        self.retired = True
                raise MCPConnectionError(
                    f"{self.name}: transport closed. Run /mcp reconnect to restart it.",
                    stderr=self.stderr_tail(),
                    boundary=self.boundary,
                ) from error
            deadline = time.monotonic() + self.call_timeout
            while True:
                if signal is not None and signal.is_set():
                    future.cancel()
                    raise MCPCallCancelled(f"{self.name}: cancelled {tool}")
                try:
                    return future.result(0.05)
                except FutureTimeout:
                    if time.monotonic() >= deadline:
                        future.cancel()
                        transport_failed = True
                        error = TimeoutError(
                            f"{self.name}: tool call timed out after {self.call_timeout:g}s; "
                            "connection closed. Run /mcp reconnect to restart it."
                        )
                        with self._lock:
                            self.error = error
                            self.retired = True
                        raise error from None
                except Exception as error:
                    # S4-03: Only transport failures poison & close the connection;
                    # normal JSON-RPC errors (e.g. -32602 Invalid Params) leave the server reusable.
                    if self._is_transport_failure(error):
                        transport_failed = True
                        with self._lock:
                            if not self._closed and self.error is None:
                                self.error = error
                            self.retired = True
                    raise
        finally:
            with self._lock:
                self.inflight -= 1
                should_close = (self.retired or transport_failed) and self.inflight == 0
            if should_close:
                self.close()

    def retire(self) -> None:
        with self._lock:
            self.retired = True
            should_close = self.inflight == 0
        if should_close:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._cancel.set()
        if not self._ready.is_set():
            # Interrupting startup/handshake: terminate the child process immediately so
            # blocked stdout reads and session.initialize() unwind instead of waiting 10s.
            self._terminate()
        if self._owner is not None:
            try:
                if self._ready.is_set() and self._stop is not None:
                    self._loop.call_soon_threadsafe(self._stop.set)
                else:
                    self._loop.call_soon_threadsafe(self._owner.cancel)
            except RuntimeError:
                pass
            self._done.wait(10)
        self._terminate()
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
        if threading.current_thread() is not self._thread:
            self._thread.join(5)
        self._close_stderr_writer()
        if threading.current_thread() is not self._stderr_thread:
            self._stderr_thread.join(2)


def _norm(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


def _short_hash(server: str, tool: str) -> str:
    return hashlib.sha256(f"{server}\0{tool}".encode()).hexdigest()[:8]


def _disambiguated_tool_name(server: str, tool: str) -> str:
    raw = f"mcp__{_norm(server)}__{_norm(tool)}"
    return f"{raw[:55]}_{_short_hash(server, tool)}"


def tool_name(server: str, tool: str) -> str:
    raw = f"mcp__{_norm(server)}__{_norm(tool)}"
    if len(raw) <= 64:
        return raw
    return _disambiguated_tool_name(server, tool)


def _tool_allowed(connection: _Connection, raw_name: str) -> bool:
    if connection.include_tools is not None and not any(
        fnmatch.fnmatchcase(raw_name, pattern) for pattern in connection.include_tools
    ):
        return False
    return not any(fnmatch.fnmatchcase(raw_name, pattern) for pattern in connection.exclude_tools)


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
        self._lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self.connections: dict[str, _Connection] = {}
        self.failures: dict[str, str] = {}
        self.configured: list[str] = []
        self.retired: list[_Connection] = []
        self._connecting: set[str] = set()
        self._published_counts: dict[str, int] = {}
        self._warned_collisions: set[tuple[str, str, str]] = set()
        self._warned_generation = -1
        self._generation = 0
        self._closed = False
        self._startup_cancel: threading.Event | None = None
        self._startup_done = threading.Event()
        self._startup_done.set()
        self._startup_thread: threading.Thread | None = None

    def _project_trusted(self) -> bool:
        return bool(self.api.project_trusted)

    def _host_context(self) -> HostContext:
        return self.api.host_context()

    def _read_servers_file(self, path: Path, *, from_settings: bool = False) -> dict:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("configuration must be a JSON object")
            if from_settings and "mcpServers" not in data:
                return {}
            listed = data.get("mcpServers", {})
            if listed is None:
                return {}
            if not isinstance(listed, dict):
                raise ValueError("mcpServers must be an object")
            return listed
        except (OSError, ValueError) as error:
            self.api.ui.print(f"[mcp: invalid {path}: {safe_text(error)}]", "error")
            return {}

    def load_config(self) -> dict:
        servers: dict = {}
        # Precedence: user mcp.json < user settings.json < trusted project mcp.json
        # < trusted project settings.json < startup settings overrides.
        servers.update(self._read_servers_file(self.api.paths.user / "mcp.json"))
        servers.update(self._read_servers_file(self.api.paths.user / "settings.json", from_settings=True))
        if not same_directory(self.api.paths.project, self.api.paths.user):
            project_mcp = self.api.paths.project / "mcp.json"
            # S4-02 (R5): Project-level .virgo/mcp.json and settings.json require explicit project trust.
            if self._project_trusted():
                servers.update(self._read_servers_file(project_mcp))
                servers.update(
                    self._read_servers_file(self.api.paths.project / "settings.json", from_settings=True)
                )
            elif project_mcp.is_file():
                self.api.ui.print(
                    "[mcp: ignored untrusted project .virgo/mcp.json; add this directory to "
                    "extensions.trustedProjects or pass --trust-project-extensions]",
                    "warn",
                )
        overrides = getattr(self.api.settings, "overrides", None)
        if isinstance(overrides, dict) and "mcpServers" in overrides:
            from_overrides = overrides.get("mcpServers")
            if isinstance(from_overrides, dict):
                servers.update(from_overrides)
            elif from_overrides is not None:
                self.api.ui.print("[mcp: invalid settings: mcpServers must be an object]", "error")
        valid: dict = {}
        for name, config in servers.items():
            if not isinstance(config, dict):
                self.api.ui.print(f"[mcp: invalid server {name!r}: expected an object]", "error")
                continue
            if config.get("disabled"):
                continue
            valid[name] = config
        return valid

    def _connect(self, name: str, config: dict, *, signal=None) -> _Connection:
        connection = _Connection(name, config, self._host_context(), signal=signal)
        connection.connect()
        return connection

    def _connect_one(self, name: str, server, *, signal=None) -> tuple[str, _Connection | None, str | None]:
        try:
            return name, self._connect(name, server, signal=signal), None
        except Exception as error:
            secrets = _secrets(server) if isinstance(server, dict) else ()
            context = {"server": name}
            if isinstance(error, MCPConnectionError):
                if error.stderr:
                    context["stderr"] = error.stderr
                if error.boundary is not None:
                    context["boundary"] = error.boundary
            if not isinstance(error, MCPCallCancelled):
                self.api.report_error(error, phase="mcp.connect", context=context, notify=False)
            return name, None, safe_text(error, secrets=secrets)

    def _connect_all(self, config: dict, *, signal=None) -> tuple[dict[str, _Connection], dict[str, str]]:
        connections: dict[str, _Connection] = {}
        failures: dict[str, str] = {}
        if not config:
            return connections, failures

        with ThreadPoolExecutor(max_workers=min(8, len(config))) as pool:
            for name, connection, failure in pool.map(
                lambda item: self._connect_one(item[0], item[1], signal=signal), config.items()
            ):
                if connection is not None:
                    connections[name] = connection
                else:
                    failures[name] = failure or "connection failed"
        return connections, failures

    def _publish(self) -> None:
        with self._publish_lock:
            with self._lock:
                if self._closed:
                    return
                generation = self._generation
                configured = list(self.configured)
                ordered_names = list(dict.fromkeys([*configured, *sorted(self.connections)]))
                snapshot = [self.connections[name] for name in ordered_names if name in self.connections]
            if generation != self._warned_generation:
                self._warned_collisions.clear()
                self._warned_generation = generation
            # Pre-detect normalized server collisions from all configured servers so
            # handshake completion order or incremental startup never rebinds a name.
            server_norm_counts = Counter(_norm(name) for name in dict.fromkeys([*configured, *(c.name for c in snapshot)]))
            colliding_servers = {norm for norm, count in server_norm_counts.items() if count > 1}

            candidates: list[tuple[_Connection, object, str]] = []
            for connection in snapshot:
                for spec in connection.tools:
                    if not spec.name or not _tool_allowed(connection, spec.name):
                        continue
                    if (
                        not valid_schema(spec.input_schema)
                        or spec.input_schema.get("type", "object") != "object"
                    ):
                        self.api.ui.print(
                            f"[mcp: {connection.name}/{spec.name} has an unsupported input schema; skipped]",
                            "warn",
                        )
                        continue
                    candidates.append((connection, spec, tool_name(connection.name, spec.name)))

            base_counts = Counter(base for _, _, base in candidates)
            entries = []
            seen: set[str] = set()
            counts: dict[str, int] = {connection.name: 0 for connection in snapshot}
            for connection, spec, base_name in candidates:
                name = base_name
                if _norm(connection.name) in colliding_servers or base_counts[base_name] > 1:
                    alt = _disambiguated_tool_name(connection.name, spec.name)
                    if alt != name:
                        key = (connection.name, spec.name, alt)
                        if key not in self._warned_collisions:
                            self._warned_collisions.add(key)
                            self.api.ui.print(
                                f"[mcp: renamed colliding tool {connection.name}/{spec.name} "
                                f"from {name!r} to {alt!r}]",
                                "warn",
                            )
                        name = alt
                if name in seen:
                    key = (connection.name, spec.name, f"skip:{name}")
                    if key not in self._warned_collisions:
                        self._warned_collisions.add(key)
                        self.api.ui.print(
                            f"[mcp: skipped duplicate tool {connection.name}/{spec.name} ({name!r})]",
                            "warn",
                        )
                    continue
                seen.add(name)
                counts[connection.name] = counts.get(connection.name, 0) + 1
                schema = deepcopy(spec.input_schema or {"type": "object", "properties": {}})

                def execute(args, ctx, _connection=connection, _tool=spec.name, _name=name):
                    try:
                        result = _connection.call(_tool, args, ctx.signal)
                    except MCPCallCancelled as error:
                        return ToolResult.error(
                            str(error),
                            details={
                                "mcp": True,
                                "server": _connection.name,
                                "tool": _name,
                                "status": "aborted",
                                "cancelled": True,
                                "boundary": _connection.boundary,
                            },
                        )
                    except Exception as error:
                        detail = f"{_connection.name}: {safe_text(error, secrets=_connection._secrets)}"
                        stderr = _connection.stderr_tail() if not _connection.ready else ""
                        if stderr and stderr not in detail:
                            detail += f"\n{stderr}"
                        extra = {
                            "mcp": True,
                            "server": _connection.name,
                            "tool": _name,
                            "boundary": _connection.boundary,
                        }
                        if stderr:
                            extra["stderr"] = stderr
                        return ToolResult.error(detail, details=extra)
                    return ToolResult(
                        _content(result),
                        details={
                            "mcp": True,
                            "server": _connection.name,
                            "tool": _name,
                            "boundary": _connection.boundary,
                        },
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
                            config_fingerprint=connection.fingerprint,
                        ),
                        ("main", "subagent"),
                    )
                )
            with self._lock:
                if self._closed:
                    return
                self._published_counts = counts
            self.api.replace_tools("mcp", entries)

    def _cancel_startup(self) -> None:
        with self._lock:
            cancel = self._startup_cancel
            thread = self._startup_thread
        if cancel is not None:
            cancel.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)

    def start(self, *, signal=None, wait: float | None = STARTUP_WAIT) -> None:
        config = self.load_config()
        if not config:
            with self._lock:
                self._generation += 1
                self.configured = []
                self.connections = {}
                self.failures = {}
                self._connecting.clear()
                self._published_counts = {}
                self._startup_done.set()
            self._publish()
            return

        cancel = threading.Event()
        done = threading.Event()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self.configured = list(config)
            self.connections = {}
            self.failures = {}
            self._connecting = set(config)
            self._published_counts = {}
            self._startup_cancel = cancel
            self._startup_done = done

        def worker() -> None:
            try:
                with ThreadPoolExecutor(max_workers=min(8, len(config))) as pool:
                    futures = [
                        pool.submit(self._connect_one, name, server, signal=cancel)
                        for name, server in config.items()
                    ]
                    for future in as_completed(futures):
                        name, connection, failure = future.result()
                        stale = False
                        with self._lock:
                            if self._closed or self._generation != generation:
                                stale = True
                            else:
                                self._connecting.discard(name)
                                if connection is not None:
                                    self.connections[name] = connection
                                else:
                                    self.failures[name] = failure or "connection failed"
                        if stale:
                            if connection is not None:
                                connection.close()
                        else:
                            self._publish()
            finally:
                with self._lock:
                    if self._generation == generation:
                        self._connecting.clear()
                done.set()

        thread = threading.Thread(target=worker, daemon=True, name="mcp-startup")
        with self._lock:
            self._startup_thread = thread
        self._publish()
        thread.start()
        if wait == 0:
            return
        deadline = None if wait is None else time.monotonic() + wait
        while not done.wait(0.05):
            if signal is not None and signal.is_set():
                cancel.set()
                break
            if deadline is not None and time.monotonic() >= deadline:
                break

    def reconnect(self, *, signal=None) -> bool:
        # S4-04: Never hold self._lock across _connect_all(), retire(), or close().
        self._cancel_startup()
        config = self.load_config()
        candidates, failures = self._connect_all(config, signal=signal)
        if failures:
            with self._lock:
                self.configured = list(config)
                self.failures = failures
            for connection in candidates.values():
                connection.close()
            return False
        with self._lock:
            self._generation += 1
            previous = list(self.connections.values())
            self.connections = candidates
            self.configured = list(config)
            self.failures = {}
            self._connecting.clear()
            self.retired = [conn for conn in self.retired if not conn.closed] + previous
        self._publish()
        for connection in previous:
            connection.retire()
        return True

    def status(self) -> list[dict]:
        with self._lock:
            names = list(dict.fromkeys([*self.configured, *self.connections, *self.failures]))
            connections = dict(self.connections)
            failures = dict(self.failures)
            connecting = set(self._connecting)
            published_counts = dict(self._published_counts)
        rows = []
        for name in names:
            connection = connections.get(name)
            if connection is not None and connection.ready:
                rows.append(
                    {
                        "server": name,
                        "state": "ready",
                        "tools": published_counts.get(name, len(connection.tools)),
                        "error": None,
                        "boundary": connection.boundary,
                    }
                )
                continue
            if name in connecting:
                rows.append(
                    {
                        "server": name,
                        "state": "connecting",
                        "tools": 0,
                        "error": None,
                    }
                )
                continue
            error = failures.get(name)
            if error is None and connection is not None and connection.error is not None:
                error = safe_text(connection.error, secrets=connection._secrets)
            row = {
                "server": name,
                "state": "unavailable",
                "tools": 0,
                "error": error or "not connected",
            }
            if connection is not None:
                row["boundary"] = connection.boundary
                if stderr := connection.stderr_tail():
                    row["stderr"] = stderr
            rows.append(row)
        return rows

    def status_tool(self, args: dict, ctx) -> ToolResult:
        # S4-07: Works safely even when ctx.origin is None.
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
                with self._lock:
                    failures = dict(self.failures)
                for name, error in failures.items():
                    self.api.ui.print(f"  {name}: error: {error}", "error")
        rows = self.status()
        if not rows:
            self.api.ui.print("No MCP servers configured (~/.virgo/mcp.json or trusted .virgo/mcp.json).")
            return
        for row in rows:
            if row["state"] == "connecting":
                state = "connecting..."
            elif row["error"]:
                state = f"error: {row['error']}"
            else:
                state = f"{row['tools']} tools"
            self.api.ui.print(f"  {row['server']}: {state}")

    def close(self) -> None:
        # S4-04: Snapshot and clear under lock, then close outside lock.
        with self._lock:
            self._closed = True
            self._generation += 1
            cancel = self._startup_cancel
            thread = self._startup_thread
            to_close = [*self.connections.values(), *self.retired]
            self.connections.clear()
            self.retired.clear()
            self._connecting.clear()
        if cancel is not None:
            cancel.set()
        for connection in to_close:
            connection.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)


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
            execution_scope="broker",
        ),
        roles=("main", "subagent"),
    )
    api.register_command(
        "mcp",
        manager.command,
        "show MCP servers (/mcp reconnect)",
        argument_hint="[reconnect]",
    )
