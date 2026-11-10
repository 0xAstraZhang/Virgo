from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from virgo.ai.providers import google
from virgo.ai.types import (
    AssistantMessage,
    Context,
    Model,
    RawContent,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)


def account_model(base_url=None):
    return Model(
        "google-gemini-cli", "gemini-2.5-pro", "google-gemini-cli", base_url=base_url,
        reasoning=True, options={"apiKey": "secret-token", "authMetadata": {"projectId": "test-project"}},
    )


@pytest.fixture
def google_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    state = {"events": [], "requests": [], "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            state["requests"].append({"path": self.path, "headers": dict(self.headers), "body": body})
            self.send_response(state["status"])
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in state["events"]:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()
    worker.join()


def chunk(parts=None, finish=None, usage=None):
    candidate = {"content": {"parts": parts or []}}
    if finish:
        candidate["finishReason"] = finish
    response = {"candidates": [candidate]}
    if usage:
        response["usageMetadata"] = usage
    return {"response": response}


def test_google_account_http_stream_tools_usage_and_signed_replay(google_server):
    thought = {"text": "I will inspect it.", "thought": True, "thoughtSignature": "c2ln"}
    call = {"functionCall": {"id": "server-call", "name": "read", "args": {"path": "README.md"}}, "thoughtSignature": "dG9vbA=="}
    google_server["events"] = [
        chunk([thought]),
        chunk([{"text": "Reading "}]),
        chunk([{"text": "now."}, call], "STOP", {
            "promptTokenCount": 20, "cachedContentTokenCount": 5,
            "candidatesTokenCount": 3, "thoughtsTokenCount": 4,
        }),
    ]
    model = account_model(google_server["url"])
    ctx = Context("SYS", [UserMessage("read it")], [ToolSpec("read", "Read file", {"type": "object"})])
    events = list(google.stream(model, ctx, StreamOptions(effort="medium")))
    msg = events[-1].partial
    assert events[-1].type == "done" and msg.stop_reason == "tool_use"
    assert msg.text() == "Reading now."
    assert isinstance(msg.content[0], ThinkingContent) and msg.content[0].signature == "c2ln"
    assert msg.tool_calls()[0].arguments == {"path": "README.md"}
    assert (msg.usage.input, msg.usage.output, msg.usage.cache_read) == (15, 7, 5)
    request = google_server["requests"][0]
    assert request["path"] == "/v1internal:streamGenerateContent?alt=sse"
    assert request["headers"]["Authorization"] == "Bearer secret-token"
    assert request["body"]["project"] == "test-project"
    body = request["body"]["request"]
    assert body["systemInstruction"] == {"parts": [{"text": "SYS"}]}
    assert body["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 8192
    assert body["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"] == {"type": "object"}
    result = ToolResultMessage(msg.tool_calls()[0].id, "read", [TextContent("file content")])
    replay = google.convert_messages(model, [msg, result])
    assert replay[0]["parts"] == [thought, {"text": "Reading "}, {"text": "now."}, call]
    assert replay[1]["parts"][0]["functionResponse"]["id"] == "server-call"


def test_google_parallel_results_share_one_turn_and_cross_model_signatures_drop():
    model = account_model()
    previous = AssistantMessage(
        [TextContent("Older text"), RawContent("google-gemini-cli", {"parts": [{"text": "signed", "thoughtSignature": "sig"}]})],
        api="google-gemini-cli", provider=model.provider, model="another-model",
    )
    results = [ToolResultMessage(str(i), "read", [TextContent(str(i))]) for i in range(2)]
    converted = google.convert_messages(model, [previous, *results])
    assert converted[0] == {"role": "model", "parts": [{"text": "Older text"}]}
    assert len(converted) == 2 and len(converted[1]["parts"]) == 2


def test_google_gemini3_foreign_tool_history_uses_text():
    model = account_model()
    model.id = "gemini-3-pro-preview"
    previous = AssistantMessage([ToolCall("call1", "read", {"path": "a"})], api="openai", model="gpt-5")
    result = ToolResultMessage("call1", "read", [TextContent("result")])
    converted = google.convert_messages(model, [previous, result])
    assert "text" in converted[0]["parts"][0]
    assert converted[1]["parts"] == [{"text": "Previous result from read: result"}]


@pytest.mark.parametrize("events,expected", [
    ([chunk([{"text": "partial"}])], "error"),
    ([chunk([], "MAX_TOKENS")], "length"),
    ([chunk([], "SAFETY")], "refusal"),
])
def test_google_terminal_states(monkeypatch, events, expected):
    monkeypatch.setattr(google, "json_events", lambda *args: iter(events))
    msg = google.stream(account_model(), Context("", [UserMessage("x")]), StreamOptions()).result()
    assert msg.stop_reason == expected


def test_google_auth_errors_and_cancellation_do_not_send_requests(monkeypatch):
    def unexpected_request(*args):
        raise AssertionError("request must not be sent")

    monkeypatch.setattr(google, "json_events", unexpected_request)
    model = account_model()
    model.options["authMetadata"] = {}
    msg = google.stream(model, Context("", []), StreamOptions()).result()
    assert msg.stop_reason == "error" and "project ID" in msg.error_message
    signal = threading.Event()
    signal.set()
    msg = google.stream(account_model(), Context("", []), StreamOptions(signal=signal)).result()
    assert msg.stop_reason == "aborted"


def test_google_stream_error_redacts_access_token(monkeypatch):
    closed = []

    def failure(*args):
        try:
            yield {"error": {"code": 429, "message": "quota exceeded for secret-token"}}
            raise AssertionError("must stop after error")
        finally:
            closed.append(True)

    monkeypatch.setattr(google, "json_events", failure)
    stream = google.stream(account_model(), Context("", []), StreamOptions())
    msg = stream.result()
    assert msg.error_kind == "retryable" and "secret-token" not in msg.error_message
    assert closed == [True]
