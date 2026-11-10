"""Reloading a compacted conversation must not revive its old window usage."""

from __future__ import annotations

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, TextContent, ThinkingContent, Usage, UserMessage
from virgo.harness.context import build_context_report
from virgo.harness.store import SessionStore


def saved_compaction(tmp_path):
    store = SessionStore.new(tmp_path / "sessions", tmp_path)
    store.append_message(UserMessage("Earlier context. " * 1000))
    user_id = store.append_message(UserMessage("Keep this latest request."))
    answer_id = store.append_message(AssistantMessage(
        [ThinkingContent("Old reasoning"), TextContent("Latest answer.")],
        usage=Usage(input=60_000, cache_read=20_000, output=10),
    ))
    store.append("compaction", summary="Earlier work was completed.", kept_ids=[user_id, answer_id])
    return store, answer_id


def test_retained_usage_is_cleared_only_in_reconstructed_context(tmp_path):
    store, answer_id = saved_compaction(tmp_path)
    saved = store.path.read_bytes()
    loaded = SessionStore.load(store.path)
    messages, ids = loaded.build_context()
    retained = messages[ids.index(answer_id)]
    assert retained.text() == "Latest answer."
    assert retained.usage.total == 0
    assert not any(isinstance(block, ThinkingContent) for block in retained.content)
    assert loaded.by_id[answer_id]["message"]["usage"] == {
        "input": 60_000, "output": 10, "cache_read": 20_000, "cache_write": 0,
    }
    assert store.path.read_bytes() == saved


def test_new_usage_after_compaction_resets_previous_message_indices(make_session):
    session = make_session(settings={"compaction": {"enabled": False, "keepRecentTokens": 10}})
    for i in range(5):
        faux.push("test", faux.reply("Answer " * 100))
        session.prompt(f"Question {i}")
    session.permission_mode = "plan"
    faux.push("test", faux.reply("Plan " * 100))
    session.prompt("Plan the next task")
    previous_boundary = session.main.usage_valid_from
    faux.push("test", faux.reply("A compact summary."))
    assert session.compact() is not None
    assert len(session.main.messages) < previous_boundary
    assert not build_context_report(session.main).reported_usage
    faux.push("test", faux.reply("Continued after compaction."))
    session.prompt("Continue")
    report = build_context_report(session.main)
    assert report.reported_usage
    assert report.used == session.main.messages[-1].usage.total


@pytest.mark.parametrize("after_compaction", [False, True])
def test_resumed_context_uses_only_usage_after_compaction(make_session, tmp_path, after_compaction):
    store, _ = saved_compaction(tmp_path)
    if after_compaction:
        store.append_message(UserMessage("Continue after the summary."))
        store.append_message(AssistantMessage(
            [TextContent("Continued.")], usage=Usage(input=6_000, cache_read=1_000, output=10),
        ))
    saved = store.path.read_bytes()
    session = make_session(session=store.path)
    report = build_context_report(session.main)
    assert report.used == session.context_tokens()
    if after_compaction:
        assert report.used == 7_010
    else:
        assert 0 < report.used < 80_010
        assert any(row.name == "System tools" and row.tokens > 0 for row in report.rows)
    assert store.path.read_bytes() == saved
