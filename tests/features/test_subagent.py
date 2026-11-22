"""Sub-agents start from a fresh context with only their own tools."""

from __future__ import annotations

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage

from tests.support.harness import tool_results


def test_subagent_fresh_context_and_no_recursion(make_session):
    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            assert "agent" not in [t.name for t in ctx.tools]
            assert len(ctx.messages) in (1, 3)  # fresh context: just the task (+ its own tool round)
            if len(ctx.messages) == 1:
                return faux.reply(tools=[("glob", {"pattern": "*"})])
            return faux.reply("sub result: found files")
        return None

    s = make_session()
    faux.push(
        "test",
        faux.reply(tools=[("agent", {"description": "look", "prompt": "find files"})]),
    )
    faux.handler("test", lambda ctx: respond(ctx) or faux.reply("main done"))
    s.prompt("delegate")
    r = tool_results(s)
    assert r[0].text() == "sub result: found files"
    assert s.main.agent.last_text == "main done"


@pytest.mark.parametrize("tail", [" from the child", ""], ids=["visible-tail", "empty-tail"])
def test_subagent_returns_complete_answer_after_length_continuation(make_session, tail):
    session = make_session(
        faux.reply(tools=[("agent", {"description": "inspect", "prompt": "Inspect the files."})]),
        faux.reply("Report", stop_reason="length"),
        faux.reply(tail),
        faux.reply("main done"),
    )
    session.prompt("delegate")
    (result,) = tool_results(session)
    assert not result.is_error
    assert result.text() == "Report" + tail


def test_subagent_returns_complete_answer_after_overflow_compaction(make_session):
    session = make_session(
        faux.reply(tools=[("agent", {"description": "inspect", "prompt": "Inspect the files."})]),
        faux.reply("First ", stop_reason="length"),
        faux.error("context overflow", kind="overflow"),
        faux.reply("Earlier progress"),
        faux.reply("finding"),
        faux.reply("main done"),
        settings={"compaction": {"keepRecentTokens": 1}},
    )
    session.prompt("delegate")

    continuation = faux.CALLS["test"][-2]["messages"]
    assert continuation[0]["kind"] == "summary"
    assert not any(message["role"] == "assistant" for message in continuation)
    (result,) = tool_results(session)
    assert not result.is_error
    assert result.text() == "First finding"


def test_subagent_request_exposes_only_its_own_tools(make_session):
    # Read-only delegation uses a capability ceiling and controlled Git reads.
    seen = {}

    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            seen.update(system=ctx.system, tools=[t.name for t in ctx.tools])
            return faux.reply("sub done")
        return faux.reply("main done")

    session = make_session(settings={"shell": {"executable": "bash"}})
    faux.push("test", faux.reply(tools=[("agent", {"description": "look", "prompt": "find"})]))
    faux.handler("test", respond)
    session.prompt("delegate")
    assert "<tools>" not in seen["system"]
    assert set(seen["tools"]) == {tool.name for tool in session.tools_for("subagent")}
    assert {"write", "edit", "bash", "read"}.issubset(seen["tools"])


def test_subagent_schema_exposes_capabilities_not_types(make_session):
    session = make_session()
    properties = session.tool("agent").parameters["properties"]
    assert "read_only" not in properties
    assert "type" not in properties
    assert session.tool("agent").parameters["additionalProperties"] is False
    assert "<agent_types>" not in session.main.system


def test_subagent_inherits_parent_readonly_file_boundary(broker_session, project):
    target = project / "forbidden.txt"
    child_tools = []

    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            child_tools[:] = [tool.name for tool in ctx.tools]
            if len(ctx.messages) == 1:
                return faux.reply(tools=[("write", {"path": "forbidden.txt", "content": "unsafe"})])
            result = next(m for m in ctx.messages if isinstance(m, ToolResultMessage))
            assert result.is_error
            return faux.reply("Read-only inspection complete.")
        return faux.reply("Main complete.")

    session = broker_session(approval="never", preset="read-only", settings={"shell": {"executable": "bash"}})
    faux.push("test", faux.reply(tools=[("agent", {
        "description": "inspect", "prompt": "Inspect the workspace.",
    })]))
    faux.handler("test", respond)
    session.prompt("Inspect without changing files.")
    assert not target.exists()
    assert {"write", "edit", "bash", "read"}.issubset(child_tools)
    assert tool_results(session)[0].text() == "Read-only inspection complete."
