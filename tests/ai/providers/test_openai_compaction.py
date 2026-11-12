"""Native OpenAI compaction preserves opaque state across provider requests."""

from __future__ import annotations

import copy
import json
import threading
import time

import pytest

from virgo.ai.providers import codex, responses
from virgo.ai.types import (
    AssistantMessage,
    Context,
    Model,
    RawContent,
    StreamOptions,
    TextContent,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)

from tests.support.http import sse

COMPACTION = {"type": "compaction", "id": "cmp_1", "encrypted_content": "encrypted-state"}
RETAINED = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Finish it"}]}
OUTPUT = [RETAINED, COMPACTION, {"type": "message", "role": "assistant", "content": [], "phase": None}]


@pytest.fixture
def compact_server(server):
    server["content_type"] = "application/json"
    return server


def _model(server, api="openai-responses"):
    return Model(
        "openai-codex" if api == "openai-codex" else "openai", "test", api,
        base_url=server["url"] + ("" if api == "openai-codex" else "/v1"),
        options={"apiKey": "private-token", "authMetadata": {"accountId": "acct-1"}},
    )


def _context():
    return Context("System", [
        UserMessage("Read a"),
        AssistantMessage([ToolCall("call_1", "read", {"path": "a"})]),
        ToolResultMessage("call_1", "read", [TextContent("file contents")]),
        AssistantMessage([TextContent("Done")]),
        UserMessage("Finish it"),
    ], [ToolSpec("read", "Read", {"type": "object"})])


def _compact_payload(output=None):
    return {
        "id": "resp_compact", "object": "response.compaction", "created_at": 1,
        "output": OUTPUT if output is None else output,
        "usage": {
            "input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050,
            "input_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 100},
            "output_tokens_details": {"reasoning_tokens": 10},
        },
    }


def _sse(output):
    events = [
        {"type": "response.output_item.done", "output_index": i, "item": item}
        for i, item in enumerate(output)
    ]
    events.append({"type": "response.completed", "response": {
        "status": "completed", "output": output,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }})
    return sse(events, done=False)


def test_responses_compact_http_roundtrip_preserves_entire_window(compact_server):
    compact_server["body"] = json.dumps(_compact_payload()).encode()
    model, ctx = _model(compact_server), _context()
    original = copy.deepcopy(ctx)
    result = responses.compact(model, ctx, StreamOptions(effort="high"), focus="the API")
    assert result.stop_reason == "compaction", result.error_message
    assert (result.usage.input, result.usage.cache_read, result.usage.cache_write, result.usage.output) == (300, 600, 100, 50)
    assert responses.convert_messages([result]) == OUTPUT
    assert ctx == original
    req = compact_server["requests"][0]
    assert req["path"] == "/v1/responses/compact"
    assert req["headers"]["authorization"] == "Bearer private-token"
    assert req["json"] == {"model": "test", "input": responses.convert_messages(ctx.messages)}
    next_params = responses.build_params(model, Context("System", [result, UserMessage("Continue")]), StreamOptions())
    assert next_params["input"] == [*OUTPUT, {"role": "user", "content": "Continue"}]


@pytest.mark.parametrize("output", [[], [RETAINED], [{**COMPACTION, "encrypted_content": ""}]])
def test_responses_rejects_missing_compaction_state(compact_server, output):
    compact_server["body"] = json.dumps(_compact_payload(output)).encode()
    result = responses.compact(_model(compact_server), _context(), StreamOptions())
    assert result.stop_reason == "error"
    assert not result.content


@pytest.mark.parametrize("provider", [responses, codex])
def test_compact_cancelled_before_request_does_not_contact_provider(compact_server, provider):
    signal = threading.Event()
    signal.set()
    model = _model(compact_server, "openai-codex" if provider is codex else "openai-responses")
    result = provider.compact(model, _context(), StreamOptions(signal=signal))
    assert result.stop_reason == "aborted"
    assert compact_server["requests"] == []


@pytest.mark.parametrize("provider", [responses, codex])
def test_compact_cancellation_returns_without_waiting_for_body(compact_server, provider):
    compact_server["body"] = json.dumps(_compact_payload()).encode()
    compact_server["stall"] = True
    signal = threading.Event()

    def cancel():
        compact_server["started"].wait(2)
        signal.set()

    cancel_thread = threading.Thread(target=cancel)
    cancel_thread.start()
    model = _model(compact_server, "openai-codex" if provider is codex else "openai-responses")
    start = time.monotonic()
    result = provider.compact(model, _context(), StreamOptions(signal=signal))
    cancel_thread.join(1)
    assert result.stop_reason == "aborted"
    assert time.monotonic() - start < 1.5
    assert not result.content


@pytest.mark.parametrize("provider", [responses, codex])
def test_compact_http_errors_redact_credentials(compact_server, provider):
    compact_server["status"] = 429
    compact_server["body"] = b'{"error":{"message":"Rejected private-token"}}'
    model = _model(compact_server, "openai-codex" if provider is codex else "openai-responses")
    result = provider.compact(model, _context(), StreamOptions())
    assert result.stop_reason == "error" and result.error_kind == "retryable"
    assert "private-token" not in result.error_message
    assert "[redacted]" in result.error_message


def test_responses_stream_preserves_compaction_and_final_fields():
    model = Model("openai", "test", "openai-responses")
    out = AssistantMessage(api=model.api)
    final = {**COMPACTION, "created_by": "server"}
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "compaction", "id": "cmp_1"}},
        {"type": "response.output_item.done", "output_index": 0, "item": COMPACTION},
        {"type": "response.completed", "response": {"status": "completed", "output": [final]}},
    ]
    parsed = list(responses.parse_events(events, model, out, StreamOptions()))
    assert parsed[-1].type == "done"
    assert responses.convert_messages([out]) == [final]
    assert responses.convert_messages([out], api="openai-codex") == []
    # Conversion must not expose the stored state to mutations of request dictionaries.
    converted = responses.convert_messages([out])
    converted[0]["encrypted_content"] = "changed"
    assert isinstance(out.content[0], RawContent)
    assert out.content[0].data == final


def test_codex_compact_uses_trigger_and_preserves_user_inputs(compact_server):
    compact_server["body"] = _sse([COMPACTION])
    compact_server["content_type"] = "text/event-stream"
    model, ctx = _model(compact_server, "openai-codex"), _context()
    before = copy.deepcopy(ctx)
    result = codex.compact(model, ctx, StreamOptions())
    assert result.stop_reason == "compaction", result.error_message
    req = compact_server["requests"][0]
    assert req["path"] == "/codex/responses"
    assert req["headers"]["authorization"] == "Bearer private-token"
    assert req["headers"]["chatgpt-account-id"] == "acct-1"
    assert req["json"]["input"][-1] == {"type": "compaction_trigger"}
    assert req["json"]["instructions"] == "System"
    assert req["json"]["tools"][0]["name"] == "read"
    assert responses.convert_messages([result], api=model.api) == [
        {"role": "user", "content": "Read a"}, {"role": "user", "content": "Finish it"}, COMPACTION,
    ]
    assert ctx == before


@pytest.mark.parametrize("output", [[], [COMPACTION, {**COMPACTION, "id": "cmp_2"}]])
def test_codex_compact_requires_exactly_one_state_item(compact_server, output):
    compact_server["body"] = _sse(output)
    compact_server["content_type"] = "text/event-stream"
    result = codex.compact(_model(compact_server, "openai-codex"), _context(), StreamOptions())
    assert result.stop_reason == "error"


def test_codex_compact_rejects_incomplete_stream(compact_server):
    compact_server["body"] = f'data: {json.dumps({"type": "response.output_item.done", "output_index": 0, "item": COMPACTION})}\n\n'.encode()
    compact_server["content_type"] = "text/event-stream"
    result = codex.compact(_model(compact_server, "openai-codex"), _context(), StreamOptions())
    assert result.stop_reason == "error" and result.error_kind == "retryable"


@pytest.mark.parametrize("api", ["openai-responses", "openai-codex"])
def test_compaction_does_not_enable_native_search(compact_server, api):
    provider = codex if api == "openai-codex" else responses
    compact_server["body"] = _sse([COMPACTION]) if provider is codex else json.dumps(_compact_payload()).encode()
    compact_server["content_type"] = "text/event-stream" if provider is codex else "application/json"
    context = _context()
    context.web_search = {"type": "web_search"}
    out = provider.compact(_model(compact_server, api), context, StreamOptions())
    assert out.stop_reason == "compaction", out.error_message
    assert all(tool.get("type") != "web_search" for tool in compact_server["requests"][0]["json"].get("tools", []))
    assert context.web_search == {"type": "web_search"}
