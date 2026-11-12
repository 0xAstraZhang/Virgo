"""Sub-agents start from a fresh context with only their own tools."""

from __future__ import annotations

from virgo.ai.providers import faux

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
        faux.reply(tools=[("agent", {"description": "look", "prompt": "find files", "type": "explore"})]),
    )
    faux.handler("test", lambda ctx: respond(ctx) or faux.reply("main done"))
    s.prompt("delegate")
    r = tool_results(s)
    assert r[0].text() == "sub result: found files"
    assert s.main.agent.last_text == "main done"


def test_subagent_system_prompt_lists_only_its_own_tools(make_session):
    seen = {}

    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            seen.update(system=ctx.system, tools=[t.name for t in ctx.tools])
            return faux.reply("sub done")
        return faux.reply("main done")

    session = make_session()
    faux.push("test", faux.reply(tools=[("agent", {"description": "look", "prompt": "find", "type": "explore"})]))
    faux.handler("test", respond)
    session.prompt("delegate")
    listed = seen["system"].split("<tools>\n", 1)[1].split("\n</tools>", 1)[0]
    names = [line[2:].split(":", 1)[0] for line in listed.splitlines()]
    assert sorted(names) == sorted(seen["tools"]) == ["bash", "glob", "grep", "read"]
