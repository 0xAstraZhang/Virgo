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
    with pytest.raises(ValueError, match="Project permissions"):
        AgentSession(project, model="faux/test", ui=RecordingUI(),
                     settings={"profile": "minimal", "workMode": "plan", "sandbox": {"mode": "disabled"}},
                     extensions=False, refresh_models=False)


@pytest.mark.parametrize("project_denies", [[], ["write(repo.txt)"]])
def test_project_deny_rules_cannot_erase_user_restrictions(project, project_denies):
    home = Path(os.environ["VIRGO_HOME"])
    (home / "settings.json").write_text(json.dumps({"permissions": {"deny": ["write(user.txt)"]}}))
    (project / ".virgo").mkdir()
    (project / ".virgo/settings.json").write_text(json.dumps({"permissions": {"deny": project_denies}}))
    with pytest.raises(ValueError, match="Project permissions"):
        AgentSession(project, model="faux/test", ui=RecordingUI(), extensions=False, refresh_models=False)


def test_never_rejects_unknown_operations_without_blocking_workspace_writes(make_session, project, python_command):
    ui = RecordingUI(answers=["y"])
    session = make_session(approval="never", allow=False, ui=ui,
                           settings={"execution": {"mode": "no-limit"}})
    shell = session.services["shell"].current
    command = python_command("from pathlib import Path; Path('forbidden.txt').touch()", shell=shell)
    faux.script("test", faux.reply(tools=[
        ("write", {"path": "allowed.txt", "content": "allowed"}),
        (shell.tool_name, {"command": command}),
    ]), faux.reply("done"))
    session.prompt("Try the selected operations")
    assert (project / "allowed.txt").read_text() == "allowed"
    assert (project / "forbidden.txt").exists()  # No-Limit authorizes managed operations.
    assert not ui.questions
    assert [result.is_error for result in tool_results(session)] == [False, False]


@pytest.mark.parametrize("permission", [{"allow": ["write"]}, {"approval": "never"}])
def test_project_cannot_select_authorization_grants(project, permission):
    (project / ".virgo").mkdir()
    (project / ".virgo/settings.json").write_text(json.dumps({"permissions": permission}))
    with pytest.raises(ValueError, match="Project permissions"):
        AgentSession(project, model="faux/test", extensions=False, refresh_models=False)


def test_hard_sandbox_denial_does_not_request_approval(broker_session, tmp_path):
    outside = tmp_path / "outside.txt"
    ui = RecordingUI(answers=["y"])
    session = broker_session(
        faux.reply(tools=[("write", {"path": str(outside), "content": "blocked"})]), faux.reply("done"),
        allow=False, ui=ui, preset="read-only", approval="never",
    )
    session.prompt("Try this write")
    assert not outside.exists()
    assert tool_results(session)[-1].is_error
    assert not ui.questions


def test_requested_config_cannot_pretend_host_shell_is_isolated(make_session, project, python_command):
    session = make_session(approval="never", allow=False)
    shell = session.services["shell"].current
    command = python_command("from pathlib import Path; Path('host-only.txt').touch()", shell=shell)
    faux.script("test", faux.reply(tools=[(shell.tool_name, {"command": command})]), faux.reply("done"))
    assert session.services["execution"].name == "host"
    session.settings.set("execution.mode", "read-only")
    session.prompt("Try the operation")
    assert (project / "host-only.txt").exists()
    assert not tool_results(session)[-1].is_error
    assert session.services["execution"].name == "host"
