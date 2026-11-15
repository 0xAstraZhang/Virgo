"""Sandbox acceptance scenarios, specified before implementing the launch path.

Failure cases: a missing backend runs the payload natively; accept-all or a
project setting disables isolation; a child/background job loses its owner;
indirect programs, symlinks or descendants write outside the workspace; Plan
widens filesystem rights; network/Unix sockets escape; credentials leak through
the environment; metadata can be rewritten; failures are replayed or mislabeled;
timeouts leave the normal command lifecycle broken. Native checks execute real
processes. Dedicated CI requires them instead of accepting capability skips.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import threading

import pytest

from virgo.agent import ToolContext
from virgo.ai.providers import faux
from virgo.harness.extensions import ExtensionAPI
from virgo.tools import sandbox

from tests.support.harness import tool_results
from tests.support.shell import selected


def python_command(source: str) -> str:
    return shlex.join([sys.executable, "-c", source])


def run(session, command: str, **args):
    shell = selected(session)
    faux.script("test", faux.reply(tools=[(shell.tool_name, {"command": command, **args})]),
                faux.reply("done"))
    session.prompt("Run the requested local check")
    return tool_results(session)[-1]


@pytest.fixture
def native(make_session):
    if sys.platform not in {"linux", "darwin"}:
        pytest.skip("native backends are Linux/macOS only")
    session = make_session(settings={"profile": "minimal", "sandbox": {"mode": "workspace-write"}},
                           refresh_models=False, extensions=False)
    result = run(session, "printf sandbox-ready")
    if result.is_error and result.details.get("status") == "sandbox_unavailable":
        if os.environ.get("VIRGO_REQUIRE_SANDBOX_TESTS") == "1":
            pytest.fail(result.text())
        pytest.skip(result.text())
    assert not result.is_error, result.text()
    assert "sandbox-ready" in result.text()
    return session


def test_missing_backend_is_fail_closed_in_real_session(make_session, project, monkeypatch):
    def missing():
        raise sandbox.SandboxUnavailable("backend deliberately unavailable")
    monkeypatch.setattr(sandbox, "platform_backend", missing)
    session = make_session(settings={"profile": "minimal", "sandbox": {"mode": "workspace-write"}},
                           refresh_models=False)
    shell = selected(session)
    command = "Set-Content escaped.txt bad" if shell.tool_name == "powershell" else "echo bad > escaped.txt"
    result = run(session, command)
    assert result.is_error and result.details["status"] == "sandbox_unavailable"
    assert not (project / "escaped.txt").exists()
    assert result.details["sandbox"]["mode"] == "workspace-write"


def test_project_cannot_disable_user_sandbox(make_session, project):
    home = project.parent / "home"
    (home / "settings.json").write_text(json.dumps({"sandbox": {"mode": "read-only"}}))
    (project / ".virgo").mkdir()
    (project / ".virgo" / "settings.json").write_text(json.dumps({"sandbox": {"mode": "disabled"}}))
    session = make_session(settings={"profile": "minimal"}, refresh_models=False)
    session.prompt("/sandbox")
    assert session.services["sandbox"].mode == "read-only"
    assert "ignored" in session.ui.text and "read-only" in session.ui.text
    faux.script("test", faux.reply(tools=[("write", {"path": "blocked.txt", "content": "no"})]),
                faux.reply("done"))
    session.prompt("Try a direct write with accept-all")
    assert not (project / "blocked.txt").exists() and tool_results(session)[-1].is_error


def test_workspace_file_tools_obey_sandbox_even_with_accept_all(make_session, project):
    session = make_session(settings={"profile": "minimal", "sandbox": {"mode": "workspace-write"}},
                           refresh_models=False)
    outside = project.parent / "outside.txt"
    faux.script("test", faux.reply(tools=[("write", {"path": str(outside), "content": "no"}),
                                         ("write", {"path": ".virgo/settings.json", "content": "{}"}),
                                         ("write", {"path": "allowed.txt", "content": "yes"})]),
                faux.reply("done"))
    session.prompt("Try paths under the same mandatory filesystem ceiling")
    assert not outside.exists() and not (project / ".virgo/settings.json").exists()
    assert (project / "allowed.txt").read_text() == "yes"
    assert [r.is_error for r in tool_results(session)] == [True, True, False]


def test_disabled_sandbox_keeps_legacy_execution(make_session, project):
    session = make_session(settings={"profile": "minimal"}, refresh_models=False)
    shell = selected(session)
    command = "Write-Output legacy" if shell.tool_name == "powershell" else "printf legacy"
    result = run(session, command)
    assert not result.is_error and "legacy" in result.text()
    session.prompt("/sandbox")
    assert "disabled" in session.ui.text


@pytest.mark.parametrize("config", [{"mode": "typo"}, {"network": "enabled"}, {"mode": None}, "off"])
def test_invalid_sandbox_configuration_fails_at_startup(make_session, config):
    with pytest.raises(ValueError, match="sandbox"):
        make_session(settings={"sandbox": config}, refresh_models=False)


def test_native_workspace_write_and_indirect_escape(native, project):
    outside = project.parent / "external.txt"
    outside.write_text("original")
    (project / "escape-link").symlink_to(outside)
    source = (
        "from pathlib import Path; import subprocess,sys; "
        "Path('local.txt').write_text('yes'); "
        "subprocess.run([sys.executable,'-c',\"from pathlib import Path; Path('escape-link').write_text('bad')\"],check=True)"
    )
    result = run(native, python_command(source))
    assert result.is_error
    assert (project / "local.txt").read_text() == "yes"
    assert outside.read_text() == "original"
    assert result.details["sandbox"]["mode"] == "workspace-write"
    # The first partial write must not cause automatic replay without isolation.
    assert len([m for m in native.main.messages if getattr(m, "tool_name", None) == selected(native).tool_name]) == 2


def test_native_readonly_including_child_shell(native, project):
    policy = sandbox.SandboxPolicy("read-only", project)
    from virgo.tools.bash import run_command
    out, code, status = run_command("sh -c 'echo bad > blocked.txt'", project, 10,
                                    threading.Event(), interpreter=selected(native), sandbox_policy=policy)
    assert code and status == "ok" and not (project / "blocked.txt").exists(), out.text
    native.services["permissions"].set_work_mode("plan")
    ctx = ToolContext("plan", project, threading.Event(), review_source=native.main.review_source)
    assert native.services["sandbox"].for_context(ctx).mode == "read-only"


def test_native_network_and_environment_isolation(native, monkeypatch):
    monkeypatch.setenv("VIRGO_TEST_SECRET", "must-not-reach-child")
    source = """import os, socket, json
result = {'secret': os.environ.get('VIRGO_TEST_SECRET'), 'denied': []}
for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            target = ('127.0.0.1', 9) if family == socket.AF_INET else (('::1', 9) if family == socket.AF_INET6 else '/tmp/virgo-no-socket')
            sock.connect(target)
    except PermissionError:
        result['denied'].append(int(family))
    except OSError as exc:
        result['denied'].append({'unexpected': exc.errno})
print(json.dumps(result))
"""
    result = run(native, python_command(source))
    assert not result.is_error, result.text()
    data = json.loads(result.text())
    assert data["secret"] is None and len(data["denied"]) == 3
    assert all(isinstance(item, int) for item in data["denied"]), data


def test_native_existing_metadata_is_readonly(native, project):
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("original")
    (project / "AGENTS.md").write_text("original")
    for name in (".git/config", "AGENTS.md"):
        result = run(native, python_command(f"from pathlib import Path; Path({name!r}).write_text('bad')"))
        assert result.is_error and (project / name).read_text() == "original"


def test_native_background_and_child_preserve_workspace(native, project):
    # Enable the existing background plugin in a separate assembled session,
    # then exercise the same sandbox-aware shell rather than a new executor.
    from virgo.harness.session import AgentSession

    from tests.support.harness import RecordingUI
    session = AgentSession(project, model="faux/test", session=None, permission_mode="accept-all",
                           settings={"profile": "minimal", "plugins": {"background": {"enabled": True}},
                                     "sandbox": {"mode": "workspace-write"}},
                           ui=RecordingUI(), refresh_models=False, extensions=False)
    try:
        result = run(session, "echo background > background.txt", background=True)
        assert not result.is_error, result.text()
        jobs = session.services["jobs"]
        job = next(iter(jobs.jobs.values()))
        assert job.done.wait(10) and job.status == "completed"
        assert (project / "background.txt").read_text().strip() == "background"
        api = ExtensionAPI(session, "sandbox-child")
        child = api.create_runtime("outside-child", "subagent", parent=session.main, cwd=project.parent)
        try:
            faux.script("test", faux.reply(tools=[(selected(session).tool_name, {"command": "echo bad > outside-child.txt"})]), faux.reply("done"))
            child.agent.prompt("Try to turn child cwd into a writable root")
            assert not (project.parent / "outside-child.txt").exists()
            assert any(getattr(m, "is_error", False) for m in child.messages)
        finally:
            api.release_runtime(child)
    finally:
        session.shutdown()


def test_native_failure_and_timeout_are_not_reclassified(native):
    result = run(native, "exit 7")
    assert result.is_error and result.details["exit_code"] == 7 and result.details["status"] == "ok"
    result = run(native, "sleep 10", timeout=0.2)
    assert result.is_error and result.details["status"] == "timeout"
    assert result.details["sandbox"]["mode"] == "workspace-write"
