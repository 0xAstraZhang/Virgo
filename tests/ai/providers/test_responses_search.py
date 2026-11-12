"""Hosted search travels through Responses/Codex without entering the local tool loop."""

from __future__ import annotations

import copy
import json

import pytest

from virgo.ai.providers import codex, responses
from virgo.ai.types import (
    AssistantMessage,
    Context,
    Model,
    RawContent,
    StreamOptions,
    ToolSpec,
    UserMessage,
    message_from_dict,
    message_to_dict,
)

from tests.support.http import sse

SEARCH = {
    "type": "web_search_call", "id": "ws_1", "status": "completed",
    "action": {"type": "search", "queries": ["Virgo release"]},
}
CITATION = {
    "type": "url_citation", "url": "https://example.com/release", "title": "Release",
    "start_index": 0, "end_index": 7,
}
MESSAGE = {
    "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
    "content": [{"type": "output_text", "text": "Release", "annotations": [CITATION]}],
}


def _model(provider, url="https://example.test"):
    return Model(
        "openai-codex" if provider is codex else "openai", "gpt-5",
        "openai-codex" if provider is codex else "openai-responses", base_url=url,
        options={"apiKey": "test-token", "authMetadata": {"accountId": "account-1"}},
    )


def _context():
    return Context(
        "System", [UserMessage("Search for the release")],
        [ToolSpec("read", "Read a file", {"type": "object"})],
        web_search={"type": "web_search"},
    )


def _completed(output):
    return {"type": "response.completed", "response": {"status": "completed", "output": output}}


@pytest.mark.parametrize("provider", [responses, codex])
def test_native_search_is_separate_from_client_functions(provider):
    context = _context()
    before = copy.deepcopy(context)
    params = provider.build_params(_model(provider), context, StreamOptions())
    assert [tool["type"] for tool in params["tools"]] == ["function", "web_search"]
    assert params["tools"][1] == {"type": "web_search"}
    params["tools"][1]["type"] = "changed"
    assert context == before


@pytest.mark.parametrize("provider", [responses, codex])
def test_native_search_http_roundtrip_keeps_sources_and_history(server, provider):
    server["body"] = sse([
        {"type": "response.output_item.added", "output_index": 0, "item": {**SEARCH, "status": "in_progress"}},
        {"type": "response.web_search_call.searching", "output_index": 0, "item_id": "ws_1"},
        {"type": "response.output_item.done", "output_index": 0, "item": SEARCH},
        {"type": "response.output_item.added", "output_index": 1, "item": {**MESSAGE, "content": []}},
        {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": "Release"},
        {"type": "response.output_text.annotation.added", "output_index": 1, "content_index": 0,
         "annotation_index": 0, "annotation": CITATION},
        {"type": "response.output_item.done", "output_index": 1, "item": MESSAGE},
        _completed([SEARCH, MESSAGE]),
    ], named=True)
    selected = _model(provider, server["url"])
    out = provider.stream(selected, _context(), StreamOptions()).result()
    assert out.stop_reason == "stop", out.error_message
    assert out.search_started and not out.tool_calls()
    assert out.content[0] == RawContent(selected.api, SEARCH)
    assert out.content[1].citations == [CITATION]
    assert out.text() == "Release"
    assert server["requests"][0]["json"]["tools"][-1] == {"type": "web_search"}
    replay = responses.convert_messages([message_from_dict(message_to_dict(out))], api=selected.api)
    assert replay[0] == SEARCH
    assert replay[1] == MESSAGE
    assert responses.convert_messages([out], api="another-api") == [{"role": "assistant", "content": "Release"}]


def test_terminal_response_can_supply_search_and_late_citations():
    early = copy.deepcopy(MESSAGE)
    early["content"][0]["annotations"] = []
    out = AssistantMessage(api="openai-responses")
    list(responses.parse_events([
        {"type": "response.output_item.done", "output_index": 0, "item": SEARCH},
        {"type": "response.output_item.done", "output_index": 1, "item": early},
        _completed([SEARCH, MESSAGE]),
    ], _model(responses), out, StreamOptions()))
    assert out.content[1].citations == [CITATION]
    only_final = AssistantMessage(api="openai-responses")
    list(responses.parse_events([_completed([SEARCH, MESSAGE])], _model(responses), only_final, StreamOptions()))
    assert only_final.search_started and only_final.content == out.content


@pytest.mark.parametrize("include_added", [False, True])
def test_search_status_streams_before_answer_and_item_done_supplies_action(include_added):
    wire = []
    if include_added:
        wire.append({"type": "response.output_item.added", "output_index": 0,
                     "item": {"type": "web_search_call", "id": "ws_1", "status": "in_progress"}})
    wire.extend([
        {"type": "response.web_search_call.searching", "output_index": 0, "item_id": "ws_1"},
        {"type": "response.output_item.done", "output_index": 0, "item": SEARCH},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "Release"},
        _completed([SEARCH, MESSAGE]),
    ])
    events = [copy.deepcopy(event) for event in responses.parse_events(
        wire, _model(responses), AssistantMessage(api="openai-responses"), StreamOptions(),
    )]
    native = [event for event in events if event.type.startswith("native_tool_")]
    assert [(event.type, event.index) for event in native] == [
        ("native_tool_start", 0), ("native_tool_delta", 0), ("native_tool_end", 0),
    ]
    assert native[1].partial.content[0].data["status"] == "searching"
    assert native[-1].partial.content[0] == RawContent("openai-responses", SEARCH)
    assert all(not event.partial.text() and not event.partial.tool_calls() for event in native)
    assert events[-1].partial.text() == "Release"


def test_terminal_only_search_emits_native_events_before_its_text():
    events = list(responses.parse_events(
        [_completed([SEARCH, MESSAGE])], _model(responses), AssistantMessage(), StreamOptions(),
    ))
    assert [event.type for event in events] == [
        "start", "native_tool_start", "native_tool_end", "text_start", "text_end", "done",
    ]


def test_multiple_text_parts_adjust_citation_offsets_without_changing_wire_data():
    message = copy.deepcopy(MESSAGE)
    message["content"].insert(0, {"type": "output_text", "text": "See: ", "annotations": []})
    before = copy.deepcopy(message)
    out = AssistantMessage(api="openai-responses")
    list(responses.parse_events([_completed([message])], _model(responses), out, StreamOptions()))
    assert out.text() == "See: Release"
    assert out.content[0].citations == [{**CITATION, "start_index": 5, "end_index": 12}]
    assert responses.convert_messages([out])[0] == message
    assert message == before


def test_streaming_annotations_survive_truncation():
    out = AssistantMessage(api="openai-responses")
    list(responses.parse_events([
        {"type": "response.web_search_call.in_progress", "output_index": 0, "item_id": "ws_1"},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "Release"},
        {"type": "response.output_text.annotation.added", "output_index": 1, "annotation": CITATION},
    ], _model(responses), out, StreamOptions()))
    assert out.search_started and out.error_kind == "retryable"
    assert out.content[0] == RawContent("openai-responses", {
        "type": "web_search_call", "id": "ws_1", "status": "in_progress",
    })
    assert out.content[1].citations == [CITATION]


@pytest.mark.parametrize("provider", [responses, codex])
@pytest.mark.parametrize("status,message,kind", [
    (400, "web_search is not supported with this model", "search_unsupported"),
    (400, "Invalid parameter tools[1].filters in web_search", "fatal"),
    (400, "read is an unsupported tool", "fatal"),
    (401, "web_search is not permitted: bad API key", "fatal"),
    (429, "web_search is unavailable due to rate limit", "retryable"),
    (503, "web_search service unavailable", "retryable"),
])
def test_only_explicit_search_capability_rejection_allows_fallback(server, provider, status, message, kind):
    server["status"] = status
    server["body"] = json.dumps({"error": {"type": "invalid_request_error", "message": message}}).encode()
    out = provider.stream(_model(provider, server["url"]), _context(), StreamOptions()).result()
    assert out.error_kind == kind, out.error_message
    assert not out.search_started


@pytest.mark.parametrize("provider", [responses, codex])
def test_search_rejection_without_native_declaration_is_not_reclassified(server, provider):
    server["status"] = 400
    server["body"] = b'{"error":{"message":"web_search is not supported"}}'
    out = provider.stream(_model(provider, server["url"]), Context("", []), StreamOptions()).result()
    assert out.error_kind == "fatal"


@pytest.mark.parametrize("code,message,expected", [
    ("unsupported_tool", "web_search not supported", "search_unsupported"),
    ("invalid_request_error", "web_search parameter is invalid", "fatal"),
    ("rate_limit_exceeded", "web_search rate limit", "retryable"),
])
def test_sse_search_rejection_is_classified_narrowly(code, message, expected):
    out = AssistantMessage()
    event = {"type": "response.failed", "response": {"status": "failed", "error": {"code": code, "message": message}}}
    list(responses.parse_events([event], _model(responses), out, StreamOptions(), web_search=True))
    assert out.error_kind == expected
