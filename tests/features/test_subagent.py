"""Sub-agents start from a fresh context with only their own tools."""

from __future__ import annotations

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
        faux.reply(tools=[("agent", {"description": "look", "prompt": "find files", "read_only": True})]),
    )
    faux.handler("test", lambda ctx: respond(ctx) or faux.reply("main done"))
    s.prompt("delegate")
    r = tool_results(s)
    assert r[0].text() == "sub result: found files"
    assert s.main.agent.last_text == "main done"


def test_subagent_request_exposes_only_its_own_tools(make_session):
    # Read-only delegation uses a capability ceiling and controlled Git reads.
    seen = {}

    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            seen.update(system=ctx.system, tools=[t.name for t in ctx.tools])
            return faux.reply("sub done")
        return faux.reply("main done")

    session = make_session(settings={"shell": {"executable": "bash"}})
    faux.push("test", faux.reply(tools=[("agent", {"description": "look", "prompt": "find", "read_only": True})]))
    faux.handler("test", respond)
    session.prompt("delegate")
    assert "<tools>" not in seen["system"]
    assert set(seen["tools"]) == {tool.name for tool in session.tools_for("subagent")}
    assert {"write", "edit", "bash", "read"}.issubset(seen["tools"])


def test_subagent_schema_exposes_capabilities_not_types(make_session):
    session = make_session()
    properties = session.tool("agent").parameters["properties"]
    assert "read_only" in properties
    assert "type" not in properties
    assert session.tool("agent").parameters["additionalProperties"] is False
    assert "<agent_types>" not in session.main.system


def test_read_only_subagent_blocks_shell_writes_even_if_parent_is_unrestricted(make_session, project):
    target = project / "forbidden.txt"
    child_tools = []

    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            child_tools[:] = [tool.name for tool in ctx.tools]
            if len(ctx.messages) == 1:
                return faux.reply(tools=[("bash", {"command": "printf unsafe > forbidden.txt"})])
            result = next(m for m in ctx.messages if isinstance(m, ToolResultMessage))
            assert result.is_error
            return faux.reply("Read-only inspection complete.")
        return faux.reply("Main complete.")

    session = make_session(approval="never", settings={"shell": {"executable": "bash"}})
    faux.push("test", faux.reply(tools=[("agent", {
        "description": "inspect", "prompt": "Inspect the workspace.", "read_only": True,
    })]))
    faux.handler("test", respond)
    session.prompt("Inspect without changing files.")
    assert not target.exists()
    assert {"write", "edit", "bash", "read"}.issubset(child_tools)
    assert tool_results(session)[0].text() == "Read-only inspection complete."
