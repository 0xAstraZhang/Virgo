"""History normalization when the model or protocol changes mid-conversation."""

from __future__ import annotations

import copy
import re

import pytest

from virgo.ai import resolve_model
from virgo.ai.providers import anthropic, openai, responses
from virgo.ai.providers import openai as oai
from virgo.ai.transform import normalize
from virgo.ai.types import (
    AssistantMessage,
    Model,
    RawContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from virgo.harness.store import SessionStore


@pytest.mark.parametrize("source", ["openai-codex", "openai-responses"])
@pytest.mark.parametrize("target", ["anthropic", "openai"])
def test_responses_tool_ids_and_results_are_normalized_together(source, target):
    history = [
        UserMessage("Read a file"),
        AssistantMessage(
            api=source,
            provider="account",
            content=[ToolCall("call_1|fc_item_1", "read", {"path": "README.md"})],
            stop_reason="tool_use",
        ),
        ToolResultMessage("call_1|fc_item_1", "read", [TextContent("contents")]),
    ]
    original = copy.deepcopy(history)
    normalized = normalize(history, Model(target, "test", target))
    call_id = normalized[1].tool_calls()[0].id
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", call_id)
    assert normalized[2].tool_call_id == call_id
    assert history == original
    if target == "anthropic":
        request = anthropic.convert_messages(normalized)
        assert request[1]["content"][0]["id"] == request[2]["content"][0]["tool_use_id"] == call_id
    else:
        request = openai.convert_messages("", normalized)
        assert request[1]["tool_calls"][0]["id"] == request[2]["tool_call_id"] == call_id


def test_foreign_tool_ids_do_not_collide_and_missing_results_use_normalized_id():
    ids = ["call|item", "call_item", "call_" + "a" * 100 + "1", "call_" + "a" * 100 + "2"]
    history = [
        AssistantMessage(api="openai-codex", content=[ToolCall(value, "read", {}) for value in ids]),
        ToolResultMessage(ids[0], "read", [TextContent("first")]),
        UserMessage("Continue"),
    ]
    normalized = normalize(history, Model("anthropic", "test", "anthropic"))
    calls = normalized[0].tool_calls()
    assert len({call.id for call in calls}) == len(ids)
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", call.id) for call in calls)
    results = [message for message in normalized if isinstance(message, ToolResultMessage)]
    assert [result.tool_call_id for result in results] == [call.id for call in calls]
    assert not results[0].is_error and all(result.is_error for result in results[1:])
    longer = normalize([*history, UserMessage("Another turn")], Model("anthropic", "test", "anthropic"))
    assert longer[0].tool_calls() == calls


def test_same_responses_protocol_keeps_compound_call_ids():
    history = [
        AssistantMessage(api="openai-codex", provider="openai-codex", content=[ToolCall("call|fc_1", "read", {})]),
        ToolResultMessage("call|fc_1", "read", [TextContent("contents")]),
    ]
    assert normalize(history, Model("openai-codex", "test", "openai-codex")) == history


def test_cross_provider_normalization_drops_foreign_thinking_and_orphans():
    model = resolve_model("openai/gpt-5")
    msgs = [
        UserMessage("hi"),
        AssistantMessage([ThinkingContent("x", signature="s"), ToolCall("c1", "read", {})], api="anthropic"),
        UserMessage("interrupting"),
    ]
    out = normalize(msgs, model)
    assert not any(isinstance(b, ThinkingContent) for b in out[1].content)
    assert isinstance(out[2], ToolResultMessage) and out[2].is_error  # synthesized result for the orphan call
    conv = oai.convert_messages("SYS", out)
    assert [m["role"] for m in conv] == ["system", "user", "assistant", "tool", "user"]
    assert conv[2]["tool_calls"][0]["function"]["name"] == "read"


_INTERRUPTED = "[Previous response interrupted.]"
_DEEPSEEK = Model("deepseek", "deepseek-flash", "openai")
_RESPONSES = Model("openai", "gpt-5", "openai-responses")
_CODEX = Model("openai-codex", "gpt-5", "openai-codex")
_ANTHROPIC = Model("anthropic", "claude", "anthropic")


@pytest.mark.parametrize("source,target,send_reasoning", [
    pytest.param(_DEEPSEEK, _DEEPSEEK, True, id="chat-with-reasoning"),
    pytest.param(_DEEPSEEK, _DEEPSEEK, False, id="chat-without-reasoning"),
    pytest.param(_RESPONSES, _RESPONSES, False, id="responses"),
    pytest.param(_CODEX, _CODEX, False, id="codex"),
    pytest.param(_ANTHROPIC, _ANTHROPIC, False, id="anthropic"),
    pytest.param(_DEEPSEEK, _ANTHROPIC, False, id="deepseek-to-anthropic"),
])
@pytest.mark.parametrize("with_thinking", [False, True], ids=["empty", "thinking-only"])
def test_reloaded_interruption_on_wire(tmp_path, source, target, send_reasoning, with_thinking):
    signature = None
    if source.api in ("openai-responses", "openai-codex"):
        signature = '{"type":"reasoning","id":"rs_1","encrypted_content":"opaque","summary":[]}'
    elif source.api == "anthropic":
        signature = "signed-thinking"
    reply = AssistantMessage(
        [ThinkingContent("...", signature=signature)] if with_thinking else [],
        api=source.api, provider=source.provider, model=source.id,
        stop_reason="aborted", error_message="aborted by user",
    )
    history = [UserMessage("Original prompt"), reply, UserMessage("Follow-up")]
    store = SessionStore.new(tmp_path, tmp_path)
    for message in history:
        store.append_message(message)
    saved = store.path.read_bytes()
    restored, _ = SessionStore.load(store.path).build_context()

    normalized = normalize(restored, target)
    expected = [
        {"role": "user", "content": "Original prompt"},
        {"role": "assistant", "content": _INTERRUPTED},
        {"role": "user", "content": "Follow-up"},
    ]
    if target.api == "anthropic":
        wire = anthropic.convert_messages(normalized)
        expected = [{"role": m["role"], "content": [{"type": "text", "text": m["content"]}]} for m in expected]
    elif target.api == "openai":
        wire = openai.convert_messages("", normalized, send_reasoning=send_reasoning)
    else:
        wire = responses.convert_messages(normalized, api=target.api)
    assert wire == expected
    assert normalized[1] is not restored[1]
    assert normalized[1].content == [TextContent(_INTERRUPTED)]
    assert restored == history
    assert store.path.read_bytes() == saved


@pytest.mark.parametrize("content", [
    [TextContent(" \t\n")],
    [ToolCall("call", "read", {})],
    [ThinkingContent("unfinished"), ToolCall("call", "read", {})],
    [RawContent("openai-responses", {"type": "web_search_call", "status": "in_progress"})],
], ids=["whitespace", "tool-call", "thinking-and-tool-call", "incomplete-raw"])
def test_aborted_turn_without_replayable_output(content):
    reply = AssistantMessage(content, api=_RESPONSES.api, provider=_RESPONSES.provider, stop_reason="aborted")
    history = [reply, ToolResultMessage("call", "read", [TextContent("orphan")])]
    original = copy.deepcopy(history)
    normalized = normalize(history, _RESPONSES)
    assert responses.convert_messages(normalized) == [{"role": "assistant", "content": _INTERRUPTED}]
    assert normalized[0].content == [TextContent(_INTERRUPTED)]
    assert history == original


def test_compaction_retains_empty_aborted_turn_for_projection(tmp_path):
    store = SessionStore.new(tmp_path, tmp_path)
    prompt_id = store.append_message(UserMessage("Original prompt"))
    aborted = AssistantMessage([ThinkingContent("unfinished")], stop_reason="aborted")
    aborted_id = store.append_message(aborted)
    length_id = store.append_message(AssistantMessage([ThinkingContent("budget exhausted")], stop_reason="length"))
    store.append("compaction", summary="Earlier turns", kept_ids=[prompt_id, aborted_id, length_id])
    follow_up_id = store.append_message(UserMessage("Follow-up"))

    restored = SessionStore.load(store.path)
    messages, ids = restored.build_context()
    assert ids[1:] == [prompt_id, aborted_id, follow_up_id]
    retained = messages[2]
    assert retained.stop_reason == "aborted" and retained.content == []
    assert normalize(messages, _DEEPSEEK)[2].content == [TextContent(_INTERRUPTED)]
    assert retained.content == []
    assert restored.build_full_context()[0][1] == aborted


@pytest.mark.parametrize("content", [[], [ThinkingContent("unfinished")], [ToolCall("call", "read", {})]])
def test_error_turn_without_output_is_still_dropped(content):
    failed = AssistantMessage(content, api="openai", provider="deepseek", stop_reason="error")
    assert normalize([failed], _DEEPSEEK) == []


@pytest.mark.parametrize("calls", [[], [ToolCall("call", "read", {})]], ids=["text", "text-and-tool-call"])
def test_aborted_turn_keeps_partial_text(calls):
    reply = AssistantMessage([TextContent(" Partial answer\n"), *calls], stop_reason="aborted")
    original = copy.deepcopy(reply)
    normalized, = normalize([reply], _DEEPSEEK)
    assert normalized.content == [TextContent(" Partial answer\n")]
    assert reply == original


def test_aborted_turn_keeps_replayable_raw_output_after_stripping_tool_call():
    item = {"type": "compaction", "encrypted_content": "opaque"}
    reply = AssistantMessage([RawContent(_RESPONSES.api, item), ToolCall("call", "read", {})],
                             api=_RESPONSES.api, provider=_RESPONSES.provider, stop_reason="aborted")
    assert responses.convert_messages(normalize([reply], _RESPONSES)) == [item]


def test_aborted_native_search_keeps_sources_and_interruption_note():
    search = RawContent(_RESPONSES.api, {
        "type": "web_search_call", "status": "in_progress",
        "action": {"query": "Virgo", "sources": [{"title": "Source", "url": "https://example.org"}]},
    })
    reply = AssistantMessage([search], api=_RESPONSES.api, provider=_RESPONSES.provider,
                             native_started=True, stop_reason="aborted")
    original = copy.deepcopy(reply)
    normalized, = normalize([reply], _RESPONSES)
    assert normalized.content == [
        TextContent("\n\nSources:\n- [Source](<https://example.org>)"),
        TextContent('\n\n[Web search was interrupted after it started; any sources above are partial results. Queries: ["Virgo"]]'),
    ]
    assert reply == original


def test_completed_call_keeps_cancelled_result_and_execution_facts():
    call = AssistantMessage([ToolCall("call", "read", {})], api="openai", provider="deepseek", stop_reason="tool_use")
    result = ToolResultMessage("call", "read", [TextContent("Operation aborted.")], is_error=True, details={
        "cancelled": True,
        "execution": {"started": True, "outcome": "unknown", "side_effects": "unknown", "result_hook": "not_run"},
    })
    history = [call, result, UserMessage("Follow-up")]
    original = copy.deepcopy(history)
    normalized = normalize(history, _DEEPSEEK)
    assert normalized == original
    wire = openai.convert_messages("", normalized)
    assert wire[0]["tool_calls"][0]["id"] == wire[1]["tool_call_id"] == "call"
    assert wire[1]["content"] == "Operation aborted."
