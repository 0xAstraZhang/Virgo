"""Responses account requests exercise the SDK against a local HTTP server."""

from __future__ import annotations

import copy
import json
import threading

import pytest

from virgo.ai.providers import responses
from virgo.ai.types import (
    AssistantMessage,
    Context,
    Model,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)

from tests.support.http import sse


def model(url="https://api.x.ai/v1"):
    return Model(
        provider="xai",
        id="grok-4.3",
        api="openai-responses",
        base_url=url,
        reasoning=True,
        options={"apiKey": "account-token", "authType": "oauth", "headers": {"x-custom": "test"}},
    )


REASONING = {
    "type": "reasoning",
    "id": "rs_1",
    "summary": [{"type": "summary_text", "text": "Thinking"}],
    "encrypted_content": "encrypted-reasoning",
}
MESSAGE = {
    "type": "message",
    "id": "msg_1",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "Reading.", "annotations": []}],
}
CALL = {
    "type": "function_call",
    "id": "fc_1",
    "call_id": "call_1",
    "name": "read",
    "arguments": '{"path":"README.md"}',
    "status": "completed",
}
EVENTS = [
    {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
    {"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "Thinking"},
    {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {k: v for k, v in REASONING.items() if k != "encrypted_content"},
    },
    {"type": "response.output_item.added", "output_index": 1, "item": {**MESSAGE, "content": []}},
    {"type": "response.output_text.delta", "output_index": 1, "delta": "Reading."},
    {"type": "response.output_item.done", "output_index": 1, "item": MESSAGE},
    {"type": "response.output_item.added", "output_index": 2, "item": {**CALL, "arguments": ""}},
    {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"path":'},
    {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '"README.md"}'},
    {"type": "response.output_item.done", "output_index": 2, "item": CALL},
    {
        "type": "response.completed",
        "response": {
            "id": "resp_1",
            "status": "completed",
            "output": [REASONING, MESSAGE, CALL],
            "usage": {"input_tokens": 20, "output_tokens": 8, "input_tokens_details": {"cached_tokens": 5}},
        },
    },
]


def test_responses_account_http_roundtrip(server):
    server["body"] = sse(EVENTS, named=True)
    context = Context("System", [UserMessage("Read README")], [ToolSpec("read", "Read", {"type": "object"})])
    result = responses.stream(model(server["url"]), context, StreamOptions(effort="high")).result()
    assert result.stop_reason == "tool_use"
    assert result.text() == "Reading."
    assert result.tool_calls()[0].id == "call_1|fc_1"
    assert result.tool_calls()[0].arguments == {"path": "README.md"}
    assert json.loads(result.content[0].signature)["encrypted_content"] == "encrypted-reasoning"
    assert (result.usage.input, result.usage.output, result.usage.cache_read) == (15, 8, 5)
    request = server["requests"][0]
    assert request["path"] == "/responses"
    headers = request["headers"]
    assert headers["authorization"] == "Bearer account-token"
    assert headers["x-custom"] == "test"
    assert request["json"]["store"] is False
    assert request["json"]["instructions"] == "System"
    assert request["json"]["tools"][0]["name"] == "read"
    assert request["json"]["reasoning"]["effort"] == "high"


@pytest.mark.parametrize("effort", ["xhigh", "max"])
@pytest.mark.parametrize("declared_by", ["supported", "default", "unknown"])
def test_responses_request_preserves_declared_effort_without_low(effort, declared_by):
    selected = model()
    if declared_by == "supported":
        selected.supported_reasoning_levels = ("high", effort)
    elif declared_by == "default":
        selected.default_reasoning_level = effort
    params = responses.build_params(selected, Context("", [UserMessage("hi")]), StreamOptions(effort=effort))
    expected = "xhigh" if declared_by == "unknown" and effort == "max" else effort
    assert params["reasoning"]["effort"] == expected


def test_responses_preserves_reasoning_tool_history_without_mutating():
    msg = AssistantMessage(
        api="openai-responses",
        content=[
            ThinkingContent("Thinking", signature=json.dumps(REASONING)),
            TextContent("Reading."),
            ToolCall("call_1|fc_1", "read", {"path": "README.md"}),
        ],
    )
    history = [UserMessage("Read"), msg, ToolResultMessage("call_1|fc_1", "read", [TextContent("contents")])]
    before = copy.deepcopy(history)
    converted = responses.convert_messages(history)
    assert converted[1] == REASONING
    assert converted[3]["call_id"] == "call_1" and converted[3]["id"] == "fc_1"
    assert converted[4] == {"type": "function_call_output", "call_id": "call_1", "output": "contents"}
    assert history == before
    foreign = responses.convert_messages(history, api="another")
    assert all(item.get("type") != "reasoning" for item in foreign)
    assert "id" not in foreign[2]


def test_responses_drops_reasoning_left_without_its_output_item():
    reasoning = ThinkingContent("Thinking", signature=json.dumps(REASONING))
    history = [
        UserMessage("Prove it"),
        # The output budget ran out while reasoning: no message or tool call followed.
        AssistantMessage(api="openai-responses", content=[reasoning], stop_reason="length"),
        UserMessage("Never mind"),
        AssistantMessage(api="openai-responses", content=[reasoning, TextContent("OK")]),
        UserMessage("Thanks"),
    ]
    converted = responses.convert_messages(history)
    assert converted == [
        {"role": "user", "content": "Prove it"},
        {"role": "user", "content": "Never mind"},
        REASONING,
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": "Thanks"},
    ]


@pytest.mark.parametrize(
    "event,reason",
    [
        (
            {
                "type": "response.incomplete",
                "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            },
            "length",
        ),
        (
            {
                "type": "response.incomplete",
                "response": {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
            },
            "refusal",
        ),
        (
            {"type": "response.failed", "response": {"status": "failed", "error": {"message": "failed"}}},
            "error",
        ),
        ({"type": "error", "code": "rate_limit_exceeded", "message": "slow down"}, "error"),
    ],
)
def test_responses_terminal_events(event, reason):
    out = AssistantMessage()
    parsed = list(responses.parse_events([event], model(), out, StreamOptions()))
    assert out.stop_reason == reason
    assert parsed[-1].type == ("done" if reason == "length" else "error")


def test_responses_truncated_stream_is_error():
    out = AssistantMessage()
    parsed = list(responses.parse_events(EVENTS[:-1], model(), out, StreamOptions()))
    assert parsed[-1].type == "error"
    assert out.error_kind == "retryable"


def test_responses_cancelled_request_does_not_contact_server(server):
    signal = threading.Event()
    signal.set()
    out = responses.stream(
        model(server["url"]), Context("", [UserMessage("x")]), StreamOptions(signal=signal)
    ).result()
    assert out.stop_reason == "aborted"
    assert not server["requests"]


def test_responses_stream_error_redacts_access_token(server):
    server["body"] = sse(
        [
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {"message": "Rejected account-token"},
                },
            },
        ],
        named=True,
    )
    out = responses.stream(model(server["url"]), Context("", [UserMessage("x")]), StreamOptions()).result()
    assert out.stop_reason == "error"
    assert out.error_message == "Rejected [redacted]"
