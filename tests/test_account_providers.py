"""Exercise the account transports against a local HTTP/SSE server."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from virgo.ai.providers import codex
from virgo.ai.providers._http import ProviderError, _parse_events
from virgo.ai.types import Context, Model, StreamOptions, ToolSpec, UserMessage


@pytest.fixture
def account_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    release = threading.Event()
    started = threading.Event()
    state = {"requests": [], "body": b"", "status": 200, "stall": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            state["requests"].append({
                "path": self.path,
                "headers": dict(self.headers),
                "json": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
            })
            self.send_response(state["status"])
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            started.set()
            if state["stall"]:
                release.wait(3)
            try:
                self.wfile.write(state["body"])
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    state["started"] = started
    yield state
    release.set()
    server.shutdown()
    server.server_close()


def _model(server):
    return Model(
        provider="openai-codex", id="gpt-5-codex", api="openai-codex", base_url=server["url"],
        reasoning=True,
        options={"apiKey": "private-test-token", "authType": "oauth", "authMetadata": {"accountId": "acct-1"}},
    )


def _sse(events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


def test_codex_account_stream_and_request(account_server):
    text = {"type": "message", "id": "msg_1", "content": [{"type": "output_text", "text": "Reading."}]}
    call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read", "arguments": '{"path":"a"}'}
    account_server["body"] = _sse([
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "id": "msg_1"}},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "Reading."},
        {"type": "response.output_item.done", "output_index": 0, "item": text},
        {"type": "response.output_item.added", "output_index": 1, "item": {**call, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"path":"a"}'},
        {"type": "response.output_item.done", "output_index": 1, "item": call},
        {"type": "response.completed", "response": {
            "status": "completed", "output": [text, call],
            "usage": {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 60}},
        }},
    ])
    model = _model(account_server)
    ctx = Context("SYS", [UserMessage("read a")], [ToolSpec("read", "Read", {"type": "object"})])
    events = list(codex.stream(model, ctx, StreamOptions(effort="high")))
    result = events[-1].partial
    assert events[-1].type == "done"
    assert result.stop_reason == "tool_use" and result.text() == "Reading."
    assert result.tool_calls()[0].arguments == {"path": "a"}
    assert (result.usage.input, result.usage.cache_read, result.usage.output) == (40, 60, 20)
    request = account_server["requests"][0]
    assert request["path"] == "/codex/responses"
    assert request["headers"]["Authorization"] == "Bearer private-test-token"
    assert request["headers"]["chatgpt-account-id"] == "acct-1"
    assert request["json"]["instructions"] == "SYS"
    assert request["json"]["reasoning"]["effort"] == "high"
    assert request["json"]["store"] is False and request["json"]["stream"] is True
    assert "max_output_tokens" not in request["json"]
    assert request["json"]["tools"][0]["name"] == "read"


def test_codex_http_failure_redacts_access_token(account_server):
    account_server["status"] = 429
    account_server["body"] = json.dumps({"error": {"message": "Invalid private-test-token"}}).encode()
    result = codex.stream(_model(account_server), Context("", [UserMessage("hi")]), StreamOptions()).result()
    assert result.stop_reason == "error" and result.error_kind == "retryable"
    assert "private-test-token" not in result.error_message


def test_codex_truncated_stream_fails(account_server):
    account_server["body"] = _sse([{"type": "response.output_text.delta", "output_index": 0, "delta": "Partial"}])
    result = codex.stream(_model(account_server), Context("", [UserMessage("hi")]), StreamOptions()).result()
    assert result.stop_reason == "error" and result.error_kind == "retryable"


def test_codex_cancel_does_not_wait_for_stalled_stream(account_server):
    account_server["stall"] = True
    signal = threading.Event()

    def cancel():
        account_server["started"].wait(2)
        signal.set()

    thread = threading.Thread(target=cancel)
    thread.start()
    start = time.monotonic()
    result = codex.stream(_model(account_server), Context("", []), StreamOptions(signal=signal)).result()
    thread.join(1)
    assert result.stop_reason == "aborted"
    assert time.monotonic() - start < 1.5


def test_codex_missing_login_never_sends_request(account_server):
    model = _model(account_server)
    model.options.clear()
    result = codex.stream(model, Context("", []), StreamOptions()).result()
    assert result.stop_reason == "error" and "/login" in result.error_message
    assert account_server["requests"] == []


def test_sse_parser_preserves_multiline_event_and_flushes_at_eof():
    assert list(_parse_events([": keepalive", 'data: {"a":', 'data: 1}'])) == [{"a": 1}]


def test_sse_parser_does_not_echo_bad_response_data():
    with pytest.raises(ProviderError, match="invalid streaming event") as caught:
        list(_parse_events(["data: secret-body", ""]))
    assert "secret-body" not in str(caught.value)
