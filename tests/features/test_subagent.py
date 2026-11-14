"""Sub-agents start from a fresh context with only their own tools."""

from __future__ import annotations

import threading

from virgo.agent.types import ToolContext
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
    # Explore uses plan permissions, where only the POSIX command tool is supported.
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
    assert sorted(seen["tools"]) == ["bash", "glob", "grep", "read"]



def test_subagent_schema_exposes_capabilities_not_types(make_session):
    session = make_session()
    properties = session.tool("agent").parameters["properties"]
    assert "read_only" in properties
    assert "type" not in properties
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

    session = make_session(mode="accept-all", settings={"shell": {"executable": "bash"}})
    faux.push("test", faux.reply(tools=[("agent", {
        "description": "inspect", "prompt": "Inspect the workspace.", "read_only": True,
    })]))
    faux.handler("test", respond)
    session.prompt("Inspect without changing files.")
    assert not target.exists()
    assert sorted(child_tools) == ["bash", "glob", "grep", "read"]
    assert tool_results(session)[0].text() == "Read-only inspection complete."


def test_legacy_explore_cannot_override_read_only_with_false(make_session):
    session = make_session()
    ctx = ToolContext("legacy", session.cwd, threading.Event(), review_source=session.main.review_source)
    result = session.tool("agent").execute({
        "description": "legacy", "prompt": "inspect", "type": "explore", "read_only": False,
    }, ctx)
    assert result.is_error
    assert "conflict" in result.as_text().lower()


def test_capability_only_subagent_does_not_inherit_shadowed_general_definition(make_session, project):
    agents = project / ".virgo" / "agents"
    agents.mkdir(parents=True)
    (agents / "general.md").write_text(
        "---\nname: general\ndescription: legacy editing worker\n"
        "tools: write, edit\nmodel: faux/legacy\n---\n"
        "LEGACY_GENERAL_INSTRUCTIONS\n",
        encoding="utf-8",
    )
    child_requests = []

    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            child_requests.append(ctx)
            return faux.reply("inspection complete")
        return faux.reply("main complete")

    session = make_session(settings={"shell": {"executable": "bash"}})
    faux.handler("test", respond)
    faux.push("test", faux.reply(tools=[("agent", {
        "description": "inspect", "prompt": "Inspect files.", "read_only": True,
    })]))
    session.prompt("Inspect without changes.")

    assert tool_results(session)[0].text() == "inspection complete"
    assert len(child_requests) == 1
    assert sorted(tool.name for tool in child_requests[0].tools) == ["bash", "glob", "grep", "read"]
    assert "LEGACY_GENERAL_INSTRUCTIONS" not in child_requests[0].system
    assert not faux.CALLS["legacy"]

    def legacy_response(ctx):
        assert "LEGACY_GENERAL_INSTRUCTIONS" in ctx.system
        assert sorted(tool.name for tool in ctx.tools) == ["edit", "write"]
        return faux.reply("legacy worker complete")

    faux.script("legacy", legacy_response)
    faux.push("test", faux.reply(tools=[("agent", {
        "description": "legacy", "prompt": "Run old worker.", "type": "general",
    })]))
    session.prompt("Use the explicitly named legacy worker.")
    assert tool_results(session)[-1].text() == "legacy worker complete"
    assert len(faux.CALLS["legacy"]) == 1
