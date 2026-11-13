"""Actual streamed HTTP, with a test-only socket mapping from a public IP to loopback."""

import gzip
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

from virgo.ai.types import AssistantMessage, TextContent, ToolCall
from virgo.tools import web_fetch

from tests.support.harness import install_responses, tool_results
from tests.support.http import serve


@pytest.mark.parametrize("case", ["plain", "gzip", "bomb", "truncated", "cancel", "deadline"])
def test_real_http_stream_boundaries(make_session, monkeypatch, case):
    ready, release = threading.Event(), threading.Event()
    body = gzip.compress(b"Evidence: 42.\n" * (5000 if case == "bomb" else 1)) if case in {"gzip", "bomb", "truncated"} else b"Evidence: 42."
    if case == "truncated":
        body = gzip.compress(b"Evidence: 42.")[:-5]
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("host"), self.headers.get("authorization")))
            ready.set()
            if case in {"cancel", "deadline"}:
                release.wait(2)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            if case in {"gzip", "bomb", "truncated"}:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    original_connect = socket.create_connection

    def connect(address, *args, **kwargs):
        host, port = address
        assert host == "93.184.216.34", "HTTP must connect to the validated address"
        return original_connect(("127.0.0.1", port), *args, **kwargs)

    with serve(Handler) as base:
        port = int(base.rsplit(":", 1)[1])
        monkeypatch.setattr(socket, "create_connection", connect)
        monkeypatch.setattr(web_fetch, "_addresses", lambda *args: ["93.184.216.34"])
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        s = make_session(settings={"compaction": {"enabled": False}, "fetch": {"maxBytes": 1024, "timeout": 0.2 if case == "deadline" else 5}})
        install_responses(monkeypatch,
            AssistantMessage(content=[ToolCall("fetch", "web_fetch", {"url": f"http://public.example:{port}/page"})], stop_reason="tool_use"),
            AssistantMessage(content=[TextContent("Done")]),
        )
        worker = threading.Thread(target=s.prompt, args=("Read the page",))
        worker.start()
        try:
            assert ready.wait(2)
            if case == "cancel":
                before = time.monotonic()
                s.main.agent.signal.set()
                worker.join(0.8)
                assert not worker.is_alive(), "Cancellation must not wait for stalled HTTP headers"
                assert time.monotonic() - before < 1
            else:
                worker.join(2)
                assert not worker.is_alive()
            result = tool_results(s)[-1]
            assert result.is_error is (case not in {"plain", "gzip"}), result.text()
            if case in {"plain", "gzip"}:
                assert "42" in result.text()
            assert seen == [("/page", f"public.example:{port}", None)]
        finally:
            release.set()
            worker.join(3)
