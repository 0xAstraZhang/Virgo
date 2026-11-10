"""Native compaction requests and signed-block replay through the real SDK."""

from __future__ import annotations

import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from virgo.ai.providers import anthropic as anth
from virgo.ai.types import (
    AssistantMessage,
    Context,
    Model,
    RawContent,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolSpec,
    UserMessage,
)

SIGNED_BLOCK = {
    "type": "compaction",
    "content": "The user is implementing the API. Preserve the error paths.",
    "signature": "signed-native-summary",
}


def reply(*, stop_reason="compaction", content=None, usage=None):
    return {
        "id": "msg_compacted",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [SIGNED_BLOCK] if content is None else content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {
            "input_tokens": 0,
            "output_tokens": 0,
            "iterations": [{
                "type": "compaction",
                "input_tokens": 1200,
                "output_tokens": 180,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 20,
            }],
        },
    }


@pytest.fixture
def native_server(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    state = {"requests": [], "body": json.dumps(reply()).encode(), "status": 200, "stall": False}
    started, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            state["requests"].append({
                "path": self.path,
                "headers": dict(self.headers),
                "json": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
            })
            started.set()
            if state["stall"]:
                release.wait(3)
            self.send_response(state["status"])
            self.send_header("Content-Type", state.get("content_type", "application/json"))
            self.end_headers()
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
    anth._clients.clear()
    yield state
    release.set()
    server.shutdown()
    server.server_close()
    anth._clients.clear()


def model(server):
    return Model(
        "anthropic", "claude-opus-5-5", "anthropic", base_url=server["url"],
        reasoning=True, options={"apiKey": "private-test-key"},
    )


def test_native_compaction_preserves_request_context_and_signed_block(native_server):
    current = model(native_server)
    messages = [UserMessage("Implement the API."), AssistantMessage([TextContent("Working on it.")])]
    original = copy.deepcopy(messages)
    tools = [ToolSpec("read", "Read a file", {"type": "object"})]
    ctx = Context("Keep the original system instructions.", messages, tools)
    opts = StreamOptions(max_tokens=8000, effort="high")
    result = anth.compact(current, ctx, opts)

    assert result.stop_reason == "compaction"
    assert result.content == [RawContent("anthropic", SIGNED_BLOCK)]
    assert result.text() == ""
    assert messages == original
    assert (result.usage.input, result.usage.output, result.usage.cache_read, result.usage.cache_write) == (
        1200, 180, 900, 20,
    )
    request = native_server["requests"][0]
    body = request["json"]
    regular = anth.build_params(current, ctx, opts)
    assert request["path"].split("?")[0] == "/v1/messages"
    assert body["compaction"] == {"type": "summarize"}
    assert body["stream"] is False and body["max_tokens"] == 8000
    for field in ("system", "tools", "thinking", "output_config", "messages"):
        assert body[field] == regular[field]
    assert anth.BETA_COMPACTION in request["headers"]["anthropic-beta"]

    recent = AssistantMessage([ThinkingContent("Recent reasoning", "thinking-signature"), TextContent("Done.")])
    replay = anth.build_params(current, Context(ctx.system, [result, recent, UserMessage("Continue")], tools), opts)
    assert replay["messages"][0]["content"][0] == SIGNED_BLOCK
    assert replay["messages"][0]["content"][1]["signature"] == "thinking-signature"
    assert replay["betas"].count(anth.BETA_COMPACTION) == 1
    assert replay["system"] == regular["system"] and replay["tools"] == regular["tools"]


def test_focus_uses_complete_summarization_instructions(native_server):
    result = anth.compact(model(native_server), Context("", [UserMessage("Work")]), StreamOptions(), focus="API errors")
    assert result.stop_reason == "compaction"
    instructions = native_server["requests"][0]["json"]["compaction"]["instructions"]
    assert "API errors" in instructions
    assert "latest request" in instructions and "call tools" in instructions


@pytest.mark.parametrize(("response", "expected"), [
    (reply(stop_reason="refusal", content=[]), "refusal"),
    (reply(stop_reason="max_tokens", content=[]), "length"),
    (reply(content=[]), "error"),
    (reply(content=[{"type": "compaction", "content": "Summary"}]), "error"),
    (reply(content=[{**SIGNED_BLOCK, "content": None}]), "error"),
    (reply(content=[{**SIGNED_BLOCK, "content": " "}]), "error"),
    (reply(content=[SIGNED_BLOCK, {"type": "text", "text": "Extra"}]), "error"),
    (reply(stop_reason="end_turn", content=[{"type": "text", "text": "A regular reply"}]), "error"),
])
def test_missing_or_incomplete_summary_is_not_accepted(native_server, response, expected):
    native_server["body"] = json.dumps(response).encode()
    result = anth.compact(model(native_server), Context("", [UserMessage("Work")]), StreamOptions())
    assert len(native_server["requests"]) == 1
    assert result.stop_reason == expected
    assert result.content == [] and result.error_message


def test_compaction_http_error_is_classified_and_redacted(native_server):
    native_server["status"] = 400
    native_server["body"] = json.dumps({
        "type": "error", "error": {
            "type": "invalid_request_error", "message": "prompt is too long: private-test-key",
        },
    }).encode()
    result = anth.compact(model(native_server), Context("", [UserMessage("Work")]), StreamOptions())
    assert result.stop_reason == "error" and result.error_kind == "overflow"
    assert "private-test-key" not in result.error_message and "[redacted]" in result.error_message


def test_pre_cancelled_compaction_sends_no_request(native_server):
    signal = threading.Event()
    signal.set()
    result = anth.compact(model(native_server), Context("", []), StreamOptions(signal=signal))
    assert result.stop_reason == "aborted" and result.content == []
    assert native_server["requests"] == []


def test_cancelled_compaction_does_not_wait_for_stalled_response(native_server):
    native_server["stall"] = True
    signal = threading.Event()

    def cancel():
        native_server["started"].wait(2)
        signal.set()

    thread = threading.Thread(target=cancel)
    thread.start()
    start = time.monotonic()
    result = anth.compact(model(native_server), Context("", [UserMessage("Work")]), StreamOptions(signal=signal))
    thread.join(1)
    assert result.stop_reason == "aborted" and result.content == []
    assert time.monotonic() - start < 1.5


def test_streaming_compaction_keeps_native_stop_reason_and_entire_block(native_server):
    events = [
        {"type": "message_start", "message": reply(stop_reason=None, content=[])},
        {"type": "content_block_start", "index": 0, "content_block": SIGNED_BLOCK},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "compaction", "stop_sequence": None}, "usage": {"output_tokens": 180}},
        {"type": "message_stop"},
    ]
    native_server["body"] = "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()
    native_server["content_type"] = "text/event-stream"
    result = anth.stream(model(native_server), Context("", [UserMessage("Work")]), StreamOptions()).result()
    assert result.stop_reason == "compaction"
    assert result.content == [RawContent("anthropic", SIGNED_BLOCK)]


def test_foreign_raw_blocks_do_not_enable_or_replay_anthropic_compaction():
    foreign = AssistantMessage([RawContent("openai-responses", {"type": "compaction", "encrypted_content": "opaque"})])
    params = anth.build_params(Model("anthropic", "test", "anthropic"), Context("", [foreign]), StreamOptions())
    assert params["messages"] == []
    assert anth.BETA_COMPACTION not in params.get("betas", [])
