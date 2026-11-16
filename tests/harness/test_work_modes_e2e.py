"""Work-mode acceptance boundaries, written before the implementation.

Failure cases: an explicit grant or approval change removes planning restrictions;
retired plan inputs silently change the approval strategy; a late hook evades the
execution checkpoint; descendants elevate authority; invalid modes partially
change state; minimal/localized hosts lose the Core command. These scenarios
exercise assembled sessions, the real tool loop and filesystem side effects.
"""
from __future__ import annotations

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.harness.extensions import ExtensionAPI

from tests.support.harness import calls, tool_results
from tests.support.shell import selected


def test_plan_work_mode_survives_approval_changes_and_restores_strategy(make_session, project):
    session = make_session(approval="ask", settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    policy = session.services["permissions"]
    session.prompt("/permissions never")
    assert session.approval_policy == "never"
    assert policy.work_mode == "plan" and policy.is_read_only(session.main)
    faux.script("test", faux.reply(tools=[("write", {"path": "answer.txt", "content": "blocked"})]),
                faux.reply("planning only"))
    session.prompt("Write answer.txt")
    assert not (project / "answer.txt").exists()
    assert tool_results(session)[-1].is_error
    assert not {"write", "edit"}.intersection(calls()[0]["tools"])

    session.prompt("/mode default")
    assert policy.work_mode == "default" and session.approval_policy == "never"
    faux.script("test", faux.reply(tools=[("write", {"path": "answer.txt", "content": "authorized"})]),
                faux.reply("done"))
    session.prompt("Now write answer.txt")
    assert (project / "answer.txt").read_text() == "authorized"
    assert not tool_results(session)[-1].is_error


def test_legacy_plan_command_preserves_the_selected_approval_strategy(make_session):
    """The retired approval-mode command must reject Plan without mutating intent."""
    session = make_session(approval="never", settings={"profile": "minimal"}, refresh_models=False)
    policy = session.services["permissions"]
    session.prompt("/permissions plan")
    assert policy.work_mode == "default" and session.approval_policy == "never"
    assert "ask" in session.ui.text and "never" in session.ui.text
    session.prompt("/mode plan")
    session.prompt("/permissions ask")
    assert policy.work_mode == "plan" and session.approval_policy == "ask"
    session.prompt("/mode default")
    assert policy.work_mode == "default" and session.approval_policy == "ask"
    assert not calls(), "Local mode commands must not invoke a model"


def test_legacy_startup_plan_cannot_be_exited_by_an_approval_command(make_session, project):
    """Legacy startup fails explicitly; the replacement keeps work intent independent."""
    with pytest.raises(ValueError, match=r"permissions\.mode.*retired"):
        make_session(settings={"profile": "minimal", "permissions": {"mode": "plan"}}, refresh_models=False)
    session = make_session(work_mode="plan", settings={"profile": "minimal"}, refresh_models=False)
    policy = session.services["permissions"]
    assert policy.work_mode == "plan"
    session.prompt("/permissions never")
    assert session.approval_policy == "never" and policy.work_mode == "plan"
    faux.script("test", faux.reply(tools=[("write", {"path": "legacy.txt", "content": "no"})]),
                faux.reply("done"))
    session.prompt("Attempt a write")
    assert not (project / "legacy.txt").exists() and tool_results(session)[-1].is_error
    session.prompt("/mode default")
    assert not policy.is_read_only(session.main) and session.approval_policy == "never"


def test_late_plan_transition_is_rechecked_before_file_and_shell_execution(make_session, project, python_command):
    """Busy mode changes are rejected; later idle Plan still blocks both executors."""
    session = make_session(settings={"profile": "minimal"}, refresh_models=False)
    policy = session.services["permissions"]
    rejected = []

    def enter_plan(event, _session):
        try:
            policy.set_work_mode("plan")
        except ValueError as error:
            rejected.append(str(error))

    ExtensionAPI(session, "mode-boundary").on("tool_call", enter_plan)
    faux.script("test", faux.reply(tools=[("write", {"path": "late.txt", "content": "authorized"})]),
                faux.reply("done"))
    session.prompt("The hook attempts to change mode after preflight")
    assert rejected and policy.work_mode == "default"
    assert (project / "late.txt").read_text() == "authorized" and not tool_results(session)[-1].is_error

    policy.set_work_mode("plan")
    faux.script("test", faux.reply(tools=[("write", {"path": "late.txt", "content": "no"})]),
                faux.reply("done"))
    session.prompt("An idle Plan transition blocks subsequent writes")
    assert (project / "late.txt").read_text() == "authorized" and tool_results(session)[-1].is_error
    shell = selected(session)
    command = python_command("from pathlib import Path; Path('shell.txt').write_text('no')", shell=shell)
    faux.script("test", faux.reply(tools=[(shell.tool_name, {"command": command})]), faux.reply("done"))
    session.prompt("The same boundary must protect the real command executor")
    assert not (project / "shell.txt").exists() and tool_results(session)[-1].is_error


def test_descendants_inherit_plan_without_losing_their_own_read_only_ceiling(make_session, project):
    session = make_session(settings={"profile": "minimal"}, refresh_models=False)
    policy = session.services["permissions"]
    api = ExtensionAPI(session, "work-mode-delegate")
    child = api.create_runtime("mode-child", "subagent", parent=session.main)
    restricted = api.create_runtime("readonly-child", "subagent", parent=child, read_only=True)
    try:
        policy.set_work_mode("plan")
        faux.script("test", faux.reply(tools=[("write", {"path": "child.txt", "content": "no"})]),
                    faux.reply("done"))
        child.agent.prompt("Attempt to write under a planning parent")
        assert not (project / "child.txt").exists()
        assert any(m.is_error for m in child.messages if isinstance(m, ToolResultMessage))
        assert policy.is_read_only(child) and policy.is_read_only(restricted)
        policy.set_work_mode("default")
        assert not policy.is_read_only(child)
        assert policy.is_read_only(restricted)
        assert policy.work_mode_for(restricted) == "default"
    finally:
        api.release_runtime(restricted)
        api.release_runtime(child)


def test_invalid_work_modes_leave_the_effective_policy_unchanged(make_session):
    session = make_session(settings={"profile": "minimal", "workMode": "plan"}, refresh_models=False)
    policy = session.services["permissions"]
    with pytest.raises(ValueError, match="workMode"):
        policy.set_work_mode("typo")
    session.prompt("/mode typo")
    assert policy.work_mode == "plan" and policy.is_read_only(session.main)
    assert session.approval_policy == "ask"
    assert not calls()
    with pytest.raises(ValueError, match="workMode"):
        make_session(settings={"profile": "minimal", "workMode": "typo"}, refresh_models=False)


@pytest.mark.parametrize("language, label", [("en", "Work mode"), ("zh-CN", "工作模式")])
def test_work_mode_command_is_localized_and_available_without_plugins(make_session, language, label):
    session = make_session(settings={"profile": "minimal", "language": language},
                           refresh_models=False, extensions=False)
    session.prompt("/mode plan")
    assert label in session.ui.text
    assert session.services["permissions"].work_mode == "plan"
    assert session.commands.get("mode").source == "core"
    assert not calls()
