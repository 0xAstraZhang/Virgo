"""Explicit authorization checks retain workspace, origin and execution boundaries."""
from __future__ import annotations

import threading

import pytest

from virgo.agent.types import Block, ToolCallEvent, ToolContext, ToolResult
from virgo.ai.providers import faux
from virgo.harness.permission_policy import rule_matches
from virgo.harness.permission_scope import command_approval_reason
from virgo.harness.permissions import APPROVAL_POLICIES, approval_policy_label, normalize_approval_policy

from tests.support.harness import RecordingUI, tool_call_event, tool_results


def request(session, tool, arguments, *, permissions=None, call_id="request"):
    context = ToolContext(call_id, session.cwd, threading.Event(), origin=session.main.origin)
    return session.tool("request_permissions").execute({
        "tool": tool, "arguments": arguments,
        "permissions": permissions or {"workspace_only": False},
        "reason": "Run this exact operation outside the default boundary",
    }, context)


def test_public_modes_and_legacy_config_aliases():
    assert APPROVAL_POLICIES == ("ask", "never")
    assert all(approval_policy_label(value) for value in APPROVAL_POLICIES)
    for retired in ("default", "accept-edits", "yolo", "auto-review", "accept-all", "plan", "unknown"):
        with pytest.raises(ValueError, match="approval policy"):
            normalize_approval_policy(retired)


def test_auto_review_executes_internal_changes_without_asking(make_session, project):
    # Retain workspace mutation coverage while retiring the model reviewer.
    ui = RecordingUI()
    session = make_session(
        faux.reply(tools=[("write", {"path": "a.txt", "content": "1"}), ("write", {"path": "b.txt", "content": "2"})]),
        faux.reply("done"), ui=ui, allow=False,
    )
    session.prompt("create the files")
    assert (project / "a.txt").read_text() == "1"
    assert (project / "b.txt").read_text() == "2"
    assert not ui.questions and all(not result.is_error for result in tool_results(session))
    assert {row["source"] for row in session.store.custom("permission_decision")} == {"workspace"}
    assert not session.store.custom("model_call") or all(row["purpose"] != "permission" for row in session.store.custom("model_call"))


def test_risky_review_respects_allow_once_and_exact_session_approval(make_session, tmp_path):
    ui = RecordingUI(answers=["y", "a", "n"])
    session = make_session(ui=ui, allow=False, settings={"sandbox": {"mode": "workspace-write"}})
    args = {"path": str(tmp_path / "outside.txt"), "content": "one"}
    changed = args | {"content": "two"}
    def ask(arguments):
        return ("request_permissions", {
            "tool": "write", "arguments": arguments, "permissions": {"workspace_only": False},
            "reason": "Write the explicitly selected external file once",
        })
    operations = [ask(args), ("write", args), ("write", args), ask(args),
                  ("write", changed), ("write", args), ask(changed), ("write", changed)]
    faux.script("test", *(faux.reply(tools=[operation]) for operation in operations), faux.reply("done"))
    session.prompt("Exercise exact single-operation permissions")
    results = tool_results(session)
    assert [result.is_error for result in results] == [False, False, True, False, True, False, True, True]
    assert len(ui.questions) == 3
    assert (tmp_path / "outside.txt").read_text() == "one"
    assert not session.services["permissions"].session_approvals
    assert session.services["sandbox"].mode_for(session.main).workspace_only


def test_plan_mode_only_allows_reads_and_dominates_agent_override(make_session):
    session = make_session(settings={"workMode": "plan"})
    permissions = session.services["permissions"]
    child = session.create_runtime("child", "subagent", parent=session.main, read_only=False)
    restricted = session.create_runtime("restricted", "subagent", parent=child, read_only=True)
    try:
        assert "Plan mode is active" in child.request_context([]).system
        assert not permissions.is_read_only(child) and permissions.is_read_only(restricted)
        assert {"read", "write", "edit", "request_permissions"}.issubset(
            tool.name for tool in child.request_context([]).tools
        )
        for runtime, expected in ((child, "allow"), (restricted, "deny")):
            event = tool_call_event(session, "write", {"path": "a.txt", "content": "x"}, agent=runtime.name)
            event.origin = runtime.origin
            assert permissions.decide(event)[0] == expected
        assert permissions.work_mode_for(child) == permissions.work_mode_for(restricted) == "plan"
    finally:
        session.release_runtime(restricted)
        session.release_runtime(child)


def test_auto_review_preserves_explicit_allow_and_deny_rules(make_session):
    session = make_session(approval="never", settings={"permissions": {
        "allow": ["bash(curl https://example.test)", "write"], "deny": ["write(*)", "bash(*touch extra*)"],
    }})
    permissions = session.services["permissions"]
    assert permissions.decide(tool_call_event(session, "bash", {"command": "curl https://example.test"}))[0] == "allow"
    assert permissions.decide(tool_call_event(session, "write", {"path": "x", "content": ""}))[0] == "deny"
    assert permissions.decide(tool_call_event(session, "bash", {"command": "curl https://example.test; touch extra"}))[0] == "deny"


def test_unattended_review_fallback_is_denied(make_session, tmp_path):
    class Headless(RecordingUI):
        def confirm(self, question, agent="main"):
            return None

    session = make_session(ui=Headless(), allow=False, settings={"sandbox": {"mode": "workspace-write"}})
    args = {"path": str(tmp_path / "x"), "content": ""}
    result = request(session, "write", args)
    assert result.is_error and "not granted" in result.content[0].text
    assert isinstance(session._before_tool(tool_call_event(session, "write", args)), Block)
    assert not (tmp_path / "x").exists()


def test_review_and_human_approval_recheck_changed_mode(make_session, tmp_path, monkeypatch):
    session = make_session(allow=False, settings={"sandbox": {"mode": "workspace-write"}})
    args = {"path": str(tmp_path / "outside"), "content": "x"}

    def confirm(*_):
        session.services["sandbox"].select_mode("read-only")
        return "y"

    monkeypatch.setattr(session.ui, "confirm", confirm)
    result = request(session, "write", args)
    assert result.is_error and "expired" in result.content[0].text
    assert session.services["permissions"].is_read_only(session.main)
    assert isinstance(session._before_tool(tool_call_event(session, "write", args)), Block)


def test_execution_rechecks_mode_arguments_and_symlink_boundary(make_session, tmp_path):
    session = make_session(allow=False, settings={"sandbox": {"mode": "workspace-write"}})
    manager = session.services["sandbox"]
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


def test_search_review_matches_actual_ignored_and_filtered_files(make_session, tmp_path):
    session = make_session(allow=False, settings={"sandbox": {"mode": "workspace-write"}})
    hidden = session.cwd / ".venv"
    hidden.mkdir()
    (tmp_path / "outside").write_text("value OUTSIDE_SECRET")
    (hidden / "python").symlink_to(tmp_path / "outside")
    (session.cwd / "secret.txt").symlink_to(tmp_path / "outside")
    faux.script("test", faux.reply(tools=[
        ("grep", {"pattern": "value", "glob": "*.py"}),
        ("glob", {"pattern": "**/*.py"}),
        ("grep", {"pattern": "value"}),
    ]), faux.reply("done"))
    session.prompt("Search without following outside aliases")
    results = tool_results(session)
    assert not results[0].is_error and not results[1].is_error
    assert results[2].is_error and results[2].details["sandbox"]["dimension"] == "workspace_only"
    assert "OUTSIDE_SECRET" not in repr(results)
    assert not session.ui.questions


def test_search_review_selects_the_same_files_as_the_glob_tools(make_session, tmp_path):
    session = make_session(allow=False, settings={"sandbox": {"mode": "workspace-write"}})
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
    assert not results[0].is_error and "src/link.py" in results[0].text()
    assert results[1].is_error
    assert not results[2].is_error and "src/pkg/inside.py" in results[2].text()
    assert not results[3].is_error and "value = 1" in results[3].text()
    assert "OUTSIDE_SECRET" not in repr(results)


@pytest.mark.parametrize("command", ["cat fixed link", "cd sub && cat ../fixed link", "git -C sub diff ../fixed link"])
def test_shell_session_approval_binds_all_resolved_path_targets(make_session, tmp_path, command):
    ui = RecordingUI(answers=["a"])
    session = make_session(ui=ui, allow=False, settings={"sandbox": {"mode": "workspace-write"}})
    (session.cwd / "sub").mkdir()
    (session.cwd / "fixed").symlink_to(tmp_path / "fixed-outside")
    link = session.cwd / ("sub/link" if command.startswith(("cd", "git")) else "link")
    link.symlink_to(tmp_path / "first-outside")
    args = {"command": command}
    assert not request(session, "bash", args, permissions={"workspace_only": False, "network_allowed": True}).is_error
    manager = session.services["sandbox"]
    event = tool_call_event(session, "bash", args)
    assert session._before_tool(event) is None
    assert not manager.mode_for_event(event).workspace_only
    link.unlink()
    link.symlink_to(tmp_path / "second-outside")
    # A changed target revokes the exception; native execution must use its
    # original workspace boundary, even though Shell schemas remain visible.
    session._before_tool_execute(event)
    assert manager.mode_for_event(event).workspace_only
    assert not session.services["permissions"].session_approvals
    assert len(ui.questions) == 1


def test_dynamic_shell_approval_is_once_and_inherited_cdpath_needs_review(make_session, monkeypatch):
    ui = RecordingUI(answers=["a", "n"])
    session = make_session(ui=ui, allow=False, settings={"sandbox": {"mode": "workspace-write"}})
    args = {"command": "cat $TARGET"}
    expanded = {"workspace_only": False, "network_allowed": True}
    assert not request(session, "bash", args, permissions=expanded).is_error
    event = tool_call_event(session, "bash", args)
    assert session._before_tool(event) is None
    assert not session.services["sandbox"].mode_for_event(event).workspace_only
    session._after_tool(event, ToolResult.text("observed completion"))
    next_event = tool_call_event(session, "bash", args)
    next_event.call_id = "next"
    assert session._before_tool(next_event) is None
    assert session.services["sandbox"].mode_for_event(next_event).workspace_only
    assert not session.services["permissions"].session_approvals
    assert request(session, "bash", args, permissions=expanded, call_id="denied").is_error
    monkeypatch.setenv("CDPATH", "sub")
    assert command_approval_reason("cd name && cat file", session.cwd, session.cwd)


def test_rule_matching_paths(project):
    event = ToolCallEvent("1", "edit", {"path": "src/app/main.py"}, None, "main", project)
    assert rule_matches("edit(src/**)", event)
    assert rule_matches("edit", event)
    assert not rule_matches("write(src/**)", event)
    assert rule_matches("mcp__*", ToolCallEvent("1", "mcp__gh__issue", {}, None, "main", project))
