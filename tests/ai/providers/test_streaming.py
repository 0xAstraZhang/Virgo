"""Anthropic and Chat Completions streams through the real SDKs against a local server."""

from __future__ import annotations

import json

import pytest

from virgo import ai
from virgo.ai.providers import anthropic as anth
from virgo.ai.types import (
    Context,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolSpec,
    UserMessage,
)
from virgo.ai.validate import INVALID_ARGUMENTS

from tests.support.anthropic import ANTHROPIC_EVENTS
from tests.support.http import sse


def test_anthropic_streaming_roundtrip(server, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    server["body"] = sse(ANTHROPIC_EVENTS, named=True)
    model = ai.resolve_model("anthropic/claude-opus-5-5")
    model.base_url = server["url"]
    ctx = Context(
        "SYS",
        [UserMessage("read the readme")],
        [ToolSpec("read", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})],
    )
    events = list(ai.stream(model, ctx, StreamOptions(effort="medium")))
    msg = events[-1].partial
    assert events[-1].type == "done" and msg.stop_reason == "tool_use"
    assert isinstance(msg.content[0], ThinkingContent) and msg.content[0].signature == "sig-abc"
    assert isinstance(msg.content[1], TextContent) and msg.content[1].text == "Reading it."
    assert isinstance(msg.content[2], ToolCall) and msg.content[2].arguments == {"path": "README.md"}
    assert msg.usage.input == 12 and msg.usage.cache_read == 100 and msg.usage.output == 40
    req = server["requests"][0]
    body = req["json"]
    assert req["path"].startswith("/v1/messages")
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True
    assert body["thinking"]["type"] == "adaptive" and body["output_config"] == {"effort": "medium"}
    assert body["cache_control"] == {"type": "ephemeral"} and body["fallbacks"] == "default"
    assert body["tools"][0]["eager_input_streaming"] is True
    assert anth.BETA_BINDING in req["headers"].get("anthropic-beta", "")


def test_anthropic_http_errors_are_classified(server, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    server["status"] = 400
    server["body"] = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "prompt is too long: 1200000 tokens > 1000000 maximum",
            },
        }
    ).encode()
    model = ai.resolve_model("anthropic/claude-opus-5-5")
    model.base_url = server["url"]
    msg = ai.stream(model, Context("", [UserMessage("x")]), StreamOptions()).result()
    assert msg.stop_reason == "error" and msg.error_kind == "overflow"


OPENAI_CHUNK = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "gpt-5"}


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_stream_dropped_mid_response_is_retryable(server, monkeypatch, provider):
    # The Anthropic SDK does not wrap transport errors raised while a response is streamed, so the
    # drop was classified as fatal and never retried. (The OpenAI SDK wraps them itself.)
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "test-key")
    server["drop"] = True
    if provider == "anthropic":
        server["body"] = sse(ANTHROPIC_EVENTS[:7], named=True)
        model = ai.resolve_model("anthropic/claude-opus-5-5")
    else:
        delta = {"index": 0, "delta": {"role": "assistant", "content": "Hel"}, "finish_reason": None}
        server["body"] = f"data: {json.dumps({**OPENAI_CHUNK, 'choices': [delta]})}\n\n".encode()
        model = ai.resolve_model("openai/gpt-5")
    model.base_url = server["url"]
    msg = ai.stream(model, Context("", [UserMessage("x")]), StreamOptions()).result()
    assert msg.stop_reason == "error" and msg.error_kind == "retryable" and msg.error_message
    assert msg.text()  # the partial output arrived before the drop


@pytest.mark.parametrize("arguments", ["null", '"src"', "[1, 2]"])
@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_non_object_tool_arguments_are_marked_invalid(server, monkeypatch, provider, arguments):
    # They used to reach the agent loop as None / str / list and crash the run in dict(arguments).
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "test-key")
    if provider == "anthropic":
        events = [
            ANTHROPIC_EVENTS[0],
            {**ANTHROPIC_EVENTS[8], "index": 0},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": arguments}},
            {"type": "content_block_stop", "index": 0},
            *ANTHROPIC_EVENTS[-2:],
        ]
        server["body"] = sse(events, named=True)
        model = ai.resolve_model("anthropic/claude-opus-5-5")
    else:
        call = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "read", "arguments": arguments}}
        chunks = [
            {**OPENAI_CHUNK, "choices": [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}]},
            {**OPENAI_CHUNK, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        server["body"] = sse(chunks, named=False)
        model = ai.resolve_model("openai/gpt-5")
    model.base_url = server["url"]
    ctx = Context("", [UserMessage("hi")], [ToolSpec("read", "Read", {"type": "object"})])
    msg = ai.stream(model, ctx, StreamOptions()).result()
    assert msg.stop_reason == "tool_use"
    assert msg.tool_calls()[0].arguments == {INVALID_ARGUMENTS: arguments}


def test_openai_streaming_roundtrip(server, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "gpt-5"}
    chunks = [
        {
            **base,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "Hel"}, "finish_reason": None}
            ],
        },
        {**base, "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]},
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"pa'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a"}'}}]},
                    "finish_reason": None,
                }
            ],
        },
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {**base, "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}},
    ]
    server["body"] = sse(chunks, named=False)
    model = ai.resolve_model("openai/gpt-5")
    model.base_url = server["url"]
    ctx = Context("SYS", [UserMessage("hi")], [ToolSpec("read", "Read", {"type": "object"})])
    msg = ai.stream(model, ctx, StreamOptions(effort="xhigh")).result()
    assert msg.stop_reason == "tool_use" and msg.text() == "Hello"
    assert msg.tool_calls()[0].arguments == {"path": "a"} and msg.usage.output == 4
    body = server["requests"][0]["json"]
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["reasoning_effort"] == "high" and "max_completion_tokens" in body


@pytest.mark.parametrize("cache_fields,expected_cached", [
    ({"prompt_tokens_details": {"cached_tokens": 60}}, 60),
    ({"prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 40}, 60),
    ({"prompt_tokens_details": {"cached_tokens": None}, "prompt_cache_hit_tokens": 60}, 60),
    ({"prompt_tokens_details": {"cached_tokens": 60}, "prompt_cache_hit_tokens": 60}, 60),
    ({"prompt_tokens_details": {"cached_tokens": 0}, "prompt_cache_hit_tokens": 60}, 0),
    ({}, 0),
])
def test_openai_compatible_cache_usage(server, monkeypatch, cache_fields, expected_cached):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    chunks = [
        {**OPENAI_CHUNK, "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]},
        {**OPENAI_CHUNK, "choices": [], "usage": {
            "prompt_tokens": 100, "completion_tokens": 4, "total_tokens": 104, **cache_fields,
        }},
    ]
    server["body"] = sse(chunks, named=False)
    model = ai.resolve_model("deepseek/deepseek-flash")
    model.base_url = server["url"]
    msg = ai.stream(model, Context("System instructions", [UserMessage("hi")]), StreamOptions()).result()
    assert msg.stop_reason == "stop" and msg.text() == "ok"
    assert msg.usage.input == 100 - expected_cached
    assert msg.usage.cache_read == expected_cached
    assert msg.usage.total == 104
