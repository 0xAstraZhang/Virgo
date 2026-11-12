"""Real shell processes through slash commands, model requests, permissions and UIs.

Failure cases specified before implementation: missing/unsupported interpreters, invalid
selection, cancelled picker, failed persistence, stale paths, wrong quoting/encoding/cwd,
nonzero exits hidden by later output, timeout/abort, background and session cross-talk,
permission parser mismatch, stale model metadata after switching and restart overrides.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from textual.widgets import OptionList

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.cli.console import ConsoleBroker, TerminalUI
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.features.permission import ReviewDecision
from virgo.harness.session import Runtime

from .conftest import calls
from .test_tui_app import wait_for

# On Windows, bash.exe can be the WSL launcher rather than a local interpreter.
SHELLS = [name for name in ("bash", "zsh", "sh", "dash", "pwsh", "powershell")
          if shutil.which(name) and (os.name != "nt" or name in {"pwsh", "powershell"})]


def selected(session):
    return session.services["shell"].current


def result(session):
    return next(m for m in reversed(session.main.messages) if isinstance(m, ToolResultMessage))


def invoke(session, command, **kwargs):
    faux.push("test", faux.reply(tools=[(selected(session).tool_name, {"command": command, **kwargs})]), faux.reply("done"))
    session.prompt("Run the requested command")
    return result(session)



@pytest.mark.parametrize("name", SHELLS)
def test_select_execute_and_model_metadata_survive_restart(make_session, project, name):
    session = make_session()
    session.settings.save_user({"effort": "high", "search": {"timeout": 21}})
    session.prompt(f"/shell {name}")
    shell = selected(session)
    assert shell.kind == name
    assert Path(shell.executable).is_absolute()
    assert shell.version
    assert not session.main.messages
    assert not calls()
    cmd = "Write-Output \"中文 'quotes' `$literal\""
    if shell.tool_name != "powershell":
        cmd = 'printf "%s\\n" "中文 \'quotes\' \\$literal"'
    res = invoke(session, cmd)
    assert not res.is_error and "中文 'quotes' $literal" in res.text()
    request = calls()[0]
    assert shell.executable in request["system"] and shell.version in request["system"]
    assert f"Command tool: {shell.tool_name}" in request["system"]
    assert shell.tool_name in request["tools"]
    assert ("powershell" in request["tools"]) != ("bash" in request["tools"])
    assert calls()[0]["system"] == calls()[1]["system"]
    restored = make_session()
    assert selected(restored) == shell
    assert restored.settings.get("effort") == "high"
    assert restored.settings.get("search.timeout") == 21
    (project / "cwd-marker").write_text("correct working directory", encoding="utf-8")
    cwd_command = "Get-Content -LiteralPath cwd-marker" if shell.tool_name == "powershell" else "cat cwd-marker"
    assert "correct working directory" in invoke(restored, cwd_command).text()


def test_headless_discovery_and_invalid_choices_do_not_write(make_session):
    session = make_session()
    before = selected(session)
    session.prompt("/shell")
    assert "/shell" in session.ui.text and before.executable in session.ui.text
    for argument in ("unknown-shell", "99999", "/missing/shell", "bash --login"):
        session.prompt(f"/shell {argument}")
        assert selected(session) == before
    assert not (session.paths.user / "settings.json").exists()
    assert not calls()


def test_plain_picker_and_cancel_use_real_terminal_selection(make_session):
    session = make_session()
    ui = TerminalUI(ConsoleBroker())
    # Feed the existing numbered selector, keeping real discovery and application logic.
    ui.input = lambda *a, **kw: "1"
    ui.write = lambda *a, **kw: None
    session.ui = ui
    session.prompt("/shell")
    path = session.paths.user / "settings.json"
    saved = path.read_bytes()
    before = selected(session)
    ui.input = lambda *a, **kw: ""
    session.prompt("/shell")
    assert path.read_bytes() == saved and selected(session) == before


def test_fullscreen_picker_select_and_cancel(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(110, 30)) as pilot:
            app.controller.submit("/shell")
            await wait_for(pilot, lambda: app.input_request is not None, timeout=15)
            choices = app.query_one("#completions", OptionList)
            assert choices.option_count >= 1
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.controller.busy and app.input_request is None)
            saved = (session.paths.user / "settings.json").read_bytes()
            assert selected(session).executable in session.main.system
            app.controller.submit("/shell")
            await wait_for(pilot, lambda: app.input_request is not None, timeout=15)
            await pilot.press("escape")
            await wait_for(pilot, lambda: not app.controller.busy and app.input_request is None)
            assert (session.paths.user / "settings.json").read_bytes() == saved
            assert not session.main.messages and not calls()
    asyncio.run(scenario())


def test_failed_save_leaves_executable_and_prompt_unchanged(make_session, monkeypatch):
    session = make_session()
    before = selected(session), session.main.system, list(session.main.tools)
    def fail(_patch):
        raise OSError("settings are read-only")
    monkeypatch.setattr(session.settings, "save_user", fail)
    session.prompt(f"/shell {selected(session).executable}")
    assert (selected(session), session.main.system, session.main.tools) == before
    assert "read-only" in session.ui.text


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("zsh"), reason="requires bash and zsh")
def test_switch_refreshes_main_but_preserves_other_runtime_and_background(make_session):
    session = make_session()
    session.prompt("/shell bash")
    other = make_session(settings={"shell": {"executable": shutil.which("bash")}})
    old = Runtime(session, "child", "subagent")
    invoke(session, "sleep 0.2; printf '%s' \"$BASH_VERSION\"", background=True)
    job = next(iter(session.services["jobs"].jobs.values()))
    session.prompt("/shell zsh")
    res = invoke(session, 'printf "%s" "$ZSH_VERSION"')
    assert not res.is_error and res.text().strip() != "(no output)"
    assert selected(session).executable in calls()[-1]["system"]
    assert old.system != session.main.system
    assert selected(other).kind == "bash"
    assert session.ui.woken.wait(5)
    assert job.status == "completed" and session.services["jobs"].output(job).strip()
    faux.script("test", faux.reply(tools=[("bash", {"command": 'printf "%s" "$BASH_VERSION"'})]), faux.reply("done"))
    from virgo.ai.types import UserMessage
    old.agent.prompt([UserMessage("run")])
    assert next(m for m in old.messages if isinstance(m, ToolResultMessage)).text().strip() != "(no output)"


def test_explicit_path_with_spaces_and_stale_selection(make_session, tmp_path):
    if os.name == "nt" or not shutil.which("bash"):
        pytest.skip("POSIX executable symlink")
    folder = tmp_path / "shell 中文 path"
    folder.mkdir()
    exe = folder / "bash"
    exe.symlink_to(shutil.which("bash"))
    session = make_session()
    session.prompt(f'/shell "{exe}"')
    assert selected(session).executable == str(exe)
    assert not invoke(session, "echo ok").is_error
    exe.unlink()
    restarted = make_session()
    assert selected(restarted) is None
    assert "unavailable" in restarted.ui.text.lower()
    assert "bash" not in [t.name for t in restarted.main.tools]
    restarted.prompt("/shell bash")
    assert selected(restarted) is not None


def test_environment_and_project_precedence_on_restart(make_session, project, monkeypatch):
    name = SHELLS[0]
    config = project / ".virgo" / "settings.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({"shell": {"executable": "/missing/project-shell"}}))
    monkeypatch.setenv("VIRGO_SHELL", shutil.which(name))
    session = make_session()
    assert selected(session).kind == name
    session.prompt(f"/shell {name}")
    assert "precedence" in session.ui.text
    monkeypatch.delenv("VIRGO_SHELL")
    restarted = make_session()
    assert selected(restarted) is None
    restarted.prompt("/shell auto")
    assert selected(restarted) is not None


@pytest.mark.parametrize("name", SHELLS)
def test_error_timeout_and_truncation_through_agent_loop(make_session, name):
    session = make_session()
    session.prompt(f"/shell {name}")
    shell = selected(session)
    failed = invoke(session, "exit 7")
    assert failed.is_error and failed.details["exit_code"] == 7
    command = "Start-Sleep -Seconds 5" if shell.tool_name == "powershell" else "sleep 5"
    timed = invoke(session, command, timeout=0.2)
    assert timed.is_error and timed.details["status"] == "timeout"
    command = "1..2100 | ForEach-Object { 'line' }" if shell.tool_name == "powershell" else "i=0; while [ $i -lt 2100 ]; do echo line; i=$((i+1)); done"
    truncated = invoke(session, command)
    assert "truncated" in truncated.text().lower()
    assert list((session.cwd / ".virgo" / "outputs").glob("*.txt"))


@pytest.mark.parametrize("name", [n for n in SHELLS if n in ("pwsh", "powershell")])
def test_powershell_failure_and_permission_boundaries(make_session, monkeypatch, name):
    session = make_session()
    session.prompt(f"/shell {name}")
    res = invoke(session, "Write-Error 'failure'; Set-Content -LiteralPath unexpected -Value bad")
    assert res.is_error and not (session.cwd / "unexpected").exists()
    reviews = []
    def deny(s, ev, reason):
        reviews.append((ev.tool_name, ev.args, reason))
        return ReviewDecision("deny", "test denial")
    monkeypatch.setattr("virgo.features.permission.review_tool_call", deny)
    session.permission_mode = "auto-review"
    res = invoke(session, "Set-Content -LiteralPath unexpected -Value bad")
    assert res.is_error and reviews and not (session.cwd / "unexpected").exists()
    session.permission_mode = "plan"
    assert "powershell" not in [t.name for t in session.main.request_context([]).tools]
    res = invoke(session, "Set-Content -LiteralPath unexpected -Value bad")
    assert res.is_error and not (session.cwd / "unexpected").exists()
    session.permission_mode = "auto-review"
    session.settings.set("permissions.deny", ["powershell(Write-Output*)"])
    assert invoke(session, "Write-Output denied").is_error


@pytest.mark.parametrize("name", [n for n in SHELLS if n in ("pwsh", "powershell")])
def test_powershell_native_exit_and_background(make_session, name):
    import sys
    exe = sys.executable.replace("'", "''")
    session = make_session()
    session.prompt(f"/shell {name}")
    assert invoke(session, f"& '{exe}' -c 'import sys; sys.exit(9)'").is_error
    invoke(session, "Start-Sleep -Milliseconds 100; Write-Output '后台完成'", background=True)
    job = next(iter(session.services["jobs"].jobs.values()))
    assert session.ui.woken.wait(8)
    assert job.status == "completed" and "后台完成" in session.services["jobs"].output(job)


def test_busy_switch_is_rejected_without_changing_settings(make_session):
    session = make_session()
    shell = selected(session)
    session.busy = True
    try:
        session.prompt(f"/shell {shell.executable}")
    finally:
        session.busy = False
    assert selected(session) == shell
    assert not (session.paths.user / "settings.json").exists()
    assert "running" in session.ui.text.lower()


@pytest.mark.parametrize("name", SHELLS)
def test_background_kill_stops_descendant_processes(make_session, name):
    import base64
    import shlex
    import sys
    import time

    session = make_session()
    session.prompt(f"/shell {name}")
    shell = selected(session)
    # A real child writes a delayed sentinel if killing only the interpreter leaves it behind.
    child = "import time; from pathlib import Path; time.sleep(2); Path('orphan').write_text('alive')"
    parent = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "Path('started').write_text(str(p.pid)); time.sleep(30)"
    )
    payload = base64.b64encode(parent.encode()).decode()
    code = f"import base64;exec(base64.b64decode('{payload}'))"
    if shell.tool_name == "powershell":
        command = "& '" + sys.executable.replace("'", "''") + "' -c '" + code.replace("'", "''") + "'"
    else:
        command = shlex.join([sys.executable, "-c", code])
    invoke(session, command, background=True)
    deadline = time.monotonic() + 10
    while not (session.cwd / "started").exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    job = next(iter(session.services["jobs"].jobs.values()))
    faux.push("test", faux.reply(tools=[("job_kill", {"id": job.id})]), faux.reply("stopped"))
    session.prompt("Stop the job")
    deadline = time.monotonic() + 5
    while not job.reported and time.monotonic() < deadline:
        time.sleep(0.02)
    assert job.reported, (job.status, result(session).text())
    assert job.status == "killed"
    time.sleep(2.2)
    assert not (session.cwd / "orphan").exists()


@pytest.mark.parametrize("name", [n for n in SHELLS if n == "pwsh"])
def test_modern_powershell_native_failure_stops_following_write(make_session, name):
    import sys
    session = make_session()
    session.prompt(f"/shell {name}")
    if not selected(session).native_errors:
        pytest.skip("native fail-fast requires 7.4+")
    exe = sys.executable.replace("'", "''")
    res = invoke(session, f"& '{exe}' -c 'import sys; sys.exit(9)'; Set-Content unexpected bad")
    assert res.is_error and not (session.cwd / "unexpected").exists()


@pytest.mark.parametrize("name", SHELLS)
def test_foreground_abort_uses_bound_shell_and_rejects_midrun_switch(make_session, name):
    import threading
    import time

    session = make_session()
    session.prompt(f"/shell {name}")
    shell = selected(session)
    command = ("Set-Content -LiteralPath started -Value ready; Start-Sleep -Seconds 20; Set-Content unexpected bad"
               if shell.tool_name == "powershell" else "echo ready > started; sleep 20; echo bad > unexpected")
    worker = threading.Thread(target=lambda: invoke(session, command), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not (session.cwd / "started").exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        session.prompt("/shell auto")
        assert selected(session) == shell and "running" in session.ui.text
    finally:
        session.abort()
        worker.join(timeout=8)
    assert not worker.is_alive()
    assert result(session).details["status"] == "aborted"
    assert not (session.cwd / "unexpected").exists()
