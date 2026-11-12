"""Chat Completions message conversion and streaming, without HTTP."""

from __future__ import annotations

import types

from virgo.ai import resolve_model
from virgo.ai.providers import openai as oai
from virgo.ai.transform import normalize
from virgo.ai.types import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


def test_openai_chat_stream_closes_thinking_block(monkeypatch):
    from virgo import ai
    from virgo.ai.providers import openai as oai
    from virgo.ai.types import Context, StreamOptions

    def chunk(**delta):
        return types.SimpleNamespace(
            usage=None,
            choices=[types.SimpleNamespace(delta=types.SimpleNamespace(tool_calls=None, **delta), finish_reason=None)],
        )

    pieces = [
        chunk(reasoning_content="think", content=None),
        chunk(reasoning_content=None, content="answer"),
    ]
    fake = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: iter(pieces)))
    )
    monkeypatch.setattr(oai, "_client", lambda model: fake)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = ai.resolve_model("openai/gpt-5")
    kinds = [ev.type for ev in oai.stream(model, Context("SYS", [UserMessage("hi")]), StreamOptions())]
    assert kinds.index("thinking_start") < kinds.index("thinking_end") < kinds.index("done")
    assert kinds.count("thinking_end") == 1


def test_chat_completions_skips_assistant_turns_without_content_or_tool_calls():
    # The output budget ran out while the model was still reasoning: nothing in the turn can be
    # replayed, and sending it as `content: null` makes every later request fail with HTTP 400.
    model = resolve_model("deepseek/deepseek-flash")
    msgs = [
        UserMessage("prove it"),
        AssistantMessage([ThinkingContent("let me think")], api="openai", provider="deepseek", stop_reason="length"),
        UserMessage("never mind"),
    ]
    conv = oai.convert_messages("", normalize(msgs, model), send_reasoning=True)
    assert conv == [{"role": "user", "content": "prove it"}, {"role": "user", "content": "never mind"}]


def test_chat_completions_reasoning_field_accompanies_every_tool_call_turn():
    model = resolve_model("deepseek/deepseek-flash")
    msgs = [
        UserMessage("read it"),
        # Thinking from another provider is dropped, but thinking-mode endpoints still require the field.
        AssistantMessage([ThinkingContent("x", signature="s"), ToolCall("c1", "read", {})], api="anthropic"),
        ToolResultMessage("c1", "read", [TextContent("data")]),
        AssistantMessage(
            [ThinkingContent("check"), ToolCall("c2", "read", {})], api="openai", provider="deepseek"
        ),
        ToolResultMessage("c2", "read", [TextContent("more")]),
        AssistantMessage([TextContent("done")], api="openai", provider="deepseek"),
    ]
    normalized = normalize(msgs, model)
    conv = oai.convert_messages("", normalized, send_reasoning=True)
    assert [m.get("reasoning_content") for m in conv] == [None, "", None, "check", None, None]
    assert "reasoning_content" not in conv[5]
    assert all("reasoning_content" not in m for m in oai.convert_messages("", normalized))
