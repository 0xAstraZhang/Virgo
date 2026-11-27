"""Origin binding survives UI switches, background work and history branches."""
from __future__ import annotations

import threading
from types import SimpleNamespace

from virgo.agent.types import AgentTool, Block, ToolCallEvent, ToolContext, ToolResult
from virgo.ai.providers import faux
from virgo.ai.types import UserMessage
from virgo.harness.extensions import ExtensionAPI
from virgo.harness.origin_context import origin_key, source_for
from virgo.harness.session import Runtime
from virgo.harness.store import SessionStore

from tests.support.harness import tool_call_event


def add(runtime, message):
    runtime.messages.append(message)
    identity = runtime.store.append_message(message) if runtime.store else None
    runtime.ids.append(identity)
    return identity


def event(session, runtime=None):
    runtime = runtime or session.main
    return ToolCallEvent("current", "read", {"path": "README.md"}, session.tool("read"), runtime.name,
                         runtime.cwd, origin=runtime.origin)


def test_context_key_changes_for_new_constraints_not_automatic_review_logs(make_session):
    session = make_session()
    ev = event(session)
    first = origin_key(session, ev)
    session.store.append("custom", kind="permission_decision", data={"source": "workspace", "decision": "allow"})
    add(session.main, UserMessage("Continue this task."))
    assert origin_key(session, ev) == first
    session.approval_policy = "never"
    assert origin_key(session, ev) != first


def test_direct_main_event_binds_once_across_chat_switch(make_session):
    session = make_session()
    add(session.main, UserMessage("Original task."))
    ev = tool_call_event(session, "write", {"path": "README.md", "content": "Original task."})
    api = ExtensionAPI(session, "direct-integration")
    # The direct entry point must bind before the mandatory sandbox checks it.
    assert api.authorize_tool(ev) is None
    original = source_for(session, ev)
    key = origin_key(session, ev)
    session.new_session()
    add(session.main, UserMessage("Unrelated new chat."))
    assert api.authorize_tool(ev) is None
    assert source_for(session, ev) is original
    assert source_for(session, ev).root is original.root
    assert origin_key(session, ev) == key
    assert source_for(session, tool_call_event(session, "read", {"path": "README.md"})) is not original


def test_unbound_nonmain_event_cannot_borrow_main_authorization(make_session):
    session = make_session()
    ev = tool_call_event(session, "read", {"path": "README.md"}, agent="orphan")
    assert source_for(session, ev) is None
    assert isinstance(session._before_tool(ev), Block)
    assert not session.ui.questions


def test_subagent_keeps_parent_task_and_marks_delegation(make_session):
    # The inherited task identity replaces serialized delegation/approval history.
    session = make_session()
    old_main = session.main
    add(old_main, UserMessage("Original task."))
    source = old_main.origin
    session.new_session()
    child = session.create_runtime("worker", "subagent", parent=old_main)
    try:
        ev = event(session, child)
        assert source_for(session, ev).root is source.root
        assert source_for(session, ev).workspace == old_main.cwd
        assert child.origin.identity == source.identity
        assert child.origin.identity != session.main.origin.identity
        assert child.execution_context is old_main.execution_context
    finally:
        session.release_runtime(child)


def test_origin_does_not_follow_a_new_branch(make_session):
    session = make_session()
    base = add(session.main, UserMessage("Task base."))
    old_leaf = add(session.main, UserMessage("Original branch restriction."))
    source = session.main.origin
    source.root.entries()
    first = origin_key(session, event(session))
    session.store.leaf = base
    new_leaf = session.store.append_message(UserMessage("Unrelated fork grant."))
    assert source.root.entries()[-1]["id"] == old_leaf
    assert session.store.leaf == new_leaf
    assert origin_key(session, event(session)) == first


def test_late_review_accounting_stays_on_its_branch_after_reload(make_session):
    session = make_session(persist=True)
    base = add(session.main, UserMessage("Base task."))
    old_leaf = add(session.main, UserMessage("Original branch."))
    source = session.main.origin
    source.root.entries()
    session.store.leaf = base
    new_leaf = session.store.append_message(UserMessage("New branch."))
    entry = source.root.append("custom", kind="permission_decision", data={"source": "human", "decision": "allow"})
    assert entry["parent_id"] == old_leaf
    assert session.store.leaf == new_leaf
    assert not session.store.custom("permission_decision")
    reopened = SessionStore.load(session.store.path)
    assert reopened.leaf == new_leaf and not reopened.custom("permission_decision")
    latest = session.store.append_message(UserMessage("Continue new branch."))
    assert SessionStore.load(session.store.path).leaf == latest


def test_runtime_hook_binds_source_before_extensions(make_session, monkeypatch):
    session = make_session()
    child = Runtime(session, "worker", "subagent", origin_parent=session.main.origin)
    seen = []
    monkeypatch.setattr(session, "_before_tool", lambda ev: seen.append(ev.origin))
    ev = ToolCallEvent("call", "bash", {"command": "date"}, session.tool("bash"), child.name, child.cwd,
                       signal=threading.Event())
    child.agent.cfg.before_tool_call(ev)
    assert seen == [child.origin]


def test_background_subagent_captures_origin_before_worker_starts(make_session, monkeypatch):
    session = make_session()
    add(session.main, UserMessage("Original task."))
    source = session.main.origin
    jobs = session.services["jobs"]
    pending, parents = [], []
    monkeypatch.setattr(jobs, "start_callable", lambda _label, fn, **kwargs: (pending.append(fn) or SimpleNamespace(id="job")))
    monkeypatch.setattr(session.services["subagents"], "run", lambda *args, **kwargs: (parents.append(args[-1]) or ToolResult.text("ok")))
    # Direct tool integrations without a pre-attached source must capture now,
    # not look up session.main after the background thread starts.
    ctx = ToolContext("spawn", session.cwd, threading.Event())
    session.tool("agent").execute({"description": "Inspect", "prompt": "Read file", "background": True}, ctx)
    session.new_session()
    add(session.main, UserMessage("Unrelated task."))
    pending[0](threading.Event())
    assert parents == [source]


def test_loop_passes_bound_source_to_tool_execution(make_session):
    session = make_session(faux.reply(tools=[("capture_source", {})]), faux.reply("done"))
    seen = []
    session.main.tools.append(AgentTool("capture_source", "Inspect context", {"type": "object", "properties": {}},
                                       lambda _args, ctx: (seen.append(ctx.origin) or ToolResult.text("ok")),
                                       side_effect="read"))
    source = session.main.origin
    session.prompt("Inspect this task.")
    assert seen == [source]


def test_child_completions_and_retries_are_accounted_to_the_original_branch(make_session, monkeypatch):
    session = make_session(faux.error("temporary", kind="retryable"), faux.reply("child complete"), persist=True)
    base = add(session.main, UserMessage("Base task."))
    old_leaf = add(session.main, UserMessage("Read the original task's file."))
    source = session.main.origin
    # No permission review has captured this source yet. Child creation itself
    # must pin the currently existing task before any later branch switch.
    assert source.root.anchor is None
    child = Runtime(session, "worker", "subagent", origin_parent=source, tools=[])
    assert child.accounting_store is source.root
    assert source.root.anchor == old_leaf
    session.store.leaf = base
    new_leaf = session.store.append_message(UserMessage("Unrelated branch."))
    monkeypatch.setattr(child.agent.signal, "wait", lambda _seconds: False)
    child.agent.prompt("Read the original task's file.")
    assert session.store.leaf == new_leaf
    assert session.store.custom("model_call") == []
    calls = [e["data"] for e in source.root.entries() if e.get("kind") == "model_call"]
    assert [call["purpose"] for call in calls] == ["retry", "agent"]
    assert all(call["agent"] == "worker" for call in calls)
    assert SessionStore.load(session.store.path).leaf == new_leaf
