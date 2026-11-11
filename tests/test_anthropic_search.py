"""Hosted search uses the real Anthropic SDK against local HTTP/SSE fixtures."""

from __future__ import annotations

import copy
import json

import pytest

from tests import test_native_anthropic_compaction as compact_test
from tests import test_provider_http as http_test
from virgo.ai.providers import anthropic as anth
from virgo.ai.types import Context, Model, RawContent, StreamOptions, TextContent, ToolSpec, UserMessage

server = http_test.server
native_server = compact_test.native_server

SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
CITATION = {
    "type": "web_search_result_location",
    "url": "https://example.com/release",
    "title": "Release notes",
    "encrypted_index": "encrypted-index",
    "cited_text": "Released today.",
}
SEARCH_RESULT = {
    "type": "web_search_tool_result",
    "tool_use_id": "srvtoolu_1",
    "content": [{
        "type": "web_search_result", "url": CITATION["url"], "title": CITATION["title"],
        "encrypted_content": "encrypted-result", "page_age": "November 11, 2026",
    }],
}


def model(url="https://api.anthropic.com"):
    return Model("anthropic", "claude-sonnet-4-6", "anthropic", base_url=url,
                 options={"apiKey": "test-key"})


def context(*, search=True):
    return Context("Find sources.", [UserMessage("Find today's release.")],
                   [ToolSpec("read", "Read", {"type": "object"})],
                   web_search=copy.deepcopy(SEARCH) if search else None)


def search_events(*, stop="end_turn", result=None):
    return [
        http_test.ANTHROPIC_EVENTS[0],
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"query":"today'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": ' release"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": SEARCH_RESULT if result is None else result},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "text", "text": "Released ", "citations": [CITATION]}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "text_delta", "text": "today."}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "citations_delta", "citation": {**CITATION, "url": "https://example.com/details"}}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
         "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ]


def test_search_request_is_native_and_does_not_mutate_context():
    ctx = context()
    params = anth.build_params(model(), ctx, StreamOptions())
    assert params["tools"][1] == SEARCH
    assert "input_schema" not in params["tools"][1]
    params["tools"][1]["max_uses"] = 1
    assert ctx.web_search == SEARCH
    assert [tool["name"] for tool in anth.build_params(model(), context(search=False), StreamOptions())["tools"]] == ["read"]


@pytest.mark.parametrize("stop", ["end_turn", "pause_turn"])
def test_native_search_roundtrip_preserves_results_citations_and_continuation(server, stop):
    server["body"] = http_test.sse(search_events(stop=stop), named=True)
    events = list(anth.stream(model(server["url"]), context(), StreamOptions()))
    message = events[-1].partial
    assert events[-1].type == "done"
    assert message.stop_reason == ("stop" if stop == "end_turn" else "pause_turn")
    assert message.search_started
    assert message.tool_calls() == []
    assert not any(event.type.startswith("toolcall_") for event in events)
    assert message.content[0].data["input"] == {"query": "today release"}
    assert message.content[1] == RawContent("anthropic", SEARCH_RESULT)
    assert isinstance(message.content[2], TextContent)
    assert message.content[2].citations == [CITATION, {**CITATION, "url": "https://example.com/details"}]
    replay = anth.convert_messages([message])[0]["content"]
    assert replay[0]["input"] == {"query": "today release"}
    assert replay[1] == SEARCH_RESULT
    assert replay[2]["citations"] == message.content[2].citations
    assert replay[2]["text"] == "Released today."
    replay[2]["citations"][0]["title"] = "Changed"
    assert message.content[2].citations[0]["title"] == "Release notes"
    message.api = "another-api"
    assert "citations" not in anth.convert_messages([message])[0]["content"][-1]
    assert server["requests"][0]["json"]["tools"][-1] == SEARCH


def test_compaction_never_enables_hosted_search(native_server):
    result = anth.compact(model(native_server["url"]), context(), StreamOptions())
    assert result.stop_reason == "compaction"
    assert [tool["name"] for tool in native_server["requests"][0]["json"]["tools"]] == ["read"]


def test_hosted_search_events_arrive_before_answer_and_keep_native_blocks(server):
    server["body"] = http_test.sse(search_events(), named=True)
    # Keep the state visible at each yield rather than the mutable final message.
    events = [copy.deepcopy(event) for event in anth.stream(model(server["url"]), context(), StreamOptions())]
    native = [event for event in events if event.type.startswith("native_tool_")]
    assert [(event.type, event.index) for event in native] == [
        ("native_tool_start", 0),
        ("native_tool_delta", 0),
        ("native_tool_delta", 0),
        ("native_tool_end", 0),
        ("native_tool_start", 1),
        ("native_tool_end", 1),
    ]
    assert len(native[0].partial.content) == 1
    assert native[0].partial.search_started
    assert native[1].delta == '{"query":"today'
    assert native[3].partial.content[0].data["input"] == {"query": "today release"}
    assert native[4].partial.content[1] == RawContent("anthropic", SEARCH_RESULT)
    assert all(not event.partial.text() for event in native)
    assert all(not event.partial.tool_calls() for event in native)


@pytest.mark.parametrize(("status", "description", "native", "expected"), [
    (400, "web_search_20250305 is not supported by this model", True, "search_unsupported"),
    (400, "web_search_20250305 is not supported by this model", False, "fatal"),
    (400, "tools.0.input_schema: invalid schema", True, "fatal"),
    (401, "invalid API key", True, "fatal"),
    (429, "web_search rate limit exceeded", True, "retryable"),
])
def test_only_explicit_native_capability_rejection_allows_fallback(server, status, description, native, expected):
    server["status"] = status
    server["body"] = json.dumps({"type": "error", "error": {
        "type": "invalid_request_error", "message": description,
    }}).encode()
    result = anth.stream(model(server["url"]), context(search=native), StreamOptions()).result()
    assert result.stop_reason == "error"
    assert result.error_kind == expected
    assert not result.search_started


def test_native_search_service_failure_is_not_a_capability_rejection(server):
    failed_result = {**SEARCH_RESULT, "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"}}
    server["body"] = http_test.sse(search_events(result=failed_result), named=True)
    result = anth.stream(model(server["url"]), context(), StreamOptions()).result()
    assert result.stop_reason == "stop"
    assert result.error_kind is None
    assert result.content[1].data == failed_result


def test_interrupted_native_search_exposes_started_state(server):
    server["drop"] = True
    server["body"] = http_test.sse(search_events()[:5], named=True)
    result = anth.stream(model(server["url"]), context(), StreamOptions()).result()
    assert result.stop_reason == "error"
    assert result.error_kind == "retryable"
    assert result.search_started


def test_clean_eof_without_message_stop_is_not_a_success(server):
    server["body"] = http_test.sse(search_events()[:5], named=True)
    result = anth.stream(model(server["url"]), context(), StreamOptions()).result()
    assert result.stop_reason == "error"
    assert result.search_started
    assert result.error_kind == "retryable"
