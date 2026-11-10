"""Changing login/model keeps old Responses tool calls usable in other protocols."""

from __future__ import annotations

import copy
import re

import pytest

from virgo.ai.providers import anthropic, openai
from virgo.ai.transform import normalize
from virgo.ai.types import AssistantMessage, Model, TextContent, ToolCall, ToolResultMessage, UserMessage


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
