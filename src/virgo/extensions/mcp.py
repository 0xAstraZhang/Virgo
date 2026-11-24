"""Built-in MCP extension: connect configured stdio and HTTP servers and expose their tools.

Loaded automatically as a built-in extension. Disable with
``extensions.disabled: ["mcp"]`` or ``--no-extensions``, or override with
``~/.virgo/extensions/mcp.py`` (or a trusted ``.virgo/extensions/mcp.py``).

Servers are configured in ``~/.virgo/mcp.json`` (or ``.virgo/mcp.json`` in a
trusted project), or under the ``mcpServers`` setting, using the common format::

    {"mcpServers": {"github": {"command": "npx",
                               "args": ["-y", "@modelcontextprotocol/server-github"],
                               "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}}

The official MCP Python SDK is imported lazily only when at least one server is
configured, so sessions without MCP servers incur zero SDK import overhead.

The Host integration launches stdio servers via SandboxExecutor and connects
to configured HTTP endpoints. Main and subagent runtimes share each connection.
Mutating MCP tools remain subject to permission approvals bound to the server's config
fingerprint. Tools become ``mcp__<server>__<tool>``. ``/mcp`` shows connection
state and ``/mcp reconnect`` reloads the configuration; ``mcp_status`` is
exposed dynamically whenever servers are configured.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
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
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, unquote_plus, urlsplit

from virgo import __version__, diagnostics
from virgo.agent import AgentTool, ToolResult
from virgo.ai.types import ImageContent, TextContent
from virgo.ai.validate import valid_schema
from virgo.diagnostics import capture_error, safe_text
from virgo.execution import HostContext, ProcessRequest, cleanup_process
from virgo.harness.settings import same_directory
from virgo.tools.output import budget, exceeds_budget

if TYPE_CHECKING:
    from mcp import ClientSession, types

CALL_TIMEOUT = 300.0
STARTUP_WAIT = 2.0
MAX_TOOL_PAGES = 100
_STDERR_LIMIT = 8192
_MESSAGE_LIMIT = 8 * 1024 * 1024
_ENV_VAR = re.compile(r"\$\{(\w+)\}")
_SECRET_NAME = re.compile(r"token|password|secret|credential|api[-_]?key|authorization", re.I)


@dataclass(frozen=True)
class _MCPSDK:
    anyio: Any
    client_session: Any
    types: Any
    mcp_error: Any
    session_message: Any
    implementation: Any
    jsonrpc: Any


_SDK_LOCK = threading.Lock()
_SDK: _MCPSDK | None = None


def _sdk() -> _MCPSDK:
    """Import the MCP SDK on first actual server connection to keep cold starts fast."""
    global _SDK
    if _SDK is not None:
        return _SDK
    with _SDK_LOCK:
        if _SDK is None:
            import anyio
            from mcp import ClientSession, types
            from mcp.shared.exceptions import MCPError
            from mcp.shared.message import SessionMessage
            from mcp.types import Implementation
            from pydantic import TypeAdapter

            _SDK = _MCPSDK(
                anyio=anyio,
                client_session=ClientSession,
                types=types,
                mcp_error=MCPError,
                session_message=SessionMessage,
                implementation=Implementation,
                jsonrpc=TypeAdapter(types.JSONRPCMessage),
            )
    return _SDK


class MCPConnectionError(Exception):
    def __init__(self, message: str, *, stderr: str = "", boundary: dict | None = None):
        super().__init__(message)
        self.stderr = stderr
        self.boundary = boundary


class MCPCallCancelled(Exception):
    pass


def _expand(value: str) -> str:
    return _ENV_VAR.sub(lambda match: os.environ.get(match.group(1), ""), value)


def _validate_tool_patterns(value, field: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise MCPConnectionError(f"MCP server {field} must be a list of nonempty strings")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class _StdioEndpoint:
    command: str
    args: list[str]
    env: dict[str, str]


@dataclass(frozen=True)
class _URLEndpoint:
    transport: str
    url: str
    headers: dict[str, str]


def _timeout(config: dict, field: str, default: float) -> float:
    value = config.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise MCPConnectionError(f"MCP server {field} must be a positive number")
    return float(value)


def _stdio_endpoint(config: dict) -> _StdioEndpoint:
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

    return _StdioEndpoint(command, args, env)


class _RepeatedKeys(dict):
    """A JSON object that contained duplicate member names before conversion to a dict."""


def _json_object(pairs):
    obj = dict(pairs)
    return obj if len(obj) == len(pairs) else _RepeatedKeys(obj)


def _url_endpoint(config: dict, transport: str) -> _URLEndpoint:
    url = config["url"]
    if not isinstance(url, str) or not url or re.search(r"[\s\x00-\x1f\x7f-\x9f#]", url):
        raise MCPConnectionError("MCP server url must be an absolute HTTP(S) URL without whitespace, controls or fragments")
    try:
        parts = urlsplit(url)
        authority = parts.netloc.rsplit("@", 1)[-1]
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or "\\" in authority
            or not re.fullmatch(r"(?:\[[^\]]+\]|[^:\[\]]+)(?::[0-9]+)?", authority)
        ):
            raise ValueError
        _ = parts.port  # urlsplit validates the port range on access.
        parts.hostname.encode("idna")
        url.encode("utf-8")
    except ValueError:
        raise MCPConnectionError("MCP server url requires an HTTP(S) scheme, hostname and valid port") from None

    raw = config.get("headers", {})
    if not isinstance(raw, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in raw.items()):
        raise MCPConnectionError("MCP server headers must be an object mapping strings to strings")
    if isinstance(raw, _RepeatedKeys):
        raise MCPConnectionError("MCP server headers contain a duplicate name")
    headers = {}
    variables = {var for value in raw.values() for var in _ENV_VAR.findall(value)}
    missing = sorted(variables.difference(os.environ))
    if missing:
        raise MCPConnectionError(f"MCP server headers reference unset environment variables: {', '.join(missing)}")
    for name, value in raw.items():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise MCPConnectionError("MCP server headers contain an invalid HTTP field name")
        if name.lower() in headers:
            raise MCPConnectionError(f"MCP server headers contain a duplicate name: {name}")
        expanded = _expand(value)
        if not re.fullmatch(r"[\t\x20-\x7e]*", expanded) or expanded != expanded.strip(" \t"):
            raise MCPConnectionError(f"MCP server headers contain an invalid HTTP field value for {name}")
        headers[name.lower()] = expanded
    return _URLEndpoint(transport, url, headers)


@dataclass(frozen=True)
class _ServerConfig:
    endpoint: _StdioEndpoint | _URLEndpoint
    timeout: float
    call_timeout: float
    include_tools: tuple[str, ...] | None
    exclude_tools: tuple[str, ...]


def _validate_config(config: dict) -> _ServerConfig:
    if not isinstance(config, dict):
        raise MCPConnectionError("MCP server configuration must be a JSON object")
    if ("command" in config) == ("url" in config):
        raise MCPConnectionError("MCP server requires exactly one of command and url")
    stdio = "command" in config
    selectors = {}
    for key in ("type", "transport"):
        if key in config:
            value = config[key]
            if not isinstance(value, str) or value not in {"stdio", "http", "streamable-http", "sse"}:
                raise MCPConnectionError(f"MCP server {key} is not a supported transport")
            selectors[key] = "streamable-http" if value == "http" else value
    if len(set(selectors.values())) > 1:
        raise MCPConnectionError("MCP server type and transport disagree")
    transport = next(iter(selectors.values()), "stdio" if stdio else "streamable-http")
    if (transport == "stdio") != stdio:
        raise MCPConnectionError(f"MCP server {'/'.join(selectors)} conflicts with the configured endpoint")
    for field in (("headers",) if stdio else ("args", "env")):
        if field in config:
            raise MCPConnectionError(f"MCP server {field} is incompatible with {transport}")
    endpoint = _stdio_endpoint(config) if stdio else _url_endpoint(config, transport)
    return _ServerConfig(
        endpoint=endpoint,
        timeout=_timeout(config, "timeout", 30.0),
        call_timeout=_timeout(config, "callTimeout", CALL_TIMEOUT),
        include_tools=_validate_tool_patterns(config.get("includeTools"), "includeTools"),
        exclude_tools=_validate_tool_patterns(config.get("excludeTools"), "excludeTools") or (),
    )


def _config_fingerprint(endpoint: _StdioEndpoint | _URLEndpoint) -> str:
    if isinstance(endpoint, _StdioEndpoint):
        # Preserve the serialization used for existing persistent approvals.
        fields = {"command": endpoint.command, "args": endpoint.args, "env": sorted(endpoint.env.items())}
    else:
        fields = {"transport": endpoint.transport, "url": endpoint.url, "headers": sorted(endpoint.headers.items())}
    payload = json.dumps(
        fields,
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
    found = {_expand(value) for value in [*values, *sensitive]}
    headers = config.get("headers")
    if isinstance(headers, dict):
        for name, value in headers.items():
            if not isinstance(value, str):
                continue
            expanded = _expand(value)
            components = [expanded, *(os.environ.get(var, "") for var in _ENV_VAR.findall(value))]
            for component in components:
                found.update((component, unquote(component)))
            if isinstance(name, str) and name.lower() in {"authorization", "proxy-authorization"}:
                # Peers may quote the credential without its authentication scheme.
                auth_parts = expanded.split(maxsplit=1)
                if len(auth_parts) == 2:
                    credential = auth_parts[1]
                    found.update(form for form in (credential, unquote(credential)) if len(form) >= 3)
    url = config.get("url")
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
        except ValueError:
            pass  # URL validation errors never include the supplied URL.
        else:
            for value in (parts.username, parts.password):
                if value:
                    found.update((value, unquote(value)))
            for pair in parts.query.split("&"):
                value = pair.partition("=")[2]
                found.update((value, unquote(value), unquote_plus(value)))
    return tuple(found - {""})


def _host_base_env() -> dict[str, str]:
    """Host environment excluding exported shell functions."""
    return {key: value for key, value in os.environ.items() if not value.startswith("()")}


class _StdioTransport:
    """Own the Host child and all three process pipes for one stdio connection."""

    def __init__(self, endpoint: _StdioEndpoint, context: HostContext):
        self.endpoint = endpoint
        self.context = context
        self._process = None
        self._process_cleaned = False
        self._stderr_buffer = bytearray()
        self._stderr_lock = threading.Lock()

    @property
    def running(self):
        return self._process is not None and self._process.poll() is None

    @property
    def boundary(self):
        return getattr(self._process, "_virgo_boundary", None) or {
            **self.context.describe(),
            "payload_started": self._process is not None,
            "side_effects": "possible" if self._process is not None else "none",
            "cleanup": "executor_completed" if self._process_cleaned else "pending",
        }

    def stderr_tail(self):
        with self._stderr_lock:
            return bytes(self._stderr_buffer).decode("utf-8", errors="replace")

    @asynccontextmanager
    async def open(self, name, cancel, disconnected):
        read_fd, write_fd = os.pipe()
        with os.fdopen(write_fd, "wb", buffering=0) as stderr:
            reader = threading.Thread(target=self._drain_stderr, args=(read_fd,), daemon=True, name=f"mcp-{name}-stderr")
            reader.start()
            try:
                request = self._request(stderr)
                self._process = self.context.run_process(request, cancel)
                async with self._streams(disconnected) as streams:
                    yield streams
            finally:
                self._terminate()
                stderr.close()
                reader.join()

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

    def _request(self, stderr) -> ProcessRequest:
        base_env = _host_base_env()
        merged_for_lookup = {**base_env, **self.endpoint.env}
        argv = _resolve_host_argv(self.endpoint.command, self.endpoint.args, merged_for_lookup)
        return ProcessRequest(
            argv=argv,
            cwd=self.context.workspace,
            env=base_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env_overrides=self.endpoint.env,
        )

    def _terminate(self) -> None:
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
    async def _streams(self, disconnected):
        """Bounded newline JSON-RPC framing bridged over SandboxExecutor pipes."""
        sdk = _sdk()
        received, read = sdk.anyio.create_memory_object_stream(0)
        write, outgoing = sdk.anyio.create_memory_object_stream(0)
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
                            line = bytes(pending[:end]).rstrip(b"\r")
                            del pending[: end + 1]
                            if line:
                                await received.send(sdk.session_message(sdk.jsonrpc.validate_json(line)))
                        if len(pending) > _MESSAGE_LIMIT:
                            raise MCPConnectionError("MCP response exceeded the message limit")
                    if pending:
                        raise MCPConnectionError("MCP server closed with an incomplete message")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                disconnected(error)
            else:
                disconnected(MCPConnectionError("MCP server closed its transport"))

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
                disconnected(error)
                await received.aclose()

        tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
        try:
            yield {"read_stream": read, "write_stream": write}
        finally:
            try:
                await asyncio.to_thread(self._terminate)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for stream in (read, write, received, outgoing):
                    await stream.aclose()


# The SDK and httpx2 log URLs and exception tracebacks before control returns
# to Virgo. Each connection owns its loop thread, so concurrent servers use
# their own credentials even for SDK background tasks.
_http_log_context = threading.local()


def _redact_http_log(record: logging.LogRecord) -> bool:
    secrets = getattr(_http_log_context, "secrets", None)
    if secrets is not None:
        record.msg = safe_text(record.getMessage(), secrets=secrets)
        record.args = ()
        if record.exc_info:
            record.exc_text = safe_text(logging.Formatter().formatException(record.exc_info), secrets=secrets)
            record.exc_info = None
    return True


@asynccontextmanager
async def _url_transport(config: _ServerConfig, secrets: tuple[str, ...], disconnected):
    import httpx2
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher

    endpoint = config.endpoint
    assert isinstance(endpoint, _URLEndpoint)
    read_timeout = max(config.call_timeout, CALL_TIMEOUT)

    _http_log_context.secrets = secrets
    for name in ("httpx2", "mcp.client.sse", "mcp.client.streamable_http", "mcp.shared.jsonrpc_dispatcher", "client"):
        logging.getLogger(name).addFilter(_redact_http_log)

    # Legacy SSE's SDK writer logs POST failures without resolving the request.
    # Observe dispatch failures before the writer consumes them.
    class HTTPClient(httpx2.AsyncClient):
        async def send(self, request, **kwargs):
            try:
                response = await super().send(request, **kwargs)
            except httpx2.RequestError as error:
                if request.method == "POST":
                    disconnected(error)
                raise
            if request.method == "POST" and (
                response.is_redirect or (endpoint.transport == "sse" and not response.is_success)
            ):
                disconnected(MCPConnectionError(f"MCP POST returned HTTP {response.status_code}"))
            return response

    def client_factory(**kwargs):
        return HTTPClient(max_redirects=0, **kwargs)

    class Dispatcher(JSONRPCDispatcher):
        async def run(self, *args, **kwargs):
            # The SDK resolves pending calls on EOF. Also retire an idle connection.
            await super().run(*args, **kwargs)
            disconnected(MCPConnectionError("MCP server closed its transport"))

    async with AsyncExitStack() as stack:
        if endpoint.transport == "sse":
            transport = sse_client(
                endpoint.url, headers=endpoint.headers, timeout=config.timeout,
                sse_read_timeout=read_timeout, httpx_client_factory=client_factory,
            )
        else:
            timeout = httpx2.Timeout(config.timeout, read=read_timeout)
            client = await stack.enter_async_context(client_factory(headers=endpoint.headers, timeout=timeout))
            transport = streamable_http_client(endpoint.url, http_client=client)
        read, write = await stack.enter_async_context(transport)
        yield {"dispatcher": Dispatcher(read, write)}


class _Connection:
    """One shared session. Its loop thread owns and unwinds the SDK contexts."""

    def __init__(self, name: str, config: dict, context: HostContext, *, signal=None):
        self.name = name
        self.config = _validate_config(config)
        endpoint = self.config.endpoint
        self.fingerprint = _config_fingerprint(endpoint)
        self._secrets = _secrets(config)
        self._stdio = _StdioTransport(endpoint, context) if isinstance(endpoint, _StdioEndpoint) else None
        self.tools: list = []
        self.error: BaseException | None = None
        self.inflight = 0
        self.retired = False
        self._closed = False
        self._lock = threading.Lock()
        self._launch_signal = signal
        self._cancel = threading.Event()
        self._ready = threading.Event()
        self._done = threading.Event()
        self._session: ClientSession | None = None
        self._thread = None
        self._loop = None
        self._owner = None
        self._stop = None

    def _run(self):
        try:
            with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
                runner.run(self._serve())
        finally:
            self._ready.set()
            self._done.set()

    def _disconnected(self, error):
        if not self._closed and self.error is None:
            self.error = error
            self._stop.set()
            if not self._ready.is_set():
                self._owner.cancel()

    async def _serve(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
            self._owner = asyncio.current_task()
            self._stop = asyncio.Event()
            if self._cancelled():
                return
            sdk = _sdk()
            if self._stdio:
                transport = self._stdio.open(self.name, self._cancel, self._disconnected)
            else:
                transport = _url_transport(self.config, self._secrets, self._disconnected)
            async with transport as session_args:
                session = sdk.client_session(**session_args, client_info=sdk.implementation(name="virgo", version=__version__))
                async with session:
                    await session.initialize()
                    self.tools = await self._list_tools(session)
                    if self.error:
                        raise self.error
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as error:
            if self.error is None:
                while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
                    error = error.exceptions[0]
                self.error = error

    @staticmethod
    async def _list_tools(session: ClientSession) -> list:
        sdk = _sdk()
        tools: list = []
        cursor = None
        for _ in range(MAX_TOOL_PAGES):
            params = sdk.types.PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_tools(params=params)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools
        raise MCPConnectionError("tools/list exceeded the pagination limit")

    def _cancelled(self) -> bool:
        return self._cancel.is_set() or (self._launch_signal is not None and self._launch_signal.is_set())

    def connect(self, timeout: float | None = None) -> None:
        wait_timeout = self.config.timeout if timeout is None else timeout
        try:
            with self._lock:
                if self._closed or self._cancelled():
                    raise MCPCallCancelled(f"{self.name}: connection cancelled before start")
                self._thread = threading.Thread(target=self._run, daemon=True, name=f"mcp-{self.name}")
                self._thread.start()
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
            if not self.ready:
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
        return self._stdio is None or self._stdio.running

    @property
    def boundary(self) -> dict:
        if self._stdio:
            return self._stdio.boundary
        return {"provider": "host", "transport": self.config.endpoint.transport}

    def stderr_tail(self) -> str:
        return safe_text(self._stdio.stderr_tail(), secrets=self._secrets) if self._stdio else ""

    def _is_transport_failure(self, error: BaseException) -> bool:
        """Distinguish JSON-RPC application errors from broken transports."""
        if not self.ready:
            return True
        sdk = _sdk()
        if isinstance(error, sdk.mcp_error):
            err_data = getattr(error, "error", None)
            code = getattr(err_data, "code", None)
            return code == sdk.types.CONNECTION_CLOSED or (
                isinstance(self.config.endpoint, _URLEndpoint) and self.config.endpoint.transport == "streamable-http"
                and code == sdk.types.INVALID_REQUEST and err_data.message == "Session terminated"
            )
        return isinstance(
            error,
            (
                MCPConnectionError,
                TimeoutError,
                BrokenPipeError,
                ConnectionError,
                EOFError,
                OSError,
                sdk.anyio.ClosedResourceError,
                sdk.anyio.BrokenResourceError,
                sdk.anyio.EndOfStream,
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
            if self.error is not None:
                raise MCPConnectionError(
                    f"{self.name}: {safe_text(self.error, secrets=self._secrets)}. Run /mcp reconnect to restart it.",
                    stderr=self.stderr_tail(),
                    boundary=self.boundary,
                )
            if self.retired:
                raise MCPConnectionError(
                    f"{self.name}: connection replaced; retry with the refreshed tool set",
                    boundary=self.boundary,
                )
            self.inflight += 1
        transport_failed = False
        try:
            assert self._session is not None
            coroutine = self._session.call_tool(tool, arguments)
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
            except RuntimeError as error:
                coroutine.close()
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
            deadline = time.monotonic() + self.config.call_timeout
            while True:
                if signal is not None and signal.is_set():
                    future.cancel()
                    raise MCPCallCancelled(f"{self.name}: cancelled {tool}")
                try:
                    return future.result(0.05)
                except FutureTimeout:
                    if self._done.is_set():
                        transport_failed = True
                        raise MCPConnectionError(f"{self.name}: connection closed. Run /mcp reconnect.") from None
                    if time.monotonic() >= deadline:
                        future.cancel()
                        transport_failed = True
                        error = TimeoutError(
                            f"{self.name}: tool call timed out after {self.config.call_timeout:g}s; "
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
                        raise MCPConnectionError(
                            f"{safe_text(self.error or error, secrets=self._secrets)}. Run /mcp reconnect."
                        ) from error
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
            self._closed = True
            self._cancel.set()
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return

        def stop():
            if self._ready.is_set():
                self._stop.set()
            else:
                self._owner.cancel()

        loop = self._loop
        if loop is not None and not self._done.is_set():
            try:
                loop.call_soon_threadsafe(stop)
            except RuntimeError:
                pass  # The loop already closed.
        thread.join(1)
        if thread.is_alive():
            # An unresponsive stateful peer can also hang the SDK's final DELETE.
            if self._loop is not None and self._owner is not None:
                try:
                    self._loop.call_soon_threadsafe(self._owner.cancel)
                except RuntimeError:
                    pass
            thread.join()


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
    if connection.config.include_tools is not None and not any(
        fnmatch.fnmatchcase(raw_name, pattern) for pattern in connection.config.include_tools
    ):
        return False
    return not any(fnmatch.fnmatchcase(raw_name, pattern) for pattern in connection.config.exclude_tools)


def _content(result: types.CallToolResult, cwd: Path, name: str, *, t, execution_context, secrets) -> ToolResult:
    converted = ToolResult([], is_error=bool(result.is_error))
    sensitive = diagnostics._secrets(secrets) if result.is_error else ()

    def add_text(text: str) -> None:
        if result.is_error:
            # Redact the full block before budgeting so read_output is safe and complete.
            try:
                text = diagnostics._redact(text, sensitive)
            except Exception:
                text = "[text unavailable: diagnostic redaction failed]"
        if execution_context is None:
            if exceeds_budget(text):
                text = "The MCP tool already ran. Storing its oversized output requires an originating runtime."
                converted.is_error = True
        else:
            try:
                text = budget(text, cwd, name, t=t, execution_context=execution_context)
            except Exception:
                text = "The MCP tool already ran, but its oversized output could not be stored."
                converted.is_error = True
        converted.content.append(TextContent(text))

    for item in result.content:
        if item.type == "text":
            add_text(item.text)
        elif item.type == "image":
            converted.content.append(ImageContent(item.data, item.mime_type))
        elif item.type == "audio":
            add_text(f"[audio {item.mime_type}]")
        elif item.type == "resource":
            resource = item.resource
            add_text(getattr(resource, "text", f"[resource {resource.uri}]"))
        elif item.type == "resource_link":
            add_text(f"[resource {item.uri}]")
        else:
            payload = item.model_dump(mode="json", by_alias=True, exclude_none=True)
            add_text(json.dumps(payload, ensure_ascii=False))
    if not converted.content:
        structured = result.structured_content
        add_text(json.dumps(structured, ensure_ascii=False) if structured is not None else "(no content)")
    return converted


class _Manager:
    def __init__(self, api):
        self.api = api
        self._lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self.connections: dict[str, _Connection] = {}
        self.failures: dict[str, str] = {}
        self.configured: list[str] = []
        self.retired: list[_Connection] = []
        self._reconnecting: set[_Connection] = set()
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
        self._status_published = False
        self._status_tool = AgentTool(
            "mcp_status",
            "Read local MCP connection state and tool counts. Does not contact servers.",
            {
                "type": "object",
                "properties": {"server": {"type": "string"}},
                "additionalProperties": False,
            },
            self.status_tool,
            side_effect="read",
            execution_scope="broker",
        )

    def _project_trusted(self) -> bool:
        return bool(self.api.project_trusted)

    def _host_context(self) -> HostContext:
        return self.api.host_context()

    def _read_servers_file(self, path: Path, *, from_settings: bool = False) -> dict:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_object)
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

    def _report_failure(self, name: str, error: Exception, secrets: tuple[str, ...]) -> str:
        if not isinstance(error, MCPCallCancelled):
            context = {"server": name}
            if isinstance(error, MCPConnectionError):
                if error.stderr:
                    context["stderr"] = error.stderr
                if error.boundary is not None:
                    context["boundary"] = error.boundary
            record = capture_error(error, phase="mcp.connect", context=context, secrets=secrets)
            self.api.report_error(record, phase="mcp.connect", notify=False)
        return safe_text(error, secrets=secrets)

    def _prepare(self, config: dict, signal) -> tuple[list[_Connection], dict[str, str]]:
        pending = []
        failures = {}
        for name, fields in config.items():
            try:
                pending.append(_Connection(name, fields, self._host_context(), signal=signal))
            except Exception as error:
                failures[name] = self._report_failure(name, error, _secrets(fields))
        return pending, failures

    def _connect_one(self, connection: _Connection) -> tuple[str, _Connection | None, str | None]:
        try:
            connection.connect()
            return connection.name, connection, None
        except Exception as error:
            return connection.name, None, self._report_failure(connection.name, error, connection._secrets)

    def _connect_all(self, pending: list[_Connection]):
        if pending:
            with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
                futures = [pool.submit(self._connect_one, conn) for conn in pending]
                for future in as_completed(futures):
                    yield future.result()

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
                            "error": capture_error(
                                error,
                                phase="tool.execute",
                                context={
                                    "agent": ctx.agent,
                                    "tool_name": _name,
                                    "tool_call_id": ctx.call_id,
                                    "server": _connection.name,
                                },
                                secrets=_connection._secrets,
                                message=detail,
                            ),
                        }
                        if stderr:
                            extra["stderr"] = stderr
                        return ToolResult.error(detail, details=extra)
                    runtime = self.api.runtime_for_context(ctx)
                    converted = _content(
                        result, ctx.cwd, _name, t=ctx.translator or self.api.translator,
                        execution_context=runtime.execution_context if runtime is not None else None,
                        secrets=_connection._secrets,
                    )
                    converted.details = {
                        "mcp": True,
                        "server": _connection.name,
                        "tool": _name,
                        "boundary": _connection.boundary,
                    }
                    return converted

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
            self._sync_status_tool_locked()

    def _sync_status_tool_locked(self) -> None:
        with self._lock:
            if self._closed:
                return
            should_publish = bool(self.configured or self.connections or self.failures)
        if should_publish == self._status_published:
            return
        entries = [(self._status_tool, ("main", "subagent"))] if should_publish else []
        self.api.replace_tools("mcp_status", entries)
        self._status_published = should_publish

    def _sync_status_tool(self) -> None:
        with self._publish_lock:
            self._sync_status_tool_locked()

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
        cancel = threading.Event()
        pending, failures = self._prepare(config, cancel)
        done = threading.Event()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self.configured = list(config)
            self.connections = {}
            self.failures = failures
            self._connecting = {conn.name for conn in pending}
            self._published_counts = {}
            self._startup_cancel = cancel
            self._startup_done = done

        def worker() -> None:
            try:
                for name, connection, failure in self._connect_all(pending):
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

        if not pending:
            done.set()
            self._publish()
            return
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
        pending, failures = self._prepare(config, signal)
        with self._lock:
            if self._closed:
                return False
            self._reconnecting.update(pending)
        candidates = {}
        try:
            if not failures:
                for name, connection, failure in self._connect_all(pending):
                    if connection is None:
                        failures[name] = failure or "connection failed"
                    else:
                        candidates[name] = connection
            with self._lock:
                if self._closed:
                    return False  # close() took ownership of the pending connections.
                self.configured = list(config)
                self.failures = failures
                if not failures:
                    self._generation += 1
                    previous = list(self.connections.values())
                    self.connections = candidates
                    self._connecting.clear()
                    self.retired = [conn for conn in self.retired if not conn.closed] + previous
            if failures:
                for connection in candidates.values():
                    connection.close()
                self._sync_status_tool()
                return False
        finally:
            with self._lock:
                self._reconnecting.difference_update(pending)
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
            to_close = {*self.connections.values(), *self.retired, *self._reconnecting}
            self._reconnecting.clear()
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
    api.register_command(
        "mcp",
        manager.command,
        "show MCP servers (/mcp reconnect)",
        argument_hint="[reconnect]",
    )
