"""Anthropic request building and error classification, without HTTP."""

from __future__ import annotations

import json

from virgo.ai import resolve_model
from virgo.ai.providers import anthropic as anth
from virgo.ai.transform import normalize
from virgo.ai.types import (
    AssistantMessage,
    Context,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)


def test_anthropic_request_shape():
    model = resolve_model("anthropic/claude-opus-5-5")
    msgs = [
        UserMessage("hi"),
        AssistantMessage(
            [
                ThinkingContent("hmm", signature="sig"),
                TextContent("let me look"),
                ToolCall("t1", "read", {"path": "a"}),
            ],
            api="anthropic",
        ),
        ToolResultMessage("t1", "read", [TextContent("content")]),
        UserMessage("<notification>done</notification>", kind="notification"),
    ]
    ctx = Context("SYS", normalize(msgs, model), [ToolSpec("read", "Read", {"type": "object"})])
    p = anth.build_params(model, ctx, StreamOptions(effort="high"))
    assert p["thinking"]["type"] == "adaptive" and p["output_config"] == {"effort": "high"}
    assert p["cache_control"] == {"type": "ephemeral"} and p["fallbacks"] == "default"
    assert set(p["betas"]) == {anth.BETA_FALLBACK, anth.BETA_BINDING}
    assert p["tools"][0]["eager_input_streaming"] is True
    roles = [m["role"] for m in p["messages"]]
    assert roles == ["user", "assistant", "user"]  # tool result + notification merged into one user turn
    assert p["messages"][1]["content"][0] == {"type": "thinking", "thinking": "hmm", "signature": "sig"}
    assert p["messages"][2]["content"][0]["type"] == "tool_result"
    assert "budget_tokens" not in json.dumps(p) and "temperature" not in p


def test_error_classification():
    class E(Exception):
        def __init__(self, status, message):
            self.status_code, self.message = status, message

    assert (
        anth.classify_error(E(400, "prompt is too long: 1200000 tokens > 1000000 maximum"))[0] == "overflow"
    )
    assert anth.classify_error(E(529, "Overloaded"))[0] == "retryable"
    assert anth.classify_error(E(401, "invalid x-api-key"))[0] == "fatal"
