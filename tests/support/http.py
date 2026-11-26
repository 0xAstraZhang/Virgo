"""Local HTTP servers that stand in for provider APIs: loopback only, no credentials."""

from __future__ import annotations

import contextlib
import json
import select
import socket
import socketserver
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")


class LoopbackHTTPServer(ThreadingHTTPServer):
    """Loopback-only HTTPServer that avoids slow reverse-DNS lookups in server_bind()."""

    def server_bind(self) -> None:
        # HTTPServer.server_bind() calls socket.getfqdn(host), which stalls ~35 s per
        # process on GitHub macOS runners when reverse DNS for 127.0.0.1 times out.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def bypass_proxies(monkeypatch) -> None:
    """Keep requests to the loopback server away from any proxy configured on the machine."""
    for key in PROXY_VARIABLES:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@contextlib.contextmanager
def serve(handler: type[BaseHTTPRequestHandler], *, join_handlers: bool = False) -> Iterator[str]:
    """Serve ``handler`` on an ephemeral loopback port and yield its base URL."""
    server = LoopbackHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = not join_handlers
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
def fake_api(*, replies=None, **overrides) -> Iterator[dict]:
    """Record each POSTed JSON request and answer it with the reply scripted in the yielded state.

    Tests change these keys between requests:

    ``status``, ``body`` (bytes)
        The reply.
    ``content_type``
        ``None`` sends SSE for status 200 and JSON otherwise.
    ``response_headers``
        Extra HTTP response headers, such as a provider request ID or Retry-After.
    ``stall`` / ``stall_at``
        Hold before headers (``"headers"``), after headers (``"body"``), or after flushing
        ``prefix`` bytes (``"delta"``). Each POST snapshots its reply so siblings can be scripted
        independently. ``started`` marks entry to the stall, ``peer_closed`` marks client EOF
        or reset there, and ``expired`` marks the stall_timeout safety deadline.
    ``drop``
        Send the body as one chunk, then cut the connection before the terminating chunk.

    Each recorded request is ``{"path", "headers" (lower-case names), "json"}``.
    If supplied, ``replies`` overrides the default state in POST order.
    """
    release = threading.Event()
    scripted = iter(replies) if replies is not None else None
    state = {
        "requests": [], "status": 200, "body": b"", "content_type": None,
        "stall": False, "stall_at": "body", "drop": False,
        "started": threading.Event(), "release": release, "peer_closed": threading.Event(),
        "expired": threading.Event(), "stall_timeout": 4.0, "prefix": b"", **overrides,
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 4

        def do_POST(self):  # noqa: N802
            self.close_connection = True
            length = int(self.headers.get("content-length", 0))
            reply = state.copy()
            if scripted is not None:
                reply.update(next(scripted, {"status": 418, "body": b"Unexpected request"}))
            state["requests"].append({
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "json": json.loads(self.rfile.read(length)),
            })
            try:
                self.respond(reply)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Client went away during a write, outside the observed stall.

        def respond(self, reply):
            stall = not reply["drop"] and reply["stall"] and reply["stall_at"]
            if stall == "headers" and self.hold(reply):
                return
            status = 200 if reply["drop"] else reply["status"]
            self.send_response(status)
            content_type = reply["content_type"] or (
                "text/event-stream" if status == 200 else "application/json"
            )
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            if reply["drop"]:
                self.send_header("Transfer-Encoding", "chunked")
            for name, value in reply.get("response_headers", {}).items():
                self.send_header(name, value)
            self.end_headers()
            if reply["drop"]:
                self.drop_mid_body(reply["body"])
                return
            if stall == "delta":
                self.wfile.write(reply["prefix"])
                self.wfile.flush()
            if stall in ("body", "delta") and self.hold(reply):
                return
            reply["started"].set()
            self.wfile.write(reply["body"])

        def hold(self, reply):
            deadline = time.monotonic() + reply["stall_timeout"]
            reply["started"].set()
            while not (release.is_set() or reply["release"].is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reply["expired"].set()
                    return False
                # Windows select accepts sockets. Only read when readable, while the
                # handler still owns the connection and has sent no further bytes.
                if select.select([self.connection], [], [], min(0.025, remaining))[0]:
                    try:
                        if self.connection.recv(1):
                            continue
                    except (ConnectionResetError, ConnectionAbortedError):
                        pass
                    reply["peer_closed"].set()
                    return True
            return False

        def drop_mid_body(self, body):
            self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)

        def log_message(self, *_args):
            pass

    with serve(Handler, join_handlers=True) as url:
        state["url"] = url
        try:
            yield state
        finally:
            release.set()
