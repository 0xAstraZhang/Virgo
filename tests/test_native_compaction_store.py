"""Compaction round trips keep provider state intact without reusing old usage."""

from __future__ import annotations

import copy

import pytest

from virgo.ai.types import (
    AssistantMessage,
    RawContent,
    TextContent,
    ThinkingContent,
    Usage,
    UserMessage,
    message_to_dict,
)
from virgo.harness.compaction import estimate_tokens
from virgo.harness.store import SessionStore


@pytest.mark.parametrize("api", ["anthropic", "openai-responses"])
@pytest.mark.parametrize("preserve_thinking", [False, True])
def test_native_context_survives_disk_roundtrip(tmp_path, api, preserve_thinking):
    store = SessionStore.new(tmp_path / "sessions", tmp_path)
    store.append_message(UserMessage("Original request"))
    kept = AssistantMessage(
        api=api,
        content=[ThinkingContent("retained reasoning", "signed-thinking"), TextContent("Recent answer")],
        usage=Usage(input=180000, output=200, cache_read=10000, cache_write=100),
    )
    kept_id = store.append_message(kept)
    thinking_id = store.append_message(AssistantMessage(
        api=api, content=[ThinkingContent("thinking only", "thinking-only-signature")],
        usage=Usage(input=190000),
    ))
    items = (
        [{"type": "compaction", "content": "Summary", "signature": "signed-summary", "extra": {"v": [1, 2]}}]
        if api == "anthropic"
        else [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Request"}]},
            {"type": "compaction", "id": "cmp-1", "encrypted_content": "encrypted-state", "extra": ["keep"]},
        ]
    )
    native = AssistantMessage(
        api=api,
        provider="anthropic" if api == "anthropic" else "openai",
        content=[RawContent(api, item) for item in items],
        stop_reason="compaction",
        usage=Usage(input=190000, output=1000),
    )
    compaction = store.append(
        "compaction", summary="Display summary", context=[message_to_dict(native)],
        kept_ids=[kept_id, thinking_id], preserve_thinking=preserve_thinking,
    )
    later = AssistantMessage(api=api, content=[TextContent("Continued")], usage=Usage(input=350, output=25))
    later_id = store.append_message(later)
    saved_bytes = store.path.read_bytes()
    loaded = SessionStore.load(store.path)
    before = copy.deepcopy(loaded.entries)

    messages, ids = loaded.build_context()

    assert messages[0].stop_reason == "compaction"
    assert [block.data for block in messages[0].content] == items
    assert ids == [compaction["id"], kept_id, *([thinking_id] if preserve_thinking else []), later_id]
    assert messages[1].text() == "Recent answer"
    assert any(isinstance(block, ThinkingContent) for block in messages[1].content) is preserve_thinking
    assert all(message.usage.total == 0 for message in messages[:-1])
    assert messages[-1].usage == later.usage
    assert estimate_tokens(messages, 1) == later.usage.total
    assert loaded.entries == before
    assert store.path.read_bytes() == saved_bytes


def test_legacy_compaction_remains_readable_and_discards_pre_compaction_usage(tmp_path):
    store = SessionStore(None, tmp_path)
    store.append_message(UserMessage("Original request"))
    kept_id = store.append_message(AssistantMessage(
        content=[ThinkingContent("old reasoning", "sig"), TextContent("Recent answer")],
        usage=Usage(input=900000, output=1000),
    ))
    entry = store.append("compaction", summary="Legacy summary", kept_ids=[kept_id])
    before = copy.deepcopy(store.entries)

    messages, ids = store.build_context()

    assert messages[0].kind == "summary"
    assert "Legacy summary" in messages[0].text()
    assert messages[1].content == [TextContent("Recent answer")]
    assert messages[1].usage.total == 0
    assert estimate_tokens(messages, 1) < 100
    assert ids == [entry["id"], kept_id]
    assert store.entries == before


def test_full_context_restores_original_active_branch_after_multiple_compactions(tmp_path):
    store = SessionStore.new(tmp_path / "sessions", tmp_path)
    originals = [
        UserMessage("First request"),
        AssistantMessage([TextContent("First answer")], usage=Usage(input=100, output=20)),
    ]
    original_ids = [store.append_message(message) for message in originals]
    store.append("compaction", summary="Earlier summary", kept_ids=[])
    newer = UserMessage("Second request")
    newer_id = store.append_message(newer)
    native = AssistantMessage(
        api="openai-responses", provider="openai", stop_reason="compaction",
        content=[RawContent("openai-responses", {"type": "compaction", "encrypted_content": "latest-state"})],
    )
    store.append("compaction", summary="Native summary", context=[message_to_dict(native)], kept_ids=[])
    branch_point = store.leaf
    store.append_message(UserMessage("Discarded branch"))
    store.leaf = branch_point
    final = AssistantMessage([TextContent("Current branch")], usage=Usage(input=30, output=4))
    final_id = store.append_message(final)
    loaded = SessionStore.load(store.path)
    before = copy.deepcopy(loaded.entries)

    messages, ids = loaded.build_full_context()

    assert [message_to_dict(message) for message in messages] == [
        message_to_dict(message) for message in [*originals, newer, final]
    ]
    assert ids == [*original_ids, newer_id, final_id]
    assert loaded.entries == before
    compacted, compacted_ids = loaded.build_context()
    assert [block.data for block in compacted[0].content] == [block.data for block in native.content]
    assert len(compacted) == 2
    assert compacted_ids == [branch_point, final_id]


def test_native_context_can_contain_multiple_messages_without_losing_order(tmp_path):
    store = SessionStore(None, tmp_path)
    store.append_message(UserMessage("Original"))
    native = AssistantMessage(
        api="openai-responses", stop_reason="compaction",
        content=[RawContent("openai-responses", {"type": "compaction", "encrypted_content": "state"})],
    )
    context = [UserMessage("Retained by provider"), native]
    entry = store.append("compaction", summary="", context=[message_to_dict(m) for m in context], kept_ids=[])

    messages, ids = store.build_context()

    assert [message_to_dict(message) for message in messages] == [message_to_dict(message) for message in context]
    assert ids == [entry["id"], entry["id"]]
