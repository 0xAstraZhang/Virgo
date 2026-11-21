"""Explicit authorization checks retain workspace, origin and execution boundaries."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from virgo.agent.types import Block, ToolContext
from virgo.ai.providers import faux
from virgo.harness.permissions import APPROVAL_POLICIES, approval_policy_label, normalize_approval_policy

from tests.support.harness import RecordingUI, tool_call_event, tool_results


def request(session, tool, arguments, *, permissions=None, call_id="request"):
    context = ToolContext(call_id, session.cwd, threading.Event(), origin=session.main.origin)
    return session.tool("request_permissions").execute({
        "tool": tool, "arguments": arguments,
        "permissions": permissions or {"write_paths": [arguments["path"]]},
        "reason": "Run this exact operation outside the default boundary",
    }, context)


def test_public_modes_and_legacy_config_aliases():
    assert APPROVAL_POLICIES == ("ask", "never")
    assert all(approval_policy_label(value) for value in APPROVAL_POLICIES)
    for retired in ("default", "accept-edits", "yolo", "auto-review", "accept-all", "plan", "unknown"):
        with pytest.raises(ValueError, match="approval policy"):
            normalize_approval_policy(retired)


def test_auto_review_executes_internal_changes_without_asking(broker_session, project):
    # Retain workspace mutation coverage while retiring the model reviewer.
    ui = RecordingUI()
    session = broker_session(
        faux.reply(tools=[("write", {"path": "a.txt", "content": "1"}), ("write", {"path": "b.txt", "content": "2"})]),
        faux.reply("done"), ui=ui,
    )
    session.prompt("create the files")
    assert (project / "a.txt").read_text() == "1"
    assert (project / "b.txt").read_text() == "2"
    assert not ui.questions and all(not result.is_error for result in tool_results(session))
    assert not session.store.custom("permission_decision")
    assert not session.store.custom("model_call") or all(row["purpose"] != "permission" for row in session.store.custom("model_call"))


def test_session_approval_persists_across_edits_until_revoked_and_isolates_siblings(broker_session, tmp_path):
    ui = RecordingUI(answers=["y", "n", "a"])
    session = broker_session(ui=ui)
    outside = tmp_path / "outside.txt"
    sibling = tmp_path / "sibling.txt"
    args = {"path": str(outside), "content": "one"}
    changed = {"path": str(outside), "content": "two"}
    sibling_args = {"path": str(sibling), "content": "blocked"}
    operations = [
        ("write", args),          # Prompts #1 -> 'y' (session grant for outside.txt)
        ("write", changed),       # Reuses session grant without prompting!
        ("write", sibling_args),  # Unapproved sibling prompts #2 -> 'n' (denied)
    ]
    faux.script("test", *(faux.reply(tools=[op]) for op in operations), faux.reply("done"))
    session.prompt("Exercise session-scoped path prefix permissions")
    results = tool_results(session)
    assert [result.is_error for result in results] == [False, False, True]
    assert len(ui.questions) == 2
    assert outside.read_text() == "two"
    assert not sibling.exists()

    # Revoking clears session grants; next write prompts #3 -> 'a' (persistent grant).
    session.services["execution"].revoke()
    faux.script("test", faux.reply(tools=[("write", {"path": str(outside), "content": "three"})]), faux.reply("done"))
    session.prompt("Write after revoke")
    assert not tool_results(session)[-1].is_error
    assert len(ui.questions) == 3
    assert outside.read_text() == "three"
    assert session.services["execution"].mode_for(session.main).write_paths == (str(session.cwd),)


def test_children_inherit_permission_ceilings_without_task_instructions(broker_session):
    session = broker_session()
    session.services["execution"].select_mode("read-only")
    child = session.create_runtime("child", "subagent", parent=session.main)
    grandchild = session.create_runtime("grandchild", "subagent", parent=child)
    try:
        for runtime in (child, grandchild):
            assert runtime.execution_context is session.main.execution_context
            event = tool_call_event(session, "write", {"path": "a.txt", "content": "x"}, agent=runtime.name)
            event.origin = runtime.origin
            assert isinstance(session._before_tool(event), Block)
    finally:
        session.release_runtime(grandchild)
        session.release_runtime(child)


def test_unattended_review_fallback_is_denied(broker_session, tmp_path):
    class Headless(RecordingUI):
        def confirm(self, question, agent="main"):
            return None

    session = broker_session(ui=Headless())
    args = {"path": str(tmp_path / "x"), "content": ""}
    result = request(session, "write", args)
    assert result.is_error and "not granted" in result.content[0].text
    assert isinstance(session._before_tool(tool_call_event(session, "write", args)), Block)
    assert not (tmp_path / "x").exists()


def test_review_and_human_approval_recheck_changed_mode(broker_session, tmp_path, monkeypatch):
    session = broker_session(allow=False)
    args = {"path": str(tmp_path / "outside"), "content": "x"}

    def confirm(*_):
        session.services["execution"].select_mode("read-only")
        return "y"

    monkeypatch.setattr(session.ui, "confirm", confirm)
    result = request(session, "write", args)
    assert result.is_error and "expired" in result.content[0].text
    assert not session.main.execution_context.resources.write_paths
    assert isinstance(session._before_tool(tool_call_event(session, "write", args)), Block)


def test_execution_rechecks_mode_arguments_and_symlink_boundary(broker_session, tmp_path):
    session = broker_session(allow=False)
    manager = session.services["execution"]
    event = tool_call_event(session, "write", {"path": "new/file", "content": "x"})
    assert session._before_tool(event) is None
    assert session._before_tool_execute(event) is None
    manager.select_mode("read-only")
    assert isinstance(session._before_tool_execute(event), Block)
    manager.select_mode("workspace-edit")
    assert session._before_tool(event) is None
    (session.cwd / "new").symlink_to(tmp_path, target_is_directory=True)
    assert isinstance(session._before_tool_execute(event), Block)
    (session.cwd / "new").unlink()
    assert session._before_tool(event) is None
    event.args["path"] = str(tmp_path / "external")
    assert isinstance(session._before_tool_execute(event), Block)


def test_search_review_matches_actual_ignored_and_filtered_files(broker_session, tmp_path):
    session = broker_session(allow=False)
    hidden = session.cwd / ".venv"
    hidden.mkdir()
    (tmp_path / "outside").write_text("value OUTSIDE_SECRET")
    (hidden / "python").symlink_to(tmp_path / "outside")
    (session.cwd / "secret.txt").symlink_to(tmp_path / "outside")
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    (outside_directory / "hidden.py").write_text("value OUTSIDE_SECRET")
    (session.cwd / "linked-directory").symlink_to(outside_directory, target_is_directory=True)
    faux.script("test", faux.reply(tools=[
        ("grep", {"pattern": "value", "glob": "*.py"}),
        ("glob", {"pattern": "**/*.py"}),
        ("grep", {"pattern": "value"}),
    ]), faux.reply("done"))
    session.prompt("Search without following outside aliases")
    results = tool_results(session)
    assert not results[0].is_error and not results[1].is_error
    assert "linked-directory" not in results[1].text()
    assert results[2].is_error and results[2].details["boundary"]["dimension"] == "read_paths"
    assert "OUTSIDE_SECRET" not in repr(results)
    assert not session.ui.questions


def test_search_review_selects_the_same_files_as_the_glob_tools(broker_session, tmp_path):
    session = broker_session(allow=False)
    (session.cwd / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "outside").write_text("value OUTSIDE_SECRET")
    (session.cwd / "src" / "link.py").symlink_to(tmp_path / "outside")
    (session.cwd / "src" / "pkg" / "inside.py").write_text("value = 1")
    faux.script("test", faux.reply(tools=[
        ("glob", {"pattern": "src/**/*.py"}),
        ("grep", {"pattern": "value", "glob": "src/**/*.py"}),
        ("glob", {"pattern": "src/pkg/*.py"}),
        ("grep", {"pattern": "value", "glob": "src/*/*.py"}),
    ]), faux.reply("done"))
    session.prompt("Keep glob selection and content reads distinct")
    results = tool_results(session)
    assert not results[0].is_error
    assert {Path(line) for line in results[0].text().splitlines()} == {
        Path("src/link.py"), Path("src/pkg/inside.py"),
    }
    assert results[1].is_error
    assert not results[2].is_error
    assert {Path(line) for line in results[2].text().splitlines()} == {Path("src/pkg/inside.py")}
    assert not results[3].is_error and "value = 1" in results[3].text()
    assert "OUTSIDE_SECRET" not in repr(results)


def _external_tool(session, name, *, read_only):
    from virgo.agent.types import AgentTool, ToolResult

    tool = AgentTool(
        name,
        "External extension operation",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _args, _ctx: ToolResult.text("done"),
        side_effect="read" if read_only else "write",
    )
    session.register_tool(tool, roles=("main",), owner="test")
    return tool


def test_read_only_external_tools_need_no_approval_in_sandbox_modes(broker_session):
    ui = RecordingUI()
    session = broker_session(ui=ui)
    _external_tool(session, "external_read", read_only=True)
    event = tool_call_event(session, "external_read", {})
    assert session._before_tool(event) is None
    assert session._before_tool_execute(event) is None
    assert not ui.questions


def test_mutating_external_tools_require_session_approval(broker_session):
    ui = RecordingUI(answers=["y"])
    session = broker_session(ui=ui)
    _external_tool(session, "external_write", read_only=False)
    event = tool_call_event(session, "external_write", {"path": "result.txt"})
    event.signal = session.main.agent.signal
    assert session._before_tool(event) is None
    assert session._before_tool_execute(event) is None
    # Subsequent call in the same session reuses the session grant without re-prompting.
    assert session._before_tool_execute(event) is None
    assert len(ui.questions) == 1 and "External tool" in ui.questions[0]


def test_denied_external_tool_is_blocked_before_dispatch(broker_session):
    ui = RecordingUI(answers=["n"])
    session = broker_session(ui=ui)
    _external_tool(session, "external_write_denied", read_only=False)
    event = tool_call_event(session, "external_write_denied", {})
    event.signal = session.main.agent.signal
    assert session._before_tool(event) is None
    blocked = session._before_tool_execute(event)
    assert isinstance(blocked, Block) and "not approved" in blocked.reason


def test_no_limit_external_tools_skip_approval(make_session):
    ui = RecordingUI()
    session = make_session(ui=ui, settings={"execution": {"mode": "no-limit"}})
    _external_tool(session, "external_write_host", read_only=False)
    event = tool_call_event(session, "external_write_host", {})
    assert session._before_tool(event) is None
    assert session._before_tool_execute(event) is None
    assert not ui.questions
