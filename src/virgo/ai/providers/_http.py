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


def _check_signal(signal):
    if cancelled(signal):
        raise _StreamAborted()


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
    request.extensions["trace"] = _request.get().handshake.trace


class AsyncSDKBridge:
    """One lazy I/O thread and SDK client pool shared by a session's requests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ready = Future()
        self._thread = None
        self._closed = False
        self._clients = {}
        self._requests = set()

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
            _check_signal(request.signal)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="virgo-sdk-io", daemon=True)
                self._thread.start()
            loop = self._ready.result()
            loop.call_soon_threadsafe(self._start, request)

    def _start(self, request):
        self._requests.add(request)
        request.task = asyncio.create_task(request.produce())
        request.task.add_done_callback(request.complete)

    def stop_request(self, request):
        with self._lock:
            if not self._closed and not request.finished.is_set():
                self._ready.result().call_soon_threadsafe(request.cancel)
        request.finished.wait()

    async def _drain(self):
        requests = list(self._requests)
        for request in requests:
            request.cancel()
        await asyncio.gather(*(r.task for r in requests), return_exceptions=True)
        async with AsyncExitStack() as clients:
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

    def __init__(self, bridge, signal, get_client, create):
        self.bridge = bridge
        self.signal = signal
        self.get_client = get_client
        self.create = create
        self.items = queue.Queue(self.capacity + 1)
        self.finished = threading.Event()
        self.handshake = _HandshakeGuard()
        self.generators = []
        self.scope = self.task = None
        self.error = None
        self.client = self.response = None

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
                _check_signal(self.signal)
                self.client = self.get_client(self.bridge)
                _check_signal(self.signal)
                stream = await resources.enter_async_context(await self.create(self.client))
                self.response = stream.response
                async for item in stream:
                    while self.items.qsize() >= self.capacity:
                        _check_signal(self.signal)
                        await asyncio.sleep(0.01)
                    self.items.put_nowait(item)
        if self.scope.cancel_called:
            raise _StreamAborted()

    def cancel(self):
        if self.scope is None:
            self.task.cancel()  # scheduled, but not started
        else:
            self.scope.cancel()

    def complete(self, task):
        self.error = _StreamAborted() if task.cancelled() else task.exception()
        self.bridge._requests.discard(self)
        self.items.put_nowait(self.end)
        self.finished.set()

    def __iter__(self):
        self.bridge.start(self)
        try:
            while True:
                _check_signal(self.signal)
                try:
                    item = self.items.get(timeout=0.05)
                except queue.Empty:
                    continue
                _check_signal(self.signal)
                if item is self.end:
                    if self.error is not None:
                        raise self.error
                    return
                yield item
        finally:
            self.bridge.stop_request(self)


def sdk_stream(out, opts, get_client, create, parse, on_error):
    """Run the SDK on its loop and the existing parser on the synchronous caller."""
    def events():
        bridge = opts.io_bridge or AsyncSDKBridge()
        request = _SDKRequest(bridge, opts.signal, get_client, create)
        failure = terminal = None
        try:
            _check_signal(opts.signal)
            with closing(iter(request)) as items, closing(parse(items, request)) as parsed:
                for event in parsed:
                    _check_signal(opts.signal)
                    if event.type in ("done", "error"):
                        terminal = event
                        break
                    yield event
                    _check_signal(opts.signal)
        except Exception as exc:
            failure = exc
        finally:
            if opts.io_bridge is None:
                bridge.close()

        if isinstance(failure, _StreamAborted) or cancelled(opts.signal):
            yield aborted_event(out)
        elif failure is not None:
            on_error(failure, request)
            yield StreamEvent("error", out)
        elif terminal is not None:
            yield terminal

    return EventStream(events(), out)


def run_cancellable(call, signal=None):
    """Wait for a bounded synchronous request without holding up cancellation.

    The caller owns the request timeout. An already dispatched request can still
    finish on the server, but its result is never applied after cancellation.
    """
    if cancelled(signal):
        raise ProviderError("Aborted by user.", "aborted")
    if signal is None:
        return call()
    result: queue.Queue = queue.Queue(maxsize=1)

    def run():
        try:
            result.put((True, call()))
        except Exception as error:
            result.put((False, error))

    threading.Thread(target=run, name="virgo-provider-request", daemon=True).start()
    while True:
        if cancelled(signal):
            raise ProviderError("Aborted by user.", "aborted")
        try:
            ok, value = result.get(timeout=0.05)
        except queue.Empty:
            continue
        if cancelled(signal):
            raise ProviderError("Aborted by user.", "aborted")
        if not ok:
            raise value
        return value


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


def json_events(url: str, headers: dict, body: dict, signal=None) -> Iterator[dict]:
    """Read SSE on a worker so a stalled server cannot hold up user cancellation.

    The queue bounds read-ahead. Closing the iterator stops the worker and closes
    its socket; connection attempts also have a finite timeout.
    """
    if cancelled(signal):
        raise ProviderError("Aborted by user.", "aborted")
    events: queue.Queue = queue.Queue(maxsize=64)
    stopped = threading.Event()
    response_ready: list[httpx.Response] = []

    def publish(kind, payload=None):
        while not stopped.is_set():
            try:
                events.put((kind, payload), timeout=0.05)
                return
            except queue.Full:
                continue

    def read():
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
                with client.stream("POST", url, headers=headers, json=body) as response:
                    response_ready.append(response)
                    if stopped.is_set():
                        return
                    if response.status_code >= 400:
                        response.read()
                        raise _response_error(response)
                    for event in _parse_events(response.iter_lines()):
                        if stopped.is_set():
                            return
                        publish("event", event)
        except Exception as error:  # provider contract: failures become error events
            if response_ready:
                response = response_ready[-1]
                # A malformed event or dropped body still belongs to this HTTP
                # request. Preserve its safe correlation fields across the worker.
                for name, value in {
                    "status_code": response.status_code,
                    "request_id": response.headers.get("x-request-id") or response.headers.get("request-id")
                    or response.headers.get("x-goog-request-id"),
                    "retry_after": response.headers.get("retry-after"),
                    "endpoint": str(response.request.url),
                }.items():
                    if getattr(error, name, None) is None:
                        setattr(error, name, value)
            publish("error", error)
        finally:
            publish("done")

    worker = threading.Thread(target=read, name="virgo-provider-sse", daemon=True)
    worker.start()
    try:
        while True:
            if cancelled(signal):
                raise ProviderError("Aborted by user.", "aborted")
            try:
                kind, payload = events.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == "done":
                return
            if kind == "error":
                raise payload
            yield payload
    finally:
        stopped.set()
        if response_ready and not response_ready[0].is_closed:
            # Closing a connection must not hold up the UI, even for a broken
            # transport. The network worker also owns a context-manager close.
            def close():
                try:
                    response_ready[0].close()
                except Exception:
                    pass

            threading.Thread(target=close, name="virgo-provider-close", daemon=True).start()
