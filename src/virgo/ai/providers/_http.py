"""Cancellable SDK streams and SSE transport for account APIs."""

from __future__ import annotations

import asyncio
import json
import queue
import re
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import AsyncExitStack, closing
from contextvars import ContextVar
from http.cookiejar import CookieJar

import anyio
import httpx

from ..stream import EventStream, StreamEvent, aborted_event


class ProviderError(Exception):
    def __init__(self, message: str, kind: str = "fatal", *, status_code=None, body=None,
                 request_id=None, retry_after=None, endpoint=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.body = body
        self.request_id = request_id
        self.retry_after = retry_after
        self.endpoint = endpoint


def cancelled(signal) -> bool:
    return signal is not None and signal.is_set()


class _StreamAborted(Exception):
    """Cancellation observed at a request boundary."""


class _HandshakeGuard:
    """httpcore2 does not own the new socket while AnyIO is upgrading it to TLS."""

    stream = None

    async def trace(self, name, info):
        if name in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
            # TLS to an HTTPS proxy can itself be the transport for a CONNECT tunnel.
            self.stream = info["return_value"]
        elif name == "http11.send_request_headers.started":
            # CONNECT upgrades this dedicated connection in place. The proxy's
            # start_tls trace does not expose its network_stream argument.
            if info["request"].method != b"CONNECT":
                self.stream = None
        elif name.startswith("http2.") or name == "proxy.start_tls.complete":
            self.stream = None
        elif name.endswith(".start_tls.failed") and isinstance(info["exception"], asyncio.CancelledError):
            if self.stream is not None:
                with anyio.CancelScope(shield=True):
                    await self.stream.aclose()
                self.stream = None


_request = ContextVar("_request", default=None)


def _track_sdk_iterators():
    """Keep nested SDK body iterators on their owner task through async close.

    OpenAI breaks out of its SSE decoder on [DONE]. Otherwise asyncio schedules
    a chain of async-generator finalizers after the task has reported completion.
    These hooks are local to the dedicated I/O thread.
    """
    hooks = sys.get_asyncgen_hooks()

    def firstiter(gen):
        request = _request.get()
        if request is not None and asyncio.current_task() is request.task:
            request.track(gen)
        hooks.firstiter(gen)

    sys.set_asyncgen_hooks(firstiter=firstiter)


async def _trace_request(request):
    active = _request.get()
    active.check_signal()
    request.extensions["trace"] = active.handshake.trace
    if active.on_dispatch is not None:
        active.on_dispatch()


class _NoClientCookies(CookieJar):
    def extract_cookies(self, response, request):
        """The shared connection pool must never retain response cookies."""


class _CodexRoute:
    # RFC 6265 cookie-octet, optionally quoted. Reject combined or malformed values.
    _cookie_pair = re.compile(rb'(__oailb|__cflb)=("?)[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*\2')

    def __init__(self):
        self.cookies = httpx.Cookies()
        self.turn_states = {}

    def apply(self, request, session):
        request.headers.pop("Cookie", None)
        request.headers.pop("Cookie2", None)
        self.cookies.set_cookie_header(request)
        if session in self.turn_states:
            request.headers.update([(b"x-codex-turn-state", self.turn_states[session])])

    def capture(self, response, session):
        allowed = []
        for name, value in response.headers.raw:
            if name.lower() == b"x-codex-turn-state":
                self.turn_states[session] = value
            elif (name.lower() == b"set-cookie" and len(value) <= 8192
                  and self._cookie_pair.fullmatch(value.partition(b";")[0].strip())):
                allowed.append((name, value))
        # Only the whitelisted fields may enter the endpoint's cookie jar. The
        # standard jar handles Domain, Path, Secure, Max-Age and Expires.
        response.headers.pop("set-cookie", None)
        response.headers.pop("set-cookie2", None)
        if allowed:
            response.headers.update(allowed)
            self.cookies.extract_cookies(response)


class AsyncSDKBridge:
    """One lazy I/O thread and SDK client pool shared by a session's requests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ready = Future()
        self._thread = None
        self._closed = False
        self._clients = {}
        self._http_client = None
        self._codex_routes = {}
        self._requests = set()

    def http_client(self):
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=15.0),
                event_hooks={"request": [_trace_request]},
                cookies=_NoClientCookies(),
            )
        return self._http_client

    def codex_route(self, request):
        url = request.url
        key = (url.scheme, url.host, url.port, url.raw_path.partition(b"?")[0],
               request.headers.get("chatgpt-account-id"))
        if key not in self._codex_routes:
            self._codex_routes[key] = _CodexRoute()
        return self._codex_routes[key]

    def client(self, key, sdk, factory, kwargs):
        # Only called by request tasks on the owner loop. Use the SDK's transport
        # defaults (including proxy mounts and timeouts) with a request-local trace.
        if key not in self._clients:
            http = sdk.DefaultAsyncHttpxClient(event_hooks={"request": [_trace_request]})
            self._clients[key] = factory(http_client=http, **kwargs)
        return self._clients[key]

    def _run(self):
        try:
            loop = asyncio.new_event_loop()
        except Exception as exc:
            self._ready.set_exception(exc)
            return
        try:
            asyncio.set_event_loop(loop)
            loop.call_soon(_track_sdk_iterators)
            self._ready.set_result(loop)
            loop.run_forever()
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            # loop.close() does not join a running getaddrinfo executor call.
            loop.close()

    def start(self, request):
        with self._lock:
            if self._closed:
                raise _StreamAborted()
            request.check_signal()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="virgo-sdk-io", daemon=True)
                self._thread.start()
            loop = self._ready.result()
            loop.call_soon_threadsafe(self._start, request)

    def _start(self, request):
        self._requests.add(request)
        request.task = asyncio.create_task(request.produce())
        request.task.add_done_callback(request.complete)

    def stop_request(self, request, *, drain_timeout: float = 0.0):
        with self._lock:
            if not self._closed and not request.finished.is_set():
                loop = self._ready.result()
                if drain_timeout > 0 and not cancelled(request.signal):
                    request.draining.set()
                    loop.call_soon_threadsafe(self._schedule_drain, request, drain_timeout)
                else:
                    loop.call_soon_threadsafe(request.cancel)
        if drain_timeout > 0:
            while not request.finished.wait(0.02):
                if cancelled(request.signal):
                    with self._lock:
                        if not self._closed and not request.finished.is_set():
                            self._ready.result().call_soon_threadsafe(request.cancel)
                    break
        request.finished.wait()

    def _schedule_drain(self, request, timeout: float):
        if request.finished.is_set():
            return
        request._drain_timer = asyncio.get_running_loop().call_later(timeout, request.cancel)

    async def _drain(self):
        requests = list(self._requests)
        for request in requests:
            request.aborted.set()
            request.cancel()
        await asyncio.gather(*(r.task for r in requests), return_exceptions=True)
        async with AsyncExitStack() as clients:
            if self._http_client is not None:
                clients.push_async_callback(self._http_client.aclose)
                self._http_client = None
            self._codex_routes.clear()
            for client in self._clients.values():
                clients.push_async_callback(client.close)
            self._clients.clear()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._thread is None:
                return
            loop = self._ready.result()
            try:
                asyncio.run_coroutine_threadsafe(self._drain(), loop).result()
            finally:
                loop.call_soon_threadsafe(loop.stop)
                self._thread.join()


class _SDKRequest:
    # Reserve the last slot for the task's completion, even under backpressure.
    capacity = 64
    end = object()
    drain_timeout = 0.2

    def __init__(self, bridge, signal, open_stream, on_dispatch=None):
        self.bridge = bridge
        self.signal = signal
        self.aborted = threading.Event()
        self.draining = threading.Event()
        self._drain_timeout = 0.0
        self._drain_timer = None
        self.open_stream = open_stream
        self.on_dispatch = on_dispatch
        self.items = queue.Queue(self.capacity + 1)
        self.finished = threading.Event()
        self.handshake = _HandshakeGuard()
        self.generators = []
        self.scope = self.task = None
        self.error = None
        self.client = self.response = None

    def allow_drain(self, timeout: float | None = None):
        self._drain_timeout = self.drain_timeout if timeout is None else timeout

    def check_signal(self):
        if cancelled(self.signal):
            self.aborted.set()
        if self.aborted.is_set():
            raise _StreamAborted()

    def track(self, gen):
        # Discard exhausted iterators during long responses. Only suspended
        # generators need an explicit aclose at the end of the request.
        if len(self.generators) >= 32:
            self.generators[:] = [g for g in self.generators if g.ag_frame is not None]
        self.generators.append(gen)

    async def close_generators(self):
        while self.generators:
            await self.generators.pop().aclose()

    async def produce(self):
        # httpcore shields pool bookkeeping with AnyIO cancel scopes. Cancelling
        # that scope respects its shields, including when EOF races cancellation.
        async with AsyncExitStack() as resources:
            _request.set(self)
            resources.push_async_callback(self.close_generators)
            self.scope = anyio.CancelScope()
            with self.scope:
                self.check_signal()
                stream = await self.open_stream(self, resources)
                async for item in stream:
                    if self.draining.is_set():
                        continue
                    while self.items.qsize() >= self.capacity:
                        self.check_signal()
                        if self.draining.is_set():
                            break
                        await asyncio.sleep(0.01)
                    if not self.draining.is_set():
                        self.items.put_nowait(item)
                # SDK decoders can break on [DONE] before HTTP/1.1 chunked EOF arrives.
                if (self.draining.is_set() and self.response is not None
                        and not getattr(self.response, "is_stream_consumed", True)):
                    async for _ in self.response.aiter_bytes():
                        pass
        if self.scope.cancel_called and not self.draining.is_set():
            raise _StreamAborted()

    def cancel(self):
        if self.scope is None:
            self.task.cancel()  # scheduled, but not started
        else:
            self.scope.cancel()

    def complete(self, task):
        if self._drain_timer is not None:
            self._drain_timer.cancel()
            self._drain_timer = None
        self.error = _StreamAborted() if task.cancelled() else task.exception()
        self.bridge._requests.discard(self)
        self.items.put_nowait(self.end)
        self.finished.set()

    def __iter__(self):
        self.bridge.start(self)
        try:
            while True:
                self.check_signal()
                try:
                    item = self.items.get(timeout=0.05)
                except queue.Empty:
                    continue
                self.check_signal()
                if item is self.end:
                    if self.error is not None:
                        raise self.error
                    return
                yield item
        finally:
            self.bridge.stop_request(self, drain_timeout=self._drain_timeout)


def sdk_stream(out, opts, get_client, create, parse, on_error):
    """Run the SDK on its loop and the existing parser on the synchronous caller."""
    async def open_stream(request, resources):
        request.client = get_client(request.bridge)
        request.check_signal()
        stream = await resources.enter_async_context(await create(request.client))
        request.response = stream.response
        return stream

    def events():
        bridge = opts.io_bridge or AsyncSDKBridge()
        request = _SDKRequest(bridge, opts.signal, open_stream, opts.on_dispatch)
        failure = terminal = None
        try:
            request.check_signal()
            with closing(iter(request)) as items, closing(parse(items, request)) as parsed:
                for event in parsed:
                    request.check_signal()
                    if event.type in ("done", "error"):
                        if event.type == "done":
                            request.allow_drain()
                        terminal = event
                        break
                    yield event
                    request.check_signal()
        except Exception as exc:
            failure = exc
        finally:
            if opts.io_bridge is None:
                bridge.close()

        if isinstance(failure, _StreamAborted) or request.aborted.is_set() or cancelled(opts.signal):
            yield aborted_event(out)
        elif failure is not None:
            on_error(failure, request)
            yield StreamEvent("error", out)
        elif terminal is not None:
            yield terminal

    return EventStream(events(), out)


def classify_error(error: Exception, token: str = "") -> tuple[str, str]:
    text = str(error)
    if token:
        text = text.replace(token, "[redacted]")
    if isinstance(error, ProviderError):
        return error.kind, text
    if isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return "retryable", "Connection to the model provider failed. Please retry."
    return "fatal", text


def _response_error(response: httpx.Response) -> ProviderError:
    status = response.status_code
    body = None
    try:
        body = response.json()
        error = body.get("error", body)
        message = error.get("message", "") if isinstance(error, dict) else str(error)
    except (ValueError, AttributeError):
        message = ""
    kind = "retryable" if status in (408, 409, 429) or status >= 500 else "fatal"
    if status in (400, 413) and re.search(
        r"context.length|context window|too many tokens|prompt is too long|input token.*exceed", message, re.I
    ):
        kind = "overflow"
    try:
        endpoint = str(response.request.url)
    except RuntimeError:
        endpoint = None
    return ProviderError(
        f"Provider HTTP {status}" + (f": {message}" if message else ""), kind,
        status_code=status, body=body,
        request_id=response.headers.get("x-request-id") or response.headers.get("request-id")
        or response.headers.get("x-goog-request-id"),
        retry_after=response.headers.get("retry-after"), endpoint=endpoint,
    )


def _parse_events(lines) -> Iterator[dict]:
    data: list[str] = []

    def decode():
        payload = "\n".join(data)
        data.clear()
        if not payload or payload == "[DONE]":
            return None
        try:
            event = json.loads(payload)
        except ValueError as error:
            raise ProviderError("The provider returned an invalid streaming event.") from error
        if not isinstance(event, dict):
            raise ProviderError("The provider returned an invalid streaming event.")
        return event

    for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            event = decode()
            if event is not None:
                yield event
    if data:
        event = decode()
        if event is not None:
            yield event


class _JsonEvents:
    def __init__(self, request: _SDKRequest, iterator: Iterator[dict]):
        self._request = request
        self._iterator = iterator

    def __iter__(self):
        return self

    def __next__(self) -> dict:
        return next(self._iterator)

    def close(self, *, drain: bool = False) -> None:
        if drain:
            self._request.allow_drain()
        self._iterator.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def json_events(url: str, headers: dict, body: dict, signal=None, *, io_bridge=None,
                codex_session: str | None = None, on_dispatch=None) -> _JsonEvents:
    """Keep the TLS read and response close on the same I/O task, including early SSE termination."""
    async def open_stream(request, resources):
        request.check_signal()
        client = bridge.http_client()
        outgoing = client.build_request("POST", url, headers=headers, json=body)
        route = bridge.codex_route(outgoing) if codex_session is not None else None
        if route is not None:
            session = codex_session or outgoing.headers.get("thread-id") or outgoing.headers.get("session-id")
            route.apply(outgoing, session)
        response = await client.send(outgoing, stream=True, follow_redirects=False)
        resources.push_async_callback(response.aclose)
        request.response = response
        if route is not None:
            route.capture(response, session)
        if response.status_code >= 400:
            await response.aread()
            raise _response_error(response)
        return response.aiter_lines()

    bridge = io_bridge or AsyncSDKBridge()
    request = _SDKRequest(bridge, signal, open_stream, on_dispatch)

    def iterate() -> Iterator[dict]:
        try:
            with closing(iter(request)) as lines:
                yield from _parse_events(lines)
        except _StreamAborted as error:
            raise ProviderError("Aborted by user.", "aborted") from error
        except Exception as error:
            response = request.response
            if response is not None:
                for name, value in {
                    "status_code": response.status_code,
                    "request_id": response.headers.get("x-request-id") or response.headers.get("request-id")
                    or response.headers.get("x-goog-request-id"),
                    "retry_after": response.headers.get("retry-after"),
                    "endpoint": str(response.request.url),
                }.items():
                    if getattr(error, name, None) is None:
                        setattr(error, name, value)
            raise
        finally:
            if io_bridge is None:
                bridge.close()

    return _JsonEvents(request, iterate())
