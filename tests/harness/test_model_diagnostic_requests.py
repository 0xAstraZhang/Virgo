"""Actual provider requests expose scoped evidence without extra recovery turns."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler

import pytest

from virgo.agent.types import AgentTool, ToolResult
from virgo.ai.types import Model, ToolResultMessage, UserMessage

from tests.support.http import bypass_proxies, serve, sse


def wire_reply(api, *, tool=None, arguments=None, call_id="tool-call", text="done"):
    if api == "openai":
        base = {"id": "reply-1", "object": "chat.completion.chunk", "created": 1, "model": "wire-test"}
        delta = {"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {
            "name": tool, "arguments": json.dumps(arguments or {}),
        }}]} if tool else {"content": text}
        return sse([
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]},
        ])
    block = {"type": "tool_use", "id": call_id, "name": tool, "input": {}} if tool else {"type": "text", "text": ""}
    delta = {"type": "input_json_delta", "partial_json": json.dumps(arguments or {})} if tool else {
        "type": "text_delta", "text": text,
    }
    return sse([
        {"type": "message_start", "message": {
            "id": "reply-1", "type": "message", "role": "assistant", "model": "wire-test", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1},
        }},
        {"type": "content_block_start", "index": 0, "content_block": block},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 10}},
        {"type": "message_stop"},
    ], named=True)


@contextmanager
def scripted_api(api, responder):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            request = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append(request)
            body = wire_reply(api, **responder(request, len(requests)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with serve(Handler) as url:
        yield url, requests


def tool_outputs(request, api):
    if api == "openai":
        return [{"id": item["tool_call_id"], "text": item["content"], "is_error": None}
                for item in request["messages"] if item["role"] == "tool"]
    return [{"id": block["tool_use_id"],
             "text": "\n".join(item.get("text", "") for item in block["content"]),
             "is_error": block.get("is_error", False)}
            for message in request["messages"] for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_result"]


def configure(session, api, url, execute):
    tool = AgentTool("probe_backend", "Check the test backend.", {"type": "object", "properties": {}},
                     execute, read_only=True)
    session.register_tool(tool)
    session.main.tools[:] = [tool, session.tool("diagnostic_get")]
    assert all(session.main.tools)
    session.model = session.main.model = Model("wire-test", "wire-test", api, base_url=url,
                                               options={"apiKey": "local-test-key"})
    session.main.rebuild_system()


def fail_with_cause(*_args):
    try:
        raise FileNotFoundError("backend adapter manifest is missing")
    except FileNotFoundError as cause:
        raise RuntimeError("backend operation failed") from cause


@pytest.mark.parametrize("api", ["openai", "anthropic"])
def test_failure_reaches_real_provider_and_model_can_query_exact_id(make_session, monkeypatch, api):
    bypass_proxies(monkeypatch)
    observed_ids = []

    def responder(request, number):
        if number == 1:
            return {"tool": "probe_backend", "call_id": "failure-call"}
        if number == 2:
            text = tool_outputs(request, api)[-1]["text"]
            match = re.search(r'"error_id"\s*:\s*"([A-Za-z0-9_.:-]+)"', text)
            error_id = match[1] if match else "missing-error-reference"
            observed_ids.append(error_id)
            return {"tool": "diagnostic_get", "arguments": {"error_id": error_id, "section": "causes"},
                    "call_id": "lookup-call"}
        return {"text": "Evidence inspected; stopping this task."}

    with scripted_api(api, responder) as (url, requests):
        session = make_session(refresh_models=False, settings={
            "compaction": {"enabled": False}, "retry": {"maxRetries": 0},
        })
        configure(session, api, url, fail_with_cause)
        session.prompt("Inspect the backend error and its causes, then stop.")

        assert len(requests) == 3, session.ui.text
        failure = tool_outputs(requests[1], api)[-1]
        assert failure["id"] == "failure-call"
        assert "backend operation failed" in failure["text"]
        assert "FileNotFoundError" in failure["text"]
        assert "backend adapter manifest is missing" in failure["text"]
        assert "tool.execute" in failure["text"]
        error_id = observed_ids[0]
        assert error_id != "missing-error-reference"
        evidence_output = tool_outputs(requests[2], api)[-1]
        assert evidence_output["id"] == "lookup-call"
        evidence = json.loads(evidence_output["text"])
        assert evidence["status"] == "ok" and evidence["error_id"] == error_id
        assert evidence["section"] == "causes"
        assert "FileNotFoundError" in evidence["text"]
        assert "backend adapter manifest is missing" in evidence["text"]
        assert session.main.diagnostic_access.get(error_id) is not None
        assert session.main.agent.last_text == "Evidence inspected; stopping this task."
        assert not session.wakeups and not session.run_pending()

        # The evidence reached the provider through ordinary result content, not
        # an internal details dictionary that the actual SDK never transmits.
        wire = json.dumps(requests[1:])
        assert '"error_details"' not in wire and '"log_path"' not in wire and '"log_pattern"' not in wire
        assert not any(isinstance(message, UserMessage) and message.kind == "notification"
                       and 'source="diagnostics"' in message.text() for message in session.main.messages)

        session.prompt("A separate follow-up; reply briefly.")
        assert len(requests) == 4
        for earlier, later in zip(requests, requests[1:], strict=False):
            assert earlier.get("system") == later.get("system")
            assert earlier["tools"] == later["tools"]
            assert later["messages"][:len(earlier["messages"])] == earlier["messages"]


@pytest.mark.parametrize("api", ["openai", "anthropic"])
@pytest.mark.parametrize("recover_hook", [False, True])
def test_success_and_hook_recovery_have_no_failure_capsule_or_extra_request(
    make_session, monkeypatch, api, recover_hook,
):
    bypass_proxies(monkeypatch)

    def responder(_request, number):
        return {"tool": "probe_backend", "call_id": "probe-call"} if number == 1 else {"text": "Complete."}

    with scripted_api(api, responder) as (url, requests):
        session = make_session(refresh_models=False, settings={
            "compaction": {"enabled": False}, "retry": {"maxRetries": 0},
        })
        execute = fail_with_cause if recover_hook else lambda *_: ToolResult.text("backend is healthy")
        configure(session, api, url, execute)
        if recover_hook:
            session.bus.on("tool_result", lambda *_: ToolResult.text("backend recovered by result hook"))
        session.prompt("Check the backend once.")

        assert len(requests) == 2, session.ui.text
        output = tool_outputs(requests[1], api)[-1]
        expected = "backend recovered by result hook" if recover_hook else "backend is healthy"
        assert output["text"] == expected
        if api == "anthropic":
            assert output["is_error"] is False
        assert "Diagnostic evidence" not in json.dumps(requests[1])
        assert "Runtime failure observations" not in json.dumps(requests[1])
        assert not session.wakeups and not session.run_pending()
        assert len(requests) == 2
        result = next(message for message in session.main.messages if isinstance(message, ToolResultMessage))
        assert not result.is_error
        if recover_hook:
            related = result.details["related_errors"][0]
            assert session.main.diagnostic_access.get(related["error_id"]) is not None
