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
import socket
import sys
import tempfile
import threading
from pathlib import Path

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
    # Use a visible read-only host mount, not a hidden path in private /tmp.
    with tempfile.TemporaryDirectory(prefix="virgo-boundary-", dir=sys.prefix) as directory:
        outside = Path(directory) / "external.txt"
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
            target = ('127.0.0.1', 9) if family == socket.AF_INET else (('::1', 9) if family == socket.AF_INET6 else UNIX_TARGET)
            sock.connect(target)
    except PermissionError:
        result['denied'].append(int(family))
    except OSError as exc:
        result['denied'].append({'unexpected': exc.errno})
print(json.dumps(result))
"""
    # A missing Unix endpoint returns ENOENT before Seatbelt checks connect.
    # Bind a real host endpoint so this existing scenario measures denial.
    with tempfile.TemporaryDirectory(prefix="virgo-net-", dir="/tmp") as directory:
        endpoint = directory + "/socket"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(endpoint)
            listener.listen(1)
            result = run(native, python_command(source.replace("UNIX_TARGET", repr(endpoint))))
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


@pytest.mark.parametrize("background", [False, True])
def test_sandbox_rejects_changed_cwd_before_creating_output(make_session, project, background):
    """A late cwd change must not create host directories or logs outside the owner.

    Failure modes: foreground spooling creates an external log before rejection;
    background start failure removes its log but leaves external directories;
    changed cwd replaces the originating workspace; a failed launch is replayed.
    """
    outside = project.parent / "outside-cwd"
    outside.mkdir()
    session = make_session(
        settings={"profile": "minimal", "plugins": {"background": {"enabled": True}},
                  "sandbox": {"mode": "workspace-write"}},
        refresh_models=False, extensions=False,
    )

    def change_cwd(event, _session):
        event.cwd = outside

    ExtensionAPI(session, "sandbox-cwd-boundary").on("tool_call", change_cwd)
    command = ("Set-Content escaped.txt bad" if selected(session).tool_name == "powershell"
               else "echo bad > escaped.txt")
    result = run(session, command, background=background)
    assert result.is_error and result.details["status"] == "sandbox_unavailable"
    assert result.details["sandbox"]["workspace"] == str(project.resolve())
    assert not (outside / "escaped.txt").exists()
    assert not (outside / ".virgo").exists(), "Rejected cwd must not receive broker output files"
    if background:
        assert not session.services["jobs"].jobs


@pytest.mark.parametrize("finish", ["exit", "timeout", "cancel"])
def test_native_detached_descendant_is_reaped_with_command(native, project, finish):
    """Command completion must not leave a detached process with workspace rights.

    Failure modes: a descendant starts a new session/process group and survives
    normal completion, timeout or cancellation; cleanup removes private scratch
    but leaves the child able to write later; timeout/cancellation is mislabeled.
    Native policy may deny detachment, which also satisfies containment. A child
    that is allowed to detach exits by itself after its bounded delayed write.
    """
    import time

    ready = project / "detached-ready"
    denied = project / "detachment-denied"
    late = project / "detached-late-write"
    scratch_file = project / "command-scratch-path"
    child_source = (
        "import time; from pathlib import Path; "
        "Path('detached-ready').write_text('ready'); "
        "time.sleep(4); Path('detached-late-write').write_text('survived')"
    )
    source = (
        "import os,subprocess,sys,time\nfrom pathlib import Path\n"
        "Path('command-scratch-path').write_text(os.environ['TMPDIR'])\n"
        "try:\n"
        f"    subprocess.Popen([sys.executable, '-c', {child_source!r}], start_new_session=True, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "except PermissionError:\n"
        "    Path('detachment-denied').write_text('denied')\n"
        "    raise SystemExit(0)\n"
        "while not Path('detached-ready').exists():\n"
        "    time.sleep(0.02)\n"
        + ("time.sleep(10)\n" if finish != "exit" else "")
    )
    results, errors = [], []

    def execute():
        try:
            results.append(run(native, python_command(source), timeout=2 if finish == "timeout" else 15))
        except Exception as error:
            errors.append(error)

    if finish == "cancel":
        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while worker.is_alive() and not ready.exists() and not denied.exists():
                assert time.monotonic() < deadline, "The native descendant did not reach a known state"
                time.sleep(0.02)
            if ready.exists():
                native.abort()
            worker.join(10)
            assert not worker.is_alive()
        finally:
            if worker.is_alive():
                native.abort()
                worker.join(10)
    else:
        execute()
    assert not errors, errors
    assert results
    result = results[0]
    assert scratch_file.exists(), result.text()
    assert not Path(scratch_file.read_text()).exists(), "Private scratch must be removed after completion"
    if denied.exists():
        assert not result.is_error and result.details["status"] == "ok", result.text()
        assert not ready.exists() and not late.exists()
        return
    assert ready.exists(), result.text()
    expected = {"exit": "ok", "timeout": "timeout", "cancel": "aborted"}[finish]
    assert result.details["status"] == expected, result.text()
    assert result.is_error == (finish != "exit")
    deadline = time.monotonic() + 4.2
    while not late.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not late.exists(), "A detached descendant retained write access after command completion"


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX native sandbox metadata paths")
@pytest.mark.parametrize("background", [False, True])
def test_sandbox_rejects_external_output_symlink_before_spooling(make_session, project, background):
    """Project metadata must not redirect the broker's file writes outside its owner.

    Failure modes: an existing .virgo symlink redirects output mkdir/mkstemp to a
    host directory; background startup cleanup leaves redirected directories;
    a valid cwd hides an invalid output destination until after a host write.
    """
    outside = project.parent / "outside-metadata"
    outside.mkdir()
    (project / ".virgo").symlink_to(outside, target_is_directory=True)
    session = make_session(
        settings={"profile": "minimal", "plugins": {"background": {"enabled": True}},
                  "sandbox": {"mode": "workspace-write"}},
        refresh_models=False, extensions=False,
    )
    result = run(session, "echo sandbox-output", background=background)
    assert result.is_error and result.details["status"] == "sandbox_unavailable"
    assert list(outside.iterdir()) == [], "Invalid output aliases must be rejected before mkdir or mkstemp"
    if background:
        assert not session.services["jobs"].jobs


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX native launch protocol")
@pytest.mark.parametrize("background", [False, True])
def test_wrapper_failure_after_probe_is_not_a_payload_exit(make_session, project, monkeypatch, background):
    """A successful capability probe does not prove the actual envelope started.

    Failure modes: the real wrapper fails after a successful probe but is reported
    as an ordinary command exit; a background job reports successful startup;
    the payload is replayed on the host; failed startup leaves a pending job.
    This launches a real synthetic wrapper that never executes the payload. It
    validates the launch protocol, not OS isolation (covered by native tests).
    """
    launcher = project.parent / "reject_after_probe.py"
    attempts = project.parent / "launch-attempts.txt"
    launcher.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        f"attempts = Path({str(attempts)!r})\n"
        "probe = len(sys.argv) == 2 and Path(sys.argv[1]).name == 'true'\n"
        "with attempts.open('a') as log:\n"
        "    log.write('probe\\n' if probe else 'launch\\n')\n"
        "if probe:\n"
        "    raise SystemExit(0)\n"
        "sys.stderr.write('synthetic sandbox: isolation setup rejected before exec\\n')\n"
        "raise SystemExit(42)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sandbox, "platform_backend", lambda: ("seatbelt", Path(sys.executable)))
    # The synthetic wrapper never executes this probe path. Keep this protocol
    # check independent of host file ownership; native cases check real trust.
    system_file = sandbox._system_file
    monkeypatch.setattr(
        sandbox, "_system_file",
        lambda path: path if path == Path("/usr/bin/true") else system_file(path),
    )
    monkeypatch.setattr(
        sandbox.SandboxPolicy, "_macos",
        lambda _self, executable, _scratch, _output: [str(executable), str(launcher)],
    )
    session = make_session(
        settings={"profile": "minimal", "plugins": {"background": {"enabled": True}},
                  "sandbox": {"mode": "workspace-write"}},
        refresh_models=False, extensions=False,
    )
    result = run(session, "echo bad > payload-ran.txt", background=background)
    assert result.is_error and result.details["status"] == "sandbox_unavailable", result.text()
    assert not (project / "payload-ran.txt").exists()
    assert attempts.read_text().splitlines() == ["probe", "launch"], "Never retry a rejected payload"
    if background:
        assert not session.services["jobs"].jobs



def test_native_protected_symlink_cannot_add_a_host_read_mount(native, project):
    """A payload-created metadata link must not reveal a host secret next launch."""
    secret = project.parent / "outside-sandbox-secret.txt"
    secret.write_text("credential-that-must-stay-outside")
    link = project / "AGENTS.md"
    if sys.platform == "linux":
        # Linux permits creation of an absent metadata name. The next launch
        # must not turn its resolved target into an additional readable mount.
        result = run(native, python_command(
            f"from pathlib import Path; Path('AGENTS.md').symlink_to({str(secret)!r})"
        ))
        assert not result.is_error, result.text()
    else:
        # macOS forbids creation of absent metadata names inside its envelope;
        # an existing project symlink must still not grant its target a read.
        link.symlink_to(secret)
    result = run(native, python_command("from pathlib import Path; print(Path('AGENTS.md').read_text())"))
    assert result.is_error, result.text()
    assert "credential-that-must-stay-outside" not in result.text()
    assert secret.read_text() == "credential-that-must-stay-outside"
    if sys.platform == "linux":
        assert result.details["status"] == "sandbox_unavailable"


def test_native_existing_metadata_symlink_cannot_be_replaced(native, project):
    """Protect the metadata directory entry, not only its resolved file target."""
    target = project / "instructions.txt"
    target.write_text("original instructions")
    link = project / "AGENTS.md"
    link.symlink_to(target)
    result = run(native, python_command(
        "from pathlib import Path; p=Path('AGENTS.md'); p.unlink(); p.write_text('replaced')"
    ))
    assert result.is_error, result.text()
    assert link.is_symlink()
    assert link.read_text() == target.read_text() == "original instructions"


def test_native_workspace_local_virtualenv_runs_but_stays_readonly(native, project):
    """A normal broker venv must run without becoming writable by its own tools."""
    import subprocess

    environment = project / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)],
                   check=True, capture_output=True, text=True, timeout=30)
    marker = environment / "runtime-canary.txt"
    marker.write_text("trusted runtime")
    executable = environment / "bin" / "python"
    repository = Path(__file__).resolve().parents[2]
    # Reuse the already locked test dependencies for this assembled broker. The
    # sandbox still strips PYTHONPATH before starting its user command payload.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([
        str(repository), str(repository / "src"),
        *(str(Path(item).resolve()) for item in sys.path if item),
    ]))
    source = r'''
import shlex
import sys
from pathlib import Path
from virgo.ai.providers import faux
from virgo.harness.session import AgentSession
from tests.support.harness import RecordingUI, tool_results
from tests.support.shell import selected

session = AgentSession(
    Path.cwd(), model="faux/test", session=None, permission_mode="accept-all",
    settings={"profile": "minimal", "sandbox": {"mode": "workspace-write"}},
    ui=RecordingUI(), refresh_models=False, extensions=False,
)
try:
    shell = selected(session)
    def call(name, args):
        faux.script("test", faux.reply(tools=[(name, args)]), faux.reply("done"))
        session.prompt("Exercise the workspace-local virtual environment")
        return tool_results(session)[-1]
    command = shlex.join([sys.executable, "-c",
        "from pathlib import Path; Path('ordinary.txt').write_text('allowed'); print('venv-ready')"])
    result = call(shell.tool_name, {"command": command})
    assert not result.is_error, result.text()
    assert "venv-ready" in result.text()
    assert Path("ordinary.txt").read_text() == "allowed"
    command = shlex.join([sys.executable, "-c",
        "from pathlib import Path; Path('.venv/runtime-canary.txt').write_text('tampered')"])
    result = call(shell.tool_name, {"command": command})
    assert result.is_error, result.text()
    assert Path(".venv/runtime-canary.txt").read_text() == "trusted runtime"
    result = call("write", {"path": ".venv/runtime-canary.txt", "content": "tampered"})
    assert result.is_error, result.text()
    assert Path(".venv/runtime-canary.txt").read_text() == "trusted runtime"
finally:
    session.shutdown()
'''
    completed = subprocess.run([str(executable), "-c", source], cwd=project, env=env,
                               text=True, capture_output=True, timeout=45)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert marker.read_text() == "trusted runtime"


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX native workspace mutation")
@pytest.mark.parametrize("tool", ["write", "edit"])
def test_sandbox_file_tool_rejects_path_replacement_after_permission_check(make_session, project, tool):
    """A workspace entry changed after permission review must not redirect I/O.

    Failure modes: the permission gate checks a normal file; a concurrent
    workspace writer replaces it while the Core tool waits for its existing
    file lock; write/edit follows the replacement and changes a sibling file.
    No permission or execution function is replaced in this assembled scenario.
    """
    import time

    from virgo.tools.fs import file_lock

    target = project / "ordinary.txt"
    outside = project.parent / "outside-file.txt"
    target.write_text("original\n")
    outside.write_text("original\n")
    session = make_session(
        settings={"profile": "minimal", "sandbox": {"mode": "workspace-write"}},
        refresh_models=False, extensions=False,
    )
    arguments = ({"path": "ordinary.txt", "content": "changed\n"} if tool == "write" else
                 {"path": "ordinary.txt", "edits": [{"old_text": "original", "new_text": "changed"}]})
    faux.script("test", faux.reply(tools=[(tool, arguments)]), faux.reply("done"))
    errors = []

    def execute():
        try:
            session.prompt("Update the ordinary project file")
        except Exception as error:
            errors.append(error)

    # Wait for the real executor, after both permission checkpoints. Holding
    # its normal per-file lock makes the filesystem change deterministic.
    with file_lock(target):
        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        reached = False
        while not reached and time.monotonic() < deadline:
            for frame in sys._current_frames().values():
                while frame is not None:
                    if frame.f_code.co_name == "_" + tool and frame.f_locals.get("p") == target:
                        reached = True
                        break
                    frame = frame.f_back
            if not reached:
                time.sleep(0.01)
        if reached:
            target.unlink()
            target.symlink_to(outside)
    worker.join(10)
    assert not worker.is_alive()
    assert reached, "The Core file executor did not reach the locked operation"
    assert not errors, errors
    assert outside.read_text() == "original\n", "The checked workspace path redirected a host write"
    assert tool_results(session)[-1].is_error


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX project context symlinks")
@pytest.mark.parametrize("route", ["rebuild", "child"])
def test_sandbox_project_context_symlink_never_reaches_provider(make_session, project, route):
    """Automatic prompt reads must not disclose a metadata link's host target.

    Failure modes: shell selection/compaction rebuilds cached instructions by
    following a payload-created AGENTS.md link; a new child eagerly reads that
    link; the shell rejects it only after its contents have reached the model;
    filtering project instructions also removes explicitly trusted user context.
    """
    from tests.support.harness import calls

    (project.parent / "home" / "AGENTS.md").write_text("trusted-user-context-marker")
    secret = project.parent / "private-context-secret.txt"
    secret.write_text("credential-never-automatic-project-context")
    session = make_session(
        settings={"profile": "minimal", "sandbox": {"mode": "workspace-write"}},
        refresh_models=False, extensions=False,
    )
    # Linux can create absent metadata names. Reproduce the state after such a
    # payload without requiring this broker-data-flow check to launch a backend.
    (project / "AGENTS.md").symlink_to(secret)
    if route == "rebuild":
        session.prompt("/shell auto")
        result = run(session, "printf context-boundary")
    else:
        api = ExtensionAPI(session, "sandbox-context")
        child = api.create_runtime("context-child", "subagent", parent=session.main)
        try:
            shell = selected(session)
            faux.script("test", faux.reply(tools=[(shell.tool_name, {"command": "printf context-boundary"})]),
                        faux.reply("done"))
            child.agent.prompt("Run the requested local check")
            result = next(message for message in reversed(child.messages)
                          if getattr(message, "tool_name", None) == shell.tool_name)
        finally:
            api.release_runtime(child)
    assert result.is_error and result.details["status"] == "sandbox_unavailable"
    requests = calls()
    assert requests
    for request in requests:
        assert "credential-never-automatic-project-context" not in request["system"]
        assert "trusted-user-context-marker" in request["system"]
    assert secret.read_text() == "credential-never-automatic-project-context"


def test_sandbox_child_context_uses_origin_workspace_not_replacement_cwd(make_session, project):
    """Changing child cwd must not import another workspace or ancestor context."""
    from tests.support.harness import calls

    (project.parent / "home" / "AGENTS.md").write_text("trusted-user-context-marker")
    (project / "AGENTS.md").write_text("origin-workspace-instructions")
    (project.parent / "AGENTS.md").write_text("outside-ancestor-private-context")
    other = project.parent / "outside-context-workspace"
    other.mkdir()
    (other / "AGENTS.md").write_text("replacement-cwd-private-context")
    session = make_session(
        settings={"profile": "minimal", "sandbox": {"mode": "workspace-write"}},
        refresh_models=False, extensions=False,
    )
    api = ExtensionAPI(session, "sandbox-context-origin")
    child = api.create_runtime("outside-context-child", "subagent", parent=session.main, cwd=other)
    try:
        faux.script("test", faux.reply("done"))
        child.agent.prompt("Inspect the original task's project instructions")
        requests = calls()
        assert requests
        for request in requests:
            assert "origin-workspace-instructions" in request["system"]
            assert "trusted-user-context-marker" in request["system"]
            assert "outside-ancestor-private-context" not in request["system"]
            assert "replacement-cwd-private-context" not in request["system"]
    finally:
        api.release_runtime(child)
