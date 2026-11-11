"""Small SSE transport for account APIs that do not use the installed SDKs."""

from __future__ import annotations

import json
import queue
import re
import threading
from collections.abc import Iterator

import httpx


class ProviderError(Exception):
    def __init__(self, message: str, kind: str = "fatal", *, status_code=None, body=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.body = body


def cancelled(signal) -> bool:
    return signal is not None and signal.is_set()


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
    return ProviderError(f"Provider HTTP {status}" + (f": {message}" if message else ""), kind,
                         status_code=status, body=body)


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
