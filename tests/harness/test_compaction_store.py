"""Compaction round trips keep portable summaries and L1 prune stubs intact on disk."""

from __future__ import annotations

import copy

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
    message_to_dict,
)
from virgo.harness.compaction import estimate_tokens
from virgo.harness.store import SessionStore

from tests.support.harness import calls


@pytest.mark.parametrize("tool,status", [
    ("edit", "No code changes made yet"),
    ("write", "No solution files created yet"),
], ids=["edit", "write"])
def test_retained_result_supersedes_evicted_status(make_session, project, tool, status):
    path = project / "solution.py"
    content = "pass\n" * 200
    if tool == "edit":
        path.write_text("TODO", encoding="utf-8")
        args = {"path": path.name, "edits": [{"old_text": "TODO", "new_text": content}]}
    else:
        args = {"path": path.name, "content": content}
    session = make_session(
        faux.reply(status), faux.reply(tools=[(tool, args, "apply-solution")]), faux.reply("Done."),
        faux.reply(status), persist=True, settings={"compaction": {"keepRecentTokens": 100}},
    )
    session.prompt("Inspect the problem.")
    owner = "Implement the solution."
    session.prompt(owner)
    assert path.read_text(encoding="utf-8") == content
    assert session.compact()["summary"] == status
    evicted_msgs = calls()[-1]["messages"][:-1]
    evicted_text = "\n".join(str(m.get("content", "")) for m in evicted_msgs)
    assert status in evicted_text and "solution.py" not in evicted_text

    live = (session.main.messages, session.main.ids)
    assert live == session.store.build_context() == SessionStore.load(session.store.path).build_context()
    checkpoint, pinned, call, result, _ = live[0]
    framing, prose = checkpoint.text().split("\n\n", 1)
    assert "only the earlier evicted turns before the retained recent messages" in framing.lower()
    assert "newer retained messages below supersede earlier status claims" in framing.lower()
    assert prose == status + "\n</conversation_summary>"
    assert pinned.text() == owner
    assert call.tool_calls()[0].id == result.tool_call_id == "apply-solution"
    assert result.tool_name == tool and not result.is_error and path.name in result.text()


def test_legacy_summary_compaction_remains_readable_and_discards_pre_compaction_usage(tmp_path):
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


def test_legacy_native_compaction_record_is_rejected_on_load(tmp_path):
    store = SessionStore.new(tmp_path / "sessions", tmp_path)
    store.append_message(UserMessage("Original request"))
    # Manually write a legacy native compaction record with a "context" field.
    with store.path.open("a", encoding="utf-8") as handle:
        import json

        handle.write(json.dumps({
            "type": "compaction",
            "id": "deadbeef",
            "parent_id": store.leaf,
            "timestamp": "2026-01-01T00:00:00",
            "summary": "Legacy native",
            "context": [message_to_dict(AssistantMessage([TextContent("opaque")]))],
            "kept_ids": [],
        }) + "\n")

    with pytest.raises(ValueError, match="legacy native compaction records are no longer supported"):
        SessionStore.load(store.path)


def test_prune_compaction_persists_composes_and_resets_after_summary(tmp_path):
    store = SessionStore.new(tmp_path / "sessions", tmp_path)
    u1 = store.append_message(UserMessage("Inspect files"))
    a1 = store.append_message(AssistantMessage([ToolCall("c1", "read", {"path": "a.py"})]))
    r1 = store.append_message(ToolResultMessage("c1", "read", [TextContent("A" * 5000)]))
    a2 = store.append_message(AssistantMessage([ToolCall("c2", "read", {"path": "b.py"})]))
    r2 = store.append_message(ToolResultMessage("c2", "read", [TextContent("B" * 5000)]))

    store.append("compaction", mode="prune", pruned={r1: "[Pruned tool result: read(path='a.py')]"}, reason="auto")
    store.append("compaction", mode="prune", pruned={r2: "[Pruned tool result: read(path='b.py')]"}, reason="auto")

    loaded = SessionStore.load(store.path)
    messages, ids = loaded.build_context()
    assert ids == [u1, a1, r1, a2, r2]
    assert messages[2].text() == "[Pruned tool result: read(path='a.py')]"
    assert messages[4].text() == "[Pruned tool result: read(path='b.py')]"
    # Original full context on disk is untouched.
    full_msgs, _ = loaded.build_full_context()
    assert full_msgs[2].text() == "A" * 5000
    assert full_msgs[4].text() == "B" * 5000

    # A later summary compaction supersedes earlier prune records, and post-summary prunes still apply.
    a3 = store.append_message(AssistantMessage([ToolCall("c3", "read", {"path": "c.py"})]))
    r3 = store.append_message(ToolResultMessage("c3", "read", [TextContent("C" * 5000)]))
    summary_entry = store.append("compaction", mode="summary", summary="Summary", kept_ids=[a3, r3])
    store.append("compaction", mode="prune", pruned={r3: "[Pruned tool result: read(path='c.py')]"}, reason="auto")

    reloaded = SessionStore.load(store.path)
    msgs2, ids2 = reloaded.build_context()
    assert ids2 == [summary_entry["id"], a3, r3]
    assert msgs2[0].kind == "summary"
    assert msgs2[2].text() == "[Pruned tool result: read(path='c.py')]"
    assert reloaded.last_compaction(mode="summary")["id"] == summary_entry["id"]
    assert reloaded.last_compaction(mode="prune")["mode"] == "prune"


def test_full_context_restores_original_active_branch_after_multiple_compactions(tmp_path):
    store = SessionStore.new(tmp_path / "sessions", tmp_path)
    originals = [
        UserMessage("First request"),
        AssistantMessage([TextContent("First answer")], usage=Usage(input=100, output=20)),
    ]
    original_ids = [store.append_message(message) for message in originals]
    store.append("compaction", mode="summary", summary="Earlier summary", kept_ids=[])
    newer = UserMessage("Second request")
    newer_id = store.append_message(newer)
    store.append("compaction", mode="summary", summary="Latest summary", kept_ids=[])
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
    assert compacted[0].kind == "summary" and "Latest summary" in compacted[0].text()
    assert len(compacted) == 2
    assert compacted_ids == [branch_point, final_id]
