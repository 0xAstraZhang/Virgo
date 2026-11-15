"""Work-mode acceptance boundaries, written before the implementation.

Failure cases: accept-all or an approval change removes planning restrictions;
legacy plan inputs discard the prior approval strategy; a late hook evades the
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
    session = make_session(mode="auto-review", settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    policy = session.services["permissions"]
    session.prompt("/permissions accept-all")
    assert session.permission_mode == "accept-all"
    assert policy.work_mode == "plan" and policy.mode_for("main") == "plan"
    faux.script("test", faux.reply(tools=[("write", {"path": "answer.txt", "content": "blocked"})]),
                faux.reply("planning only"))
    session.prompt("Write answer.txt")
    assert not (project / "answer.txt").exists()
    assert tool_results(session)[-1].is_error
    assert not {"write", "edit"}.intersection(calls()[0]["tools"])

    session.prompt("/mode default")
    assert policy.work_mode == "default" and session.permission_mode == "accept-all"
    faux.script("test", faux.reply(tools=[("write", {"path": "answer.txt", "content": "authorized"})]),
                faux.reply("done"))
    session.prompt("Now write answer.txt")
    assert (project / "answer.txt").read_text() == "authorized"
    assert not tool_results(session)[-1].is_error


def test_legacy_plan_command_preserves_the_selected_approval_strategy(make_session):
    session = make_session(mode="accept-all", settings={"profile": "minimal"}, refresh_models=False)
    policy = session.services["permissions"]
    session.prompt("/permissions plan")
    assert policy.work_mode == "plan" and session.permission_mode == "accept-all"
    session.prompt("/permissions auto-review")
    assert policy.work_mode == "plan" and session.permission_mode == "auto-review"
    session.prompt("/mode default")
    assert policy.work_mode == "default" and session.permission_mode == "auto-review"
    assert not calls(), "Local mode commands must not invoke a model"


def test_legacy_startup_plan_cannot_be_exited_by_an_approval_command(make_session, project):
    session = make_session(mode="plan", settings={"profile": "minimal"}, refresh_models=False)
    policy = session.services["permissions"]
    assert policy.work_mode == "plan"
    session.prompt("/permissions accept-all")
    assert session.permission_mode == "accept-all" and policy.work_mode == "plan"
    faux.script("test", faux.reply(tools=[("write", {"path": "legacy.txt", "content": "no"})]),
                faux.reply("done"))
    session.prompt("Attempt a write")
    assert not (project / "legacy.txt").exists() and tool_results(session)[-1].is_error
    session.prompt("/mode default")
    assert policy.mode_for("main") == "accept-all"


def test_late_plan_transition_is_rechecked_before_file_and_shell_execution(make_session, project, python_command):
    session = make_session(settings={"profile": "minimal"}, refresh_models=False)
    policy = session.services["permissions"]

    def enter_plan(event, _session):
        policy.set_work_mode("plan")
        session.permission_mode = "accept-all"

    ExtensionAPI(session, "mode-boundary").on("tool_call", enter_plan)
    faux.script("test", faux.reply(tools=[("write", {"path": "late.txt", "content": "no"})]),
                faux.reply("done"))
    session.prompt("The hook changes mode after preflight")
    assert not (project / "late.txt").exists() and tool_results(session)[-1].is_error
    assert policy.work_mode == "plan"

    policy.set_work_mode("default")
    shell = selected(session)
    command = python_command("from pathlib import Path; Path('shell.txt').write_text('no')", shell=shell)
    faux.script("test", faux.reply(tools=[(shell.tool_name, {"command": command})]), faux.reply("done"))
    session.prompt("The same boundary must protect the real command executor")
    assert not (project / "shell.txt").exists() and tool_results(session)[-1].is_error


def test_descendants_inherit_plan_without_losing_their_own_read_only_ceiling(make_session, project):
    session = make_session(settings={"profile": "minimal"}, refresh_models=False)
    policy = session.services["permissions"]
    api = ExtensionAPI(session, "work-mode-delegate")
    child = api.create_runtime("mode-child", "subagent", parent=session.main, permission="accept-all")
    restricted = api.create_runtime("readonly-child", "subagent", parent=child, permission="plan")
    try:
        policy.set_work_mode("plan")
        faux.script("test", faux.reply(tools=[("write", {"path": "child.txt", "content": "no"})]),
                    faux.reply("done"))
        child.agent.prompt("Attempt to write under a planning parent")
        assert not (project / "child.txt").exists()
        assert any(m.is_error for m in child.messages if isinstance(m, ToolResultMessage))
        assert policy.mode_for(child.name) == policy.mode_for(restricted.name) == "plan"
        policy.set_work_mode("default")
        assert policy.mode_for(child.name) == "accept-all"
        assert policy.mode_for(restricted.name) == "plan"
    finally:
        api.release_runtime(restricted)
        api.release_runtime(child)


def test_invalid_work_modes_leave_the_effective_policy_unchanged(make_session):
    session = make_session(settings={"profile": "minimal", "workMode": "plan"}, refresh_models=False)
    policy = session.services["permissions"]
    with pytest.raises(ValueError, match="workMode"):
        policy.set_work_mode("typo")
    session.prompt("/mode typo")
    assert policy.work_mode == "plan" and policy.mode_for("main") == "plan"
    assert session.permission_mode == "accept-all"
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
