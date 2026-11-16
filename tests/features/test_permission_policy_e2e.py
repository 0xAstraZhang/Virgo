"""Repository configuration must never silently expand host authorization."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from virgo.ai.providers import faux
from virgo.harness.session import AgentSession

from tests.support.harness import RecordingUI, tool_results


def test_project_cannot_turn_user_deny_into_accept_all(project):
    home = Path(os.environ["VIRGO_HOME"])
    (home / "settings.json").write_text(json.dumps({"permissions": {"deny": ["write"]}}))
    (project / ".virgo").mkdir()
    (project / ".virgo/settings.json").write_text(json.dumps({"permissions": {"mode": "accept-all"}}))
    try:
        session = AgentSession(project, model="faux/test", ui=RecordingUI(),
                               settings={"profile": "minimal", "sandbox": {"mode": "disabled"}},
                               extensions=False, refresh_models=False)
    except ValueError as error:
        assert "permissions.mode" in str(error) or "Project permissions" in str(error)
        return
    try:
        faux.script("test", faux.reply(tools=[("write", {"path": "protected.txt", "content": "marker"})]), faux.reply("done"))
        session.prompt("Try a write")
        assert tool_results(session)[-1].is_error
        assert not (project / "protected.txt").exists()
    finally:
        session.shutdown()


def test_project_cannot_mark_a_mutating_command_read_only(project):
    (project / ".virgo").mkdir()
    (project / ".virgo/settings.json").write_text(json.dumps({"permissions": {"readOnlyCommands": ["touch"]}}))
    with pytest.raises(ValueError, match="readOnlyCommands"):
        AgentSession(project, model="faux/test", ui=RecordingUI(),
                     settings={"profile": "minimal", "workMode": "plan", "sandbox": {"mode": "disabled"}},
                     extensions=False, refresh_models=False)


@pytest.mark.parametrize("project_denies", [[], ["write(repo.txt)"]])
def test_project_deny_rules_cannot_erase_user_restrictions(project, project_denies):
    home = Path(os.environ["VIRGO_HOME"])
    (home / "settings.json").write_text(json.dumps({"permissions": {"deny": ["write(user.txt)"]}}))
    (project / ".virgo").mkdir()
    (project / ".virgo/settings.json").write_text(json.dumps({"permissions": {"deny": project_denies}}))
    session = AgentSession(project, model="faux/test", ui=RecordingUI(),
                           settings={"profile": "minimal", "sandbox": {"mode": "disabled"},
                                     "permissions": {"allow": ["write"]}},
                           extensions=False, refresh_models=False)
    try:
        faux.script("test", faux.reply(tools=[
            ("write", {"path": "user.txt", "content": "user denial"}),
            ("write", {"path": "repo.txt", "content": "project denial"}),
            ("write", {"path": "allowed.txt", "content": "allowed"}),
        ]), faux.reply("done"))
        session.prompt("Write these files")
        assert not (project / "user.txt").exists()
        assert (project / "repo.txt").exists() == (not project_denies)
        assert (project / "allowed.txt").read_text() == "allowed"
    finally:
        session.shutdown()


def test_never_rejects_unknown_operations_without_blocking_workspace_writes(make_session, project):
    ui = RecordingUI(answers=["y"])
    session = make_session(faux.reply(tools=[
        ("write", {"path": "allowed.txt", "content": "allowed"}),
        ("bash", {"command": "touch forbidden.txt"}),
    ]), faux.reply("done"), approval="never", allow=False, ui=ui)
    session.prompt("Try the selected operations")
    assert (project / "allowed.txt").read_text() == "allowed"
    assert (project / "forbidden.txt").exists()  # No-Limit authorizes managed operations.
    assert not ui.questions
    assert [result.is_error for result in tool_results(session)] == [False, False]


@pytest.mark.parametrize("permission", [{"allow": ["write"]}, {"approval": "never"}])
def test_project_cannot_select_authorization_grants(project, permission):
    (project / ".virgo").mkdir()
    (project / ".virgo/settings.json").write_text(json.dumps({"permissions": permission}))
    with pytest.raises(ValueError, match="Project permissions may only add deny"):
        AgentSession(project, model="faux/test", extensions=False, refresh_models=False)


def test_hard_sandbox_denial_does_not_request_approval(make_session, tmp_path):
    outside = tmp_path / "outside.txt"
    ui = RecordingUI(answers=["y"])
    session = make_session(
        faux.reply(tools=[("write", {"path": str(outside), "content": "blocked"})]), faux.reply("done"),
        allow=False, ui=ui, settings={"sandbox": {"mode": "read-only"}},
    )
    session.prompt("Try this write")
    assert not outside.exists()
    assert tool_results(session)[-1].is_error
    assert not ui.questions


def test_requested_config_cannot_pretend_host_shell_is_isolated(make_session, project):
    session = make_session(
        faux.reply(tools=[("bash", {"command": "touch host-only.txt"})]), faux.reply("done"),
        approval="never", allow=False,
    )
    assert session.services["sandbox"].mode == "disabled"
    session.settings.set("sandbox.mode", "workspace-write")
    session.prompt("Try the operation")
    assert (project / "host-only.txt").exists()
    assert not tool_results(session)[-1].is_error
    assert session.services["sandbox"].mode_for(session.main).unrestricted
