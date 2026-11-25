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
    assert r[0].text() == "sub result: found files\n\n[agent_id: subagent-1]"
    assert r[0].details["agent_id"] == "subagent-1"
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
    assert result.text() == "Report" + tail + "\n\n[agent_id: subagent-1]"


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
    assert result.text() == "First finding\n\n[agent_id: subagent-1]"


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
    assert tool_results(session)[0].text() == "Read-only inspection complete.\n\n[agent_id: subagent-1]"


def test_subagent_reuse_preserves_context_within_session(make_session):
    child_turns = []
    main_replies = iter([
        faux.reply(tools=[("agent", {"description": "first", "prompt": "Check alpha"})]),
        faux.reply(tools=[("agent", {"description": "second", "prompt": "Now check beta", "agent_id": "subagent-1"})]),
        faux.reply("all done"),
    ])

    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            child_turns.append([m.text() for m in ctx.messages])
            if len(child_turns) == 1:
                return faux.reply("first finding")
            return faux.reply("follow-up finding")
        return next(main_replies)

    session = make_session()
    faux.handler("test", respond)
    session.prompt("delegate twice")

    results = tool_results(session)
    assert len(results) == 2
    assert results[0].details["agent_id"] == "subagent-1"
    assert results[0].details["resumed"] is False
    assert results[1].details["agent_id"] == "subagent-1"
    assert results[1].details["resumed"] is True
    assert child_turns[0] == ["Check alpha"]
    assert child_turns[1] == ["Check alpha", "first finding", "Now check beta"]
    summary = session.subagent_store_for().list()
    assert len(summary) == 1
    assert summary[0]["id"] == "subagent-1"
    assert summary[0]["runs"] == 2
    assert summary[0]["status"] == "done"


def test_subagent_reuse_survives_session_resume(make_session):
    child_calls = []

    def make_handler(main_script):
        main_iter = iter(main_script)

        def respond(ctx):
            if "You are a Virgo sub-agent" in ctx.system:
                child_calls.append([m.text() for m in ctx.messages])
                return faux.reply("reply " + str(len(child_calls)))
            return next(main_iter)

        return respond

    first = make_session(persist=True)
    session_path = first.store.path
    faux.handler(
        "test",
        make_handler([
            faux.reply(tools=[("agent", {"description": "phase 1", "prompt": "Task one"})]),
            faux.reply("phase 1 done"),
        ]),
    )
    first.prompt("start")
    trace_file = first.subagent_store_for().directory / "subagent-1.jsonl"
    assert trace_file.is_file()
    first.shutdown()

    resumed = make_session(session=session_path)
    faux.handler(
        "test",
        make_handler([
            faux.reply(tools=[("agent", {"description": "phase 2", "prompt": "Task two", "agent_id": "subagent-1"})]),
            faux.reply("phase 2 done"),
        ]),
    )
    resumed.prompt("continue")
    assert child_calls[1] == ["Task one", "reply 1", "Task two"]
    assert len(resumed.subagent_store_for().entries("subagent-1")) > 0


def test_subagent_rejects_unknown_and_busy_agent_id(make_session):
    import threading

    started = threading.Event()
    release = threading.Event()

    session = make_session()
    faux.push(
        "test",
        faux.reply(tools=[("agent", {"description": "bad", "prompt": "hi", "agent_id": "subagent-99"})]),
        faux.reply("main handled unknown"),
    )
    session.prompt("bad reuse")
    unknown_result = tool_results(session)[-1]
    assert unknown_result.is_error
    assert "subagent-99" in unknown_result.text()

    # Background agent busy check
    main_replies = iter([
        faux.reply(tools=[("agent", {"description": "slow", "prompt": "work", "background": True})]),
        faux.reply(tools=[("agent", {"description": "dup", "prompt": "again", "agent_id": "subagent-1"})]),
        faux.reply("done"),
        faux.reply("bg notified"),
    ])

    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            started.set()
            release.wait(timeout=5)
            return faux.reply("slow done")
        return next(main_replies)

    faux.handler("test", respond)
    try:
        session.prompt("start bg")
        assert started.wait(timeout=5)
    finally:
        release.set()
        session.wait_idle(timeout=5)

    results = tool_results(session)
    bg_start = results[-2]
    busy_err = results[-1]
    assert not bg_start.is_error
    assert bg_start.details["agent_id"] == "subagent-1"
    assert bg_start.details["background"] is True
    assert busy_err.is_error
    assert "subagent-1" in busy_err.text()


def test_subagent_traces_removed_when_session_trashed(make_session):
    session = make_session(persist=True)
    session_path = session.store.path
    trace_dir = session.subagent_store_for().directory
    faux.push(
        "test",
        faux.reply(tools=[("agent", {"description": "inspect", "prompt": "Check files"})]),
        faux.reply("child done"),
        faux.reply("main done"),
    )
    session.prompt("go")
    assert trace_dir.is_dir()
    session.trash_session(session_path)
    assert not trace_dir.exists()


def test_background_subagent_in_previous_session_does_not_block_new_session_subagent(make_session):
    import threading

    first_started = threading.Event()
    release_first = threading.Event()

    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            if ctx.messages[0].text() == "Slow task A":
                first_started.set()
                release_first.wait(timeout=5)
                return faux.reply("A done")
            return faux.reply("B done")
        return None

    session = make_session(persist=True)
    faux.handler("test", respond)

    # Start a background subagent-1 in conversation A.
    main_a = iter([
        faux.reply(tools=[("agent", {"description": "task A", "prompt": "Slow task A", "background": True})]),
        faux.reply("A launched"),
        faux.reply("A finished"),
    ])
    faux.handler("test", lambda ctx: respond(ctx) or next(main_a))
    try:
        session.prompt("run A")
        assert first_started.wait(timeout=5)

        # Switch to a new conversation B while A's subagent-1 is still running.
        session.new_session()
        main_b = iter([
            faux.reply(tools=[("agent", {"description": "task B", "prompt": "Fast task B"})]),
            faux.reply("B finished"),
        ])
        faux.handler("test", lambda ctx: respond(ctx) or next(main_b))
        session.prompt("run B")

        result_b = tool_results(session)[-1]
        assert not result_b.is_error
        assert result_b.details["agent_id"] == "subagent-1"
        assert "B done" in result_b.text()
    finally:
        release_first.set()
        session.wait_idle(timeout=5)


def test_trashing_session_detaches_active_background_subagent_writer(make_session):
    import threading

    started = threading.Event()
    release = threading.Event()

    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            started.set()
            release.wait(timeout=5)
            return faux.reply("late child reply")
        return None

    session = make_session(persist=True)
    session_path = session.store.path
    trace_dir = session.subagent_store_for().directory
    main_replies = iter([
        faux.reply(tools=[("agent", {"description": "slow", "prompt": "Wait then write", "background": True})]),
        faux.reply("started"),
        faux.reply("done"),
    ])
    faux.handler("test", lambda ctx: respond(ctx) or next(main_replies))
    try:
        session.prompt("launch")
        assert started.wait(timeout=5)
        assert trace_dir.is_dir()

        # Trash the parent session while the background subagent is still running.
        session.trash_session(session_path)
        assert not trace_dir.exists()
    finally:
        release.set()
        session.wait_idle(timeout=5)

    # Late events and handle release must not recreate the deleted trace directory.
    assert not trace_dir.exists()


def test_subagent_default_budget(make_session):
    settings = make_session().settings
    assert settings.get("subagents.maxTurns") == 20
    assert settings.get("subagents.maxTotalTurns") == 40
    assert settings.get("subagents.effort") is None


def test_subagent_zero_total_turns_starts_no_model_request(make_session):
    session = make_session(settings={"subagents": {"maxTotalTurns": 0}})
    result = session.services["subagents"].run("inspect", "inspect", None)
    assert result.is_error and result.details["status"] == "turn_limit"
    assert not faux.CALLS["test"]


def test_subagent_total_turns_survive_compaction_and_session_resume(make_session):
    settings = {"subagents": {"maxTurns": 2, "maxTotalTurns": 3}}
    first = make_session(persist=True, settings=settings)
    # Give the parent a durable transcript before reopening it.
    first.prompt("start")
    lookup = faux.reply(tools=[("glob", {"pattern": "*"})])
    faux.handler("test", lambda _: lookup)
    child = first.services["subagents"].run("look", "first", None)
    assert child.details["status"] == "turn_limit"
    handle = first.subagent_store_for().get(child.details["agent_id"])
    handle.store.append("compaction", summary="Previous findings", kept_ids=[])
    first.shutdown()

    resumed = make_session(session=first.store.path, settings=settings)
    calls = []

    def respond(ctx):
        calls.append(ctx)
        return lookup

    faux.handler("test", respond)
    run = resumed.services["subagents"].run
    limited = run("continue", "second", None, agent_id=handle.id)
    assert limited.is_error and limited.details["status"] == "turn_limit"
    assert len(calls) == 1
    for _ in range(2):
        exhausted = run("continue", "third", None, agent_id=handle.id)
        assert exhausted.is_error and exhausted.details["status"] == "turn_limit"
        assert "subagents.maxTotalTurns" in exhausted.as_text()
    assert len(calls) == 1
    assert not resumed.subagent_store_for().get(handle.id).active
    faux.push("test", faux.reply("fresh"))
    assert not run("another task", "fourth", None).is_error


@pytest.mark.parametrize("override,expected", [(None, "high"), ("low", "low")])
def test_subagent_effort_reaches_provider_and_usage(make_session, monkeypatch, override, expected):
    session = make_session(settings={"effort": "high", "subagents": {"effort": override}})
    requests = []
    stream = faux.stream

    def observe(model, ctx, opts):
        requests.append(opts.effort)
        return stream(model, ctx, opts)

    monkeypatch.setattr(faux, "stream", observe)
    faux.push("test", faux.reply(tools=[("agent", {"description": "look", "prompt": "find files"})]),
              faux.reply("child done"), faux.reply("main done"))
    session.prompt("delegate")
    assert requests == ["high", expected, "high"]
    usage = [e["data"] for e in session.store.entries if e.get("kind") == "model_call"]
    assert [call["effort"] for call in usage if call["agent"] == "subagent-1"] == [expected]
