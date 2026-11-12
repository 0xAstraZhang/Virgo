"""Local HTTP servers that stand in for provider APIs: loopback only, no credentials."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")


def bypass_proxies(monkeypatch) -> None:
    """Keep requests to the loopback server away from any proxy configured on the machine."""
    for key in PROXY_VARIABLES:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@contextlib.contextmanager
def serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    """Serve ``handler`` on an ephemeral loopback port and yield its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    # The default 0.5 s poll interval made every shutdown wait about half a second.
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def sse(events: list[dict], *, named: bool = False, done: bool | None = None) -> bytes:
    """Encode server-sent events.

    ``named`` adds an ``event:`` line from each event's ``type`` (Anthropic, Responses). ``done``
    appends the Chat Completions ``[DONE]`` marker; by default only unnamed streams get it.
    """
    out = []
    for event in events:
        if named:
            out.append(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n")
        else:
            out.append(f"data: {json.dumps(event)}\n\n")
    if not named if done is None else done:
        out.append("data: [DONE]\n\n")
    return "".join(out).encode()


@contextlib.contextmanager
def fake_api(**overrides) -> Iterator[dict]:
    """Record each POSTed JSON request and answer it with the reply scripted in the yielded state.

    Tests change these keys between requests:

    ``status``, ``body`` (bytes)
        The reply.
    ``content_type``
        ``None`` sends SSE for status 200 and JSON otherwise.
    ``stall`` / ``stall_at``
        Hold the reply before its headers (``"headers"``) or before its body (``"body"``) until
        teardown; ``started`` is set once the server is holding it.
    ``drop``
        Send the body as one chunk, then cut the connection before the terminating chunk.

    Each recorded request is ``{"path", "headers" (lower-case names), "json"}``.
    """
    started, release = threading.Event(), threading.Event()
    state = {
        "requests": [], "status": 200, "body": b"", "content_type": None,
        "stall": False, "stall_at": "body", "drop": False,
        "started": started, "release": release, **overrides,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", 0))
            state["requests"].append({
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "json": json.loads(self.rfile.read(length)),
            })
            if state["drop"]:
                self.drop_mid_body()
                return
            stall = state["stall"] and state["stall_at"]
            if stall == "headers":
                started.set()
                release.wait(3)
            self.send_response(state["status"])
            content_type = state["content_type"] or (
                "text/event-stream" if state["status"] == 200 else "application/json"
            )
            self.send_header("Content-Type", content_type)
            self.end_headers()
            started.set()
            if stall == "body":
                release.wait(3)
            try:
                self.wfile.write(state["body"])
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client cancelled while the reply was held

        def drop_mid_body(self):
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            body = state["body"]
            self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True

        def log_message(self, *_args):
            pass

    with serve(Handler) as url:
        state["url"] = url
        try:
            yield state
        finally:
            release.set()
