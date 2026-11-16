"""Explicit authorization checks retain workspace, origin and execution boundaries."""
from __future__ import annotations

import pytest

from virgo.agent.types import Block, ToolCallEvent
from virgo.ai.providers import faux
from virgo.harness.permission_policy import rule_matches
from virgo.harness.permission_scope import command_approval_reason
from virgo.harness.permissions import APPROVAL_POLICIES, approval_policy_label, normalize_approval_policy

from tests.support.harness import RecordingUI, tool_call_event, tool_results


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
    session = make_session(ui=ui, allow=False)
    permissions = session.services["permissions"]
    args = {"path": str(tmp_path / "outside.txt"), "content": "one"}
    for _ in range(3):
        assert permissions.on_tool_call(tool_call_event(session, "write", args), session) is None
    assert len(ui.questions) == 2
    assert isinstance(permissions.on_tool_call(tool_call_event(session, "write", args | {"content": "two"}), session), Block)
    assert len(ui.questions) == 3


def test_plan_mode_only_allows_reads_and_dominates_agent_override(make_session):
    session = make_session(settings={"workMode": "plan"})
    permissions = session.services["permissions"]
    child = session.create_runtime("child", "subagent", parent=session.main, read_only=False)
    try:
        assert permissions.decide(tool_call_event(session, "read", {"path": "README.md"}))[0] == "allow"
        for name, args in (("write", {"path": "a.txt", "content": "x"}), ("bash", {"command": "git status"}),
                           ("powershell", {"command": "Get-Content file.txt"}), ("todo_write", {"items": []})):
            event = tool_call_event(session, name, args, agent="child")
            event.origin = child.origin
            assert permissions.decide(event)[0] == "deny", name
    finally:
        session.release_runtime(child)


def test_auto_review_preserves_explicit_allow_and_deny_rules(make_session):
    session = make_session(approval="never", settings={"permissions": {
        "allow": ["bash(curl https://example.test)", "write"], "deny": ["write(*)"],
    }})
    permissions = session.services["permissions"]
    assert permissions.decide(tool_call_event(session, "bash", {"command": "curl https://example.test"}))[0] == "allow"
    assert permissions.decide(tool_call_event(session, "write", {"path": "x", "content": ""}))[0] == "deny"
    assert permissions.decide(tool_call_event(session, "bash", {"command": "curl https://example.test; touch extra"}))[0] == "deny"


def test_unattended_review_fallback_is_denied(make_session, tmp_path):
    class Headless(RecordingUI):
        def confirm(self, question, agent="main"):
            return None

    session = make_session(ui=Headless(), allow=False)
    block = session.services["permissions"].on_tool_call(
        tool_call_event(session, "write", {"path": str(tmp_path / "x"), "content": ""}), session,
    )
    assert isinstance(block, Block) and "nobody is available" in block.reason


def test_review_and_human_approval_recheck_changed_mode(make_session, tmp_path, monkeypatch):
    session = make_session(allow=False)
    permissions = session.services["permissions"]
    event = tool_call_event(session, "write", {"path": str(tmp_path / "outside"), "content": "x"})

    def confirm(*_):
        permissions.set_work_mode("plan")
        return "y"

    monkeypatch.setattr(session.ui, "confirm", confirm)
    assert isinstance(permissions.on_tool_call(event, session), Block)
    assert permissions.is_read_only(session.main)


def test_execution_rechecks_mode_arguments_and_symlink_boundary(make_session, tmp_path):
    session = make_session(allow=False)
    permissions = session.services["permissions"]
    event = tool_call_event(session, "write", {"path": "new/file", "content": "x"})
    assert permissions.on_tool_call(event, session) is None
    assert permissions.before_execute(event) is None
    permissions.set_work_mode("plan")
    assert isinstance(permissions.before_execute(event), Block)
    permissions.set_work_mode("default")
    assert permissions.on_tool_call(event, session) is None
    (session.cwd / "new").symlink_to(tmp_path, target_is_directory=True)
    assert isinstance(permissions.before_execute(event), Block)
    (session.cwd / "new").unlink()
    assert permissions.on_tool_call(event, session) is None
    event.args["path"] = str(tmp_path / "external")
    assert isinstance(permissions.before_execute(event), Block)


def test_search_review_matches_actual_ignored_and_filtered_files(make_session, tmp_path):
    session = make_session(allow=False)
    hidden = session.cwd / ".venv"
    hidden.mkdir()
    (hidden / "python").symlink_to(tmp_path / "outside")
    (session.cwd / "secret.txt").symlink_to(tmp_path / "outside")
    permissions = session.services["permissions"]
    assert permissions.decide(tool_call_event(session, "grep", {"pattern": "value", "glob": "*.py"}))[0] == "allow"
    assert permissions.decide(tool_call_event(session, "glob", {"pattern": "**/*.py"}))[0] == "allow"
    decision, reason = permissions.decide(tool_call_event(session, "grep", {"pattern": "value"}))
    assert decision == "ask" and str(tmp_path / "outside") in reason


def test_search_review_selects_the_same_files_as_the_glob_tools(make_session, tmp_path):
    session = make_session(allow=False)
    (session.cwd / "src" / "pkg").mkdir(parents=True)
    (session.cwd / "src" / "link.py").symlink_to(tmp_path / "outside")
    permissions = session.services["permissions"]
    assert permissions.decide(tool_call_event(session, "glob", {"pattern": "src/**/*.py"}))[0] == "ask"
    assert permissions.decide(tool_call_event(session, "grep", {"pattern": "value", "glob": "src/**/*.py"}))[0] == "ask"
    assert permissions.decide(tool_call_event(session, "glob", {"pattern": "src/pkg/*.py"}))[0] == "allow"
    assert permissions.decide(tool_call_event(session, "grep", {"pattern": "value", "glob": "src/*/*.py"}))[0] == "allow"


@pytest.mark.parametrize("command", ["cat fixed link", "cd sub && cat ../fixed link", "git -C sub diff ../fixed link"])
def test_shell_session_approval_binds_all_resolved_path_targets(make_session, tmp_path, command):
    ui = RecordingUI(answers=["a", "n"])
    session = make_session(ui=ui, allow=False)
    (session.cwd / "sub").mkdir()
    (session.cwd / "fixed").symlink_to(tmp_path / "fixed-outside")
    link = session.cwd / ("sub/link" if command.startswith(("cd", "git")) else "link")
    link.symlink_to(tmp_path / "first-outside")
    permissions = session.services["permissions"]
    event = tool_call_event(session, "bash", {"command": command})
    assert permissions.on_tool_call(event, session) is None
    assert permissions.on_tool_call(tool_call_event(session, "bash", {"command": command}), session) is None
    assert len(ui.questions) == 1
    link.unlink()
    link.symlink_to(tmp_path / "second-outside")
    assert isinstance(permissions.before_execute(event), Block)
    assert len(ui.questions) == 2


def test_dynamic_shell_approval_is_once_and_inherited_cdpath_needs_review(make_session, monkeypatch):
    ui = RecordingUI(answers=["a", "n"])
    session = make_session(ui=ui, allow=False)
    permissions = session.services["permissions"]
    event = tool_call_event(session, "bash", {"command": "cat $TARGET"})
    assert permissions.on_tool_call(event, session) is None
    assert not permissions.session_approvals
    assert isinstance(permissions.on_tool_call(tool_call_event(session, "bash", event.args), session), Block)
    monkeypatch.setenv("CDPATH", "sub")
    assert command_approval_reason("cd name && cat file", session.cwd, session.cwd)


def test_rule_matching_paths(project):
    event = ToolCallEvent("1", "edit", {"path": "src/app/main.py"}, None, "main", project)
    assert rule_matches("edit(src/**)", event)
    assert rule_matches("edit", event)
    assert not rule_matches("write(src/**)", event)
    assert rule_matches("mcp__*", ToolCallEvent("1", "mcp__gh__issue", {}, None, "main", project))
