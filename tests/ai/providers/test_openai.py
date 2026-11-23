"""Chat Completions message conversion, streaming, and authentication failures."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from virgo.ai import resolve_model
from virgo.ai.providers import anthropic, codex, responses
from virgo.ai.providers import openai as oai
from virgo.ai.transform import normalize
from virgo.ai.types import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

from tests.support.http import sse

PNG = ImageContent("YQ==", "image/png")
JPEG = ImageContent("Yg==", "image/jpeg")
PNG_PART = {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}
JPEG_PART = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,Yg=="}}


@pytest.mark.parametrize("follow_up", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_parallel_read_and_bash_attachment(follow_up, interrupted):
    model = Model("deepseek", "deepseek-flash", "openai", options={"sendReasoning": True})
    history = [
        UserMessage("inspect the screenshot"),
        AssistantMessage([ThinkingContent("check both"), ToolCall("read-1", "read", {}), ToolCall("bash-2", "bash", {})],
                         api=model.api, provider=model.provider),
        ToolResultMessage("read-1", "read", [TextContent("Image: bug_screenshot.png"), PNG]),
    ]
    if not interrupted:
        history.append(ToolResultMessage("bash-2", "bash", [TextContent(" done\n")]))
    if follow_up:
        history.append(UserMessage("continue"))
    before = deepcopy(history)

    def request(destination):
        ctx = Context("", normalize(history, destination))
        return oai.build_params(destination, ctx, StreamOptions())["messages"]

    tool_text = "No result provided (the call was interrupted)." if interrupted else " done\n"
    plain = [
        {"role": "user", "content": "inspect the screenshot"},
        {"role": "assistant", "content": None, "reasoning_content": "check both", "tool_calls": [
            {"type": "function", "id": call_id, "function": {"name": name, "arguments": "{}"}}
            for call_id, name in [("read-1", "read"), ("bash-2", "bash")]
        ]},
        {"role": "tool", "tool_call_id": "read-1", "content": "Image: bug_screenshot.png"},
        {"role": "tool", "tool_call_id": "bash-2", "content": tool_text},
    ]
    following = [{"role": "user", "content": "continue"}] if follow_up else []
    stripped = {**plain[2], "content": "Image: bug_screenshot.png\n[image omitted: deepseek-flash does not accept images]"}
    assert request(replace(model, vision=False)) == [*plain[:2], stripped, plain[3], *following]
    assert request(model) == [*plain, {"role": "user", "content": [
        {"type": "text", "text": "Image from tool call read-1."}, PNG_PART,
    ]}, *following]
    assert history == before


def test_grouped_tool_images_with_labels_and_image_only_fallback():
    msgs = [
        AssistantMessage([ToolCall("a", "read", {}), ToolCall("b", "read", {})]),
        ToolResultMessage("a", "read", [PNG, TextContent("tool text"), JPEG]),
        ToolResultMessage("b", "read", [JPEG]),
    ]
    assert oai.convert_messages("", msgs)[1:] == [
        {"role": "tool", "tool_call_id": "a", "content": "tool text"},
        {"role": "tool", "tool_call_id": "b", "content": "Image attached."},
        {"role": "user", "content": [
            {"type": "text", "text": "Images from tool call a."}, PNG_PART, JPEG_PART,
            {"type": "text", "text": "Image from tool call b."}, JPEG_PART,
        ]},
    ]


def test_attachment_flushes_at_skipped_assistant_boundary():
    # The skipped message still separates two runs of results in converter input.
    msgs = [ToolResultMessage("a", "read", [PNG]), AssistantMessage(), ToolResultMessage("b", "read", [JPEG])]
    assert oai.convert_messages("", msgs) == [
        {"role": "tool", "tool_call_id": "a", "content": "Image attached."},
        {"role": "user", "content": [{"type": "text", "text": "Image from tool call a."}, PNG_PART]},
        {"role": "tool", "tool_call_id": "b", "content": "Image attached."},
        {"role": "user", "content": [{"type": "text", "text": "Image from tool call b."}, JPEG_PART]},
    ]


def test_tool_results_without_images_keep_exact_strings():
    assert oai.convert_messages("", [
        ToolResultMessage("a", "read", [TextContent(" a "), TextContent("b\n")]),
        ToolResultMessage("b", "bash", []),
    ]) == [
        {"role": "tool", "tool_call_id": "a", "content": " a \nb\n"},
        {"role": "tool", "tool_call_id": "b", "content": "(no output)"},
    ]


@pytest.mark.parametrize("other", [anthropic, responses, codex])
def test_tool_images_roundtrip_between_chat_and_other_protocols(other):
    api = {anthropic: "anthropic", responses: "openai-responses", codex: "openai-codex"}[other]
    other_model = Model("source", "test", api)
    chat = Model("deepseek", "test", "openai")
    for source in [other_model, chat]:
        call_id = "call_1|fc_1" if source.api.startswith("openai-") else "call_1"
        history = [UserMessage("read"), AssistantMessage([ToolCall(call_id, "read", {})], api=source.api),
                   ToolResultMessage(call_id, "read", [PNG])]
        before = deepcopy(history)
        normalized = normalize(history, chat)
        wire = oai.convert_messages("", normalized)
        paired_id = normalized[1].tool_calls()[0].id
        assert "|" not in paired_id
        assert wire[1]["tool_calls"][0]["id"] == wire[2]["tool_call_id"] == paired_id
        assert wire[3:] == [{"role": "user", "content": [
            {"type": "text", "text": f"Image from tool call {paired_id}."}, PNG_PART,
        ]}]
        assert [m for m in normalized if isinstance(m, UserMessage)] == [before[0]]
        restored = normalize(history, other_model)
        params = other.build_params(other_model, Context("", restored), StreamOptions())
        if other is anthropic:
            call = params["messages"][1]["content"][0]
            result = params["messages"][2]["content"][0]
            assert call["id"] == result["tool_use_id"] == call_id
            assert result["content"] == [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="}}]
        else:
            _, call, result = params["input"]
            assert call["call_id"] == result["call_id"] == "call_1"
            assert result["output"] == [{"type": "input_image", "image_url": "data:image/png;base64,YQ==", "detail": "auto"}]
        assert [m for m in restored if isinstance(m, UserMessage)] == [before[0]]
        assert history == before


def test_openai_chat_stream_closes_thinking_block(server):
    model = resolve_model("openai/gpt-5")
    model.base_url = server["url"]
    model.options["apiKey"] = "test-key"
    server["body"] = sse([
        {"choices": [{"index": 0, "delta": {"reasoning_content": "think"}}]},
        {"choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": "stop"}]},
    ])
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


@pytest.mark.parametrize("status,configured", [(401, False), (401, True), (503, False)])
def test_openai_compatible_missing_credentials_guidance(server, monkeypatch, status, configured):
    model = resolve_model("deepseek-chat")
    monkeypatch.delenv(model.api_key_env, raising=False)
    model.base_url = server["url"]
    if configured:
        model.options["apiKey"] = "fixture-key"
    server["status"] = status
    server["body"] = b'{"error":{"message":"Request rejected"}}'
    result = oai.stream(model, Context("", [UserMessage("hello")]), StreamOptions()).result()
    assert result.stop_reason == "error"
    assert result.error_kind == ("fatal" if status == 401 else "retryable")
    assert result.error_details["context"]["http"]["status_code"] == status
    if status == 401 and not configured:
        assert "No deepseek credentials" in result.error_message
        assert "/login" in result.error_message and "/model" in result.error_message
        assert result.error_details["message"] == result.error_message
    else:
        assert "Request rejected" in result.error_message and "/login" not in result.error_message


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
