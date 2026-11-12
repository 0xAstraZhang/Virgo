"""Summary compaction: where to cut, size estimates, and manual, automatic and reactive runs."""

from __future__ import annotations

import pytest

from virgo.agent import AgentTool
from virgo.ai.providers import faux
from virgo.ai.types import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    Usage,
    UserMessage,
)
from virgo.harness.compaction import estimate_tokens, find_cut
from virgo.harness.session import AgentSession

from tests.support.harness import RecordingUI, calls


def summarizer(ctx):
    if "context summarization assistant" in ctx.system:
        return faux.reply("## Goal\nbuild the thing\n## Next Steps\n1. finish")
    return None


def scripted(*replies):
    """Responder: summary requests get a summary; other calls consume `replies` in order."""
    queue = list(replies)

    def respond(ctx):
        return summarizer(ctx) or queue.pop(0)

    return respond


def test_find_cut_never_splits_tool_pairs():
    msgs = [
        UserMessage("x" * 400),
        AssistantMessage([ToolCall("1", "read", {"path": "a"})]),
        ToolResultMessage("1", "read", [TextContent("y" * 4000)]),
        AssistantMessage([TextContent("z" * 40)]),
    ]
    cut = find_cut(msgs, keep_tokens=500)
    assert not isinstance(msgs[cut], ToolResultMessage)


def test_estimate_includes_system_and_tool_definitions_without_usage():
    messages = [UserMessage("x" * 40)]
    tool = ToolSpec("example", "description", {"type": "object"})
    assert estimate_tokens(messages, system="s" * 400) == 110
    with_tools = estimate_tokens(messages, system="s" * 400, tools=[tool])
    assert with_tools > 110
    tool.description += "t" * 400
    assert estimate_tokens(messages, system="s" * 400, tools=[tool]) == with_tools + 100


def test_reported_usage_already_includes_system_and_cached_input():
    messages = [AssistantMessage(usage=Usage(input=100, output=20, cache_read=60, cache_write=20)),
                UserMessage("next" * 4)]
    assert estimate_tokens(messages, system="s" * 4000) == 204
    # A compaction or context reset invalidates the old request's usage.
    assert estimate_tokens(messages, valid_from=1, system="s" * 4000) == 1004


def test_runtime_estimate_counts_project_instructions_and_tools_without_request_hooks(make_session, project, monkeypatch):
    (project / "AGENTS.md").write_text("project rules " * 100, encoding="utf-8")
    session = make_session()
    runtime = session.main
    monkeypatch.setattr(session.bus, "chain", lambda *a: pytest.fail("status invoked a context hook"))
    monkeypatch.setattr(session.services["search"], "prepare", lambda *a: pytest.fail("status prepared a request"))
    assert ("project rules " * 100).strip() in runtime.system
    baseline = runtime.context_tokens()
    assert baseline > len(runtime.system) // 4 > 0
    runtime.system += "s" * 400
    assert runtime.context_tokens() == baseline + 100
    runtime.tools = [*runtime.tools, AgentTool("example", "t" * 400, {"type": "object"}, lambda *a: None)]
    assert runtime.context_tokens() > baseline + 200
    assert session.store.total_usage() == dict(input=0, output=0, cache_read=0, cache_write=0)


def test_manual_compaction_keeps_recent_and_strips_thinking(make_session, project):
    s = make_session(persist=True, settings={"compaction": {"keepRecentTokens": 10}})
    big = "data " * 2000
    for i in range(3):
        resp = scripted(faux.reply(f"answer {i} {big}", thinking="secret reasoning"))
        faux.push("test", resp)
        s.prompt(f"question {i}")
    faux.push("test", scripted())
    res = s.compact(focus="the API")
    assert res is not None and res["tokens_before"] > 0
    msgs = s.main.messages
    assert msgs[0].kind == "summary" and "build the thing" in msgs[0].text()
    assert not any(
        isinstance(b, ThinkingContent) for m in msgs if isinstance(m, AssistantMessage) for b in m.content
    )
    assert any(
        isinstance(m, UserMessage) and m.text() == "question 2" for m in msgs
    )  # current request pinned/kept
    # the summarizer saw the focus and the transcript
    summary_req = [c for c in calls() if "context summarization assistant" in c["system"]][0]
    assert (
        "the API" in summary_req["messages"][0]["content"]
        and "question 0" in summary_req["messages"][0]["content"]
    )
    # reloading the session file rebuilds the same compacted context
    s2 = AgentSession(
        project, ui=RecordingUI(), model="faux/test", session=s.store.path, permission_mode="accept-all"
    )
    assert [m.text() if hasattr(m, "text") else None for m in s2.main.messages] == [
        m.text() if hasattr(m, "text") else None for m in msgs
    ]
    s2.shutdown()


def test_auto_compaction_triggers_before_model_call(make_session):
    s = make_session(settings={"compaction": {"keepRecentTokens": 50}})
    faux.push("test", scripted(faux.reply("x " * 1500)))
    s.prompt("first")
    s.settings.set("compaction.triggerTokens", 300)
    faux.push("test", scripted(), scripted(faux.reply("after compaction")))  # summary call comes first
    s.prompt("second")
    assert s.main.messages[0].kind == "summary"
    assert s.main.agent.last_text == "after compaction"


def test_reactive_compaction_on_overflow(make_session):
    s = make_session()
    faux.push("test", scripted(faux.reply("long " * 2000)))
    s.prompt("first")
    faux.push(
        "test",
        faux.error("prompt is too long: 2000000 tokens > 1000000", kind="overflow"),
        scripted(),
        scripted(faux.reply("fits now")),
    )
    s.prompt("second")
    assert s.main.agent.last_text == "fits now"
    assert s.main.messages[0].kind == "summary"


def test_serialization_keeps_complete_tool_arguments_and_tail_evidence_within_budget():
    from virgo.harness.compaction import serialize

    command = "x" * 800 + " verify --output /tmp/complete-result.json"
    output = "progress\n" * 1000 + "ERROR: missing entry\nFull output: /tmp/full-log.txt"
    text = serialize([
        AssistantMessage([ToolCall("check", "bash", {"command": command})]),
        ToolResultMessage("check", "bash", [TextContent(output)], is_error=True),
    ])
    assert command in text and output in text
    assert "(error)" in text and "omitted" not in text


def test_serialization_applies_only_the_total_budget_with_explicit_omission(monkeypatch):
    from virgo.harness import compaction

    monkeypatch.setattr(compaction, "MAX_INPUT_CHARS", 6000)
    text = compaction.serialize([
        UserMessage("original request"),
        AssistantMessage([ToolCall("large", "write", {"content": "x" * 20_000})]),
        ToolResultMessage("large", "write", [TextContent("ERROR at end\nOutput: /tmp/result.txt")]),
    ])
    assert len(text) == 6000
    assert "original request" in text and "middle of transcript omitted" in text
    assert text.endswith("ERROR at end\nOutput: /tmp/result.txt")


def test_freeform_summary_restores_and_can_be_compacted_incrementally(make_session):
    session = make_session(
        faux.reply("old work " * 1000),
        faux.reply("Keep the original goal; the user corrected the target to /tmp/new. Pending: verification."),
        persist=True, settings={"compaction": {"keepRecentTokens": 10}},
    )
    session.prompt("original request")
    session.compact()
    restored = make_session(
        faux.reply("new progress " * 1000), faux.reply("Target /tmp/new is ready; verification still pending."),
        session=session.store.path, settings={"compaction": {"keepRecentTokens": 10}},
    )
    assert "user corrected the target" in restored.main.messages[0].text()
    restored.prompt("continue that work")
    result = restored.compact()
    assert result is not None and "verification still pending" in result["summary"]
    request = calls()[-1]["messages"][0]["content"]
    assert "<previous-summary>" in request and "user corrected the target" in request
    assert "continue that work" in request
    assert any("new progress" in m.text() for m in restored.main.messages)
    assert any(m.text() == "continue that work" for m in restored.main.messages)
