"""Default Host execution and explicit isolation migration, through real sessions."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from virgo.ai.providers import faux
from virgo.execution import ExecutionUnavailable
from virgo.harness.session import AgentSession

from tests.support.harness import RecordingUI, tool_results


def test_host_executes_when_native_backend_is_unavailable(project, tmp_path, monkeypatch):
    """Explicit No-Limit completes file, shell, and child tasks without probing native backends."""
    import virgo.harness.session as hs
    import virgo.harness.settings as hsettings
    import virgo.sandbox.native as sn
    import virgo.sandbox.platform as sp

    def unavailable():
        raise ExecutionUnavailable("native sandbox backend unavailable on host", reason="backend_unavailable")

    monkeypatch.setattr(sp, "platform_backend", unavailable)
    monkeypatch.setattr(sn, "platform_backend", unavailable)
    monkeypatch.setattr(hsettings, "default_execution_mode", lambda: pytest.fail("explicit no-limit must not probe default mode"))
    monkeypatch.setattr(hs, "is_windows_sandbox_supported", lambda: pytest.fail("explicit no-limit must not probe Windows sandbox"))

    ui = RecordingUI()
    session = AgentSession(
        project, home=tmp_path / "fresh-home", model="faux/host", session=None,
        ui=ui, settings={"execution": {"mode": "no-limit"}}, refresh_models=False,
    )
    shell = session.services["shell"].current
    command = (
        "Write-Output ('host-' + 'ok')"
        if shell.tool_name == "powershell"
        else "for x in host-ok; do printf '%s' \"$x\"; done"
    )
    faux.script(
        "host",
        faux.reply(tools=[("write", {"path": "../outside.txt", "content": "host"})]),
        faux.reply(tools=[("read", {"path": "../outside.txt"})]),
        faux.reply(tools=[(shell.tool_name, {"command": command})]),
        faux.reply(tools=[("agent", {"description": "Create child file", "prompt": "Write child.txt"})]),
        faux.reply(tools=[("write", {"path": "child.txt", "content": "child-host"})]),
        faux.reply("child done"),
        faux.reply("done"),
    )
    try:
        session.prompt("Complete the file, shell and child operations")
        assert len(tool_results(session)) == 4
        assert not any(result.is_error for result in tool_results(session))
        assert not ui.questions
        assert (project.parent / "outside.txt").read_text() == "host"
        assert (project / "child.txt").read_text() == "child-host"
        assert "request_permissions" not in [tool.name for tool in session.main.tools]
        session.prompt("/permissions show")
        assert "Host" in ui.text
    finally:
        session.shutdown()


@pytest.mark.parametrize("config", [
    {"sandbox": {"mode": "workspace-write"}},
    {"permissions": {"preset": "read-only"}},
    {"permissions": {"allow": ["*"]}},
    {"permissions": {"deny": ["bash(git push*)"]}},
])
def test_explicit_legacy_authority_never_silently_becomes_host(project, config):
    from virgo.harness.settings import Paths, Settings

    settings = Settings.load(Paths(project))
    for rule in ('allow', 'deny'):
        if rule in config.get('permissions', {}):
            with pytest.raises(ValueError, match='migrat|retired'):
                settings.set(f'permissions.{rule}', config['permissions'][rule])
            assert not settings.get(f'permissions.{rule}')
    home = Path(os.environ["VIRGO_HOME"])
    (home / "settings.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="migrat|retired|execution.provider"):
        AgentSession(project, model="faux/test", session=None, refresh_models=False)


def test_removed_child_read_only_is_rejected_before_dispatch(project):
    faux.script("test", faux.reply(tools=[("agent", {"description": "Inspect", "prompt": "inspect",
                                                     "read_only": True})]), faux.reply("done"))
    session = AgentSession(project, model="faux/test", ui=RecordingUI(), session=None, refresh_models=False)
    try:
        session.prompt("Inspect")
        assert tool_results(session)[0].is_error
        assert not session._child_runtimes
    finally:
        session.shutdown()
