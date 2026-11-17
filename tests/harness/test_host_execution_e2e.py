"""Default Host execution and explicit isolation migration, through real sessions."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from virgo.ai.providers import faux
from virgo.harness.session import AgentSession

from tests.support.harness import RecordingUI, tool_results


def test_fresh_host_executes_without_importing_isolation(project, tmp_path):
    source = r'''
import importlib.abc, json, pathlib, sys
class NoIsolation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("virgo.") and any(word in fullname for word in ("sandbox", "mxc", "macos_process")):
            raise AssertionError("Host imported isolation: " + fullname)
sys.meta_path.insert(0, NoIsolation())
from virgo.harness.session import AgentSession
from virgo.ai.providers import faux
from tests.support.harness import RecordingUI, tool_results
workspace, home = map(pathlib.Path, sys.argv[1:])
ui = RecordingUI()
session = AgentSession(workspace, home=home, model="faux/host", session=None,
                       ui=ui, refresh_models=False)
shell = session.services["shell"].current
command = "Write-Output ('host-' + 'ok')" if shell.tool_name == "powershell" else "for x in host-ok; do printf '%s' \"$x\"; done"
faux.script("host", faux.reply(tools=[("write", {"path": "../outside.txt", "content": "host"})]),
            faux.reply(tools=[("read", {"path": "../outside.txt"})]),
            faux.reply(tools=[(shell.tool_name, {"command": command})]),
            faux.reply(tools=[("agent", {"description": "Create child file", "prompt": "Write child.txt"})]),
            faux.reply(tools=[("write", {"path": "child.txt", "content": "child-host"})]),
            faux.reply("child done"), faux.reply("done"))
try:
    session.prompt("Complete the file, shell and child operations")
    assert len(tool_results(session)) == 4
    assert not any(result.is_error for result in tool_results(session))
    assert not ui.questions
    assert (workspace / "child.txt").read_text() == "child-host"
    assert "request_permissions" not in [tool.name for tool in session.main.tools]
    session.prompt("/permissions show")
    assert "Host" in ui.text
    assert not any(entry.get("kind") == "permission_decision" for entry in session.store.entries)
    print(json.dumps({"provider": "host", "approvals": len(ui.questions)}))
finally:
    session.shutdown()
'''
    result = subprocess.run([sys.executable, "-B", "-c", source, str(project), str(tmp_path / "fresh-home")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"provider": "host", "approvals": 0}
    assert (project.parent / "outside.txt").read_text() == "host"


@pytest.mark.parametrize("config", [
    {"sandbox": {"mode": "workspace-write"}},
    {"permissions": {"preset": "read-only"}},
    {"permissions": {"allow": ["*"]}},
    {"permissions": {"deny": ["bash(git push*)"]}},
])
def test_explicit_legacy_authority_never_silently_becomes_host(project, config):
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
