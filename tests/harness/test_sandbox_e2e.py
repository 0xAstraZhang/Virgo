"""Broker file boundaries and required native platform acceptance.

Native cases must launch a payload before counting as isolation evidence. The
current shared-path policy refusal is a blocking failure in required runs.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from virgo.ai.providers import faux
from virgo.harness.extensions import ExtensionAPI

from tests.support import native_cases
from tests.support.harness import tool_results, wait_until
from tests.support.sandbox import native_session, python_command, run
from tests.support.shell import selected


@pytest.fixture
def native(make_session):
    return native_session(make_session, {"linux", "darwin"})


def test_native_generic_process_stdio(native, project):
    native_cases.generic_process_stdio(native, project)


@pytest.fixture
def short_workspace():
    """Keep AF_UNIX paths under Darwin's sun_path limit."""
    with tempfile.TemporaryDirectory(prefix="vgf-", dir="/tmp") as directory:
        yield Path(directory).resolve()


def test_workspace_file_tools_obey_sandbox_even_with_accept_all(broker_session, project):
    session = broker_session(
        settings={"profile": "minimal", "execution": {"mode": "workspace-edit"}}, refresh_models=False
    )
    outside = project.parent / "outside.txt"
    faux.script(
        "test",
        faux.reply(
            tools=[
                ("write", {"path": str(outside), "content": "no"}),
                ("write", {"path": ".virgo/settings.json", "content": "{}"}),
                ("write", {"path": "allowed.txt", "content": "yes"}),
            ]
        ),
        faux.reply("done"),
    )
    session.prompt("Try paths under the same mandatory filesystem ceiling")
    assert not outside.exists() and not (project / ".virgo/settings.json").exists()
    assert (project / "allowed.txt").read_text() == "yes"
    assert [r.is_error for r in tool_results(session)] == [True, True, False]


@pytest.mark.parametrize("tool", ["write", "edit"])
@pytest.mark.parametrize("location", ["outside", "metadata", "workspace"])
def test_sandbox_file_tools_atomic_replace_preserves_hardlink_targets(broker_session, project, tool, location):
    """Atomic replacement breaks hardlinks onto a fresh inode without mutating external/metadata targets."""
    source = {
        "outside": project.parent / "outside.txt",
        "metadata": project / ".git" / "config",
        "workspace": project / "source.txt",
    }[location]
    source.parent.mkdir(exist_ok=True)
    source.write_text("original\n")
    alias = project / "ordinary.txt"
    os.link(source, alias)
    session = broker_session(
        settings={"profile": "minimal", "execution": {"mode": "workspace-edit"}},
        refresh_models=False,
        extensions=False,
    )
    arguments = (
        {"path": alias.name, "content": "changed\n"}
        if tool == "write"
        else {"path": alias.name, "edits": [{"old_text": "original", "new_text": "changed"}]}
    )
    faux.script("test", faux.reply(tools=[(tool, arguments)]), faux.reply("done"))
    session.prompt("Update the ordinary project file")
    result = tool_results(session)[-1]
    assert not result.is_error, result.text()
    assert alias.read_text() == "changed\n"
    assert source.read_text() == "original\n"


def test_sandbox_file_mutation_roundtrip_and_failed_edit(broker_session, project):
    """Fresh-inode writes retain content/mode and do not commit a failed edit.

    Failure modes: a new nested file cannot be created; overwrite loses executable
    permissions; edit translates BOM/CRLF; a rejected edit replaces the inode or
    changes its bytes; successful or failed operations leave staging files.
    """
    session = broker_session(
        settings={"profile": "minimal", "execution": {"mode": "workspace-edit"}},
        refresh_models=False,
        extensions=False,
    )
    target = project / "nested" / "ordinary.txt"
    text = "\ufefforiginal\r\n"
    for index, (tool, arguments) in enumerate(
        [
            ("write", {"path": str(target), "content": text}),
            ("write", {"path": str(target), "content": text}),
            ("edit", {"path": str(target), "edits": [{"old_text": "original", "new_text": "changed"}]}),
        ]
    ):
        faux.script("test", faux.reply(tools=[(tool, arguments)]), faux.reply("done"))
        session.prompt("Update the ordinary file")
        assert not tool_results(session)[-1].is_error, tool_results(session)[-1].text()
        if os.name == "posix":
            if index == 0:
                target.chmod(0o751)
            else:
                assert target.stat().st_mode & 0o777 == 0o751
    assert target.read_bytes() == "\ufeffchanged\r\n".encode()
    before = target.stat()
    metadata_before = {p.relative_to(project) for p in (project / ".virgo").rglob("*")}
    assert not any((project / p).is_file() for p in metadata_before)
    for old, new in [("missing", "bad"), ("changed", "changed")]:
        faux.script(
            "test",
            faux.reply(
                tools=[
                    (
                        "edit",
                        {
                            "path": str(target),
                            "edits": [{"old_text": old, "new_text": new}],
                        },
                    )
                ]
            ),
            faux.reply("done"),
        )
        session.prompt("Attempt an edit that cannot change the file")
        assert tool_results(session)[-1].is_error
        assert target.stat().st_ino == before.st_ino and target.stat().st_mode == before.st_mode
        assert target.read_bytes() == "\ufeffchanged\r\n".encode()
        assert {p.relative_to(project) for p in (project / ".virgo").rglob("*")} == metadata_before


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX native workspace mutation")
@pytest.mark.parametrize("tool", ["write", "edit"])
def test_sandbox_file_tool_rejects_path_replacement_after_permission_check(broker_session, project, tool):
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
    session = broker_session(
        settings={"profile": "minimal", "execution": {"mode": "workspace-edit"}},
        refresh_models=False,
        extensions=False,
    )
    arguments = (
        {"path": "ordinary.txt", "content": "changed\n"}
        if tool == "write"
        else {"path": "ordinary.txt", "edits": [{"old_text": "original", "new_text": "changed"}]}
    )
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
def test_sandbox_project_context_symlink_never_reaches_provider(broker_session, project, route):
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
    session = broker_session(
        settings={"profile": "minimal", "execution": {"mode": "workspace-edit"}},
        refresh_models=False,
        extensions=False,
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
            faux.script(
                "test",
                faux.reply(tools=[(shell.tool_name, {"command": "printf context-boundary"})]),
                faux.reply("done"),
            )
            child.agent.prompt("Run the requested local check")
            result = next(
                message
                for message in reversed(child.messages)
                if getattr(message, "tool_name", None) == shell.tool_name
            )
        finally:
            api.release_runtime(child)
    assert result.is_error and result.details["status"] == "execution_unavailable"
    requests = calls()
    assert requests
    for request in requests:
        assert "credential-never-automatic-project-context" not in request["system"]
        assert "trusted-user-context-marker" in request["system"]
    assert secret.read_text() == "credential-never-automatic-project-context"


def test_sandbox_child_context_uses_origin_workspace_not_replacement_cwd(broker_session, project):
    """Changing child cwd must not import another workspace or ancestor context."""
    from tests.support.harness import calls

    (project.parent / "home" / "AGENTS.md").write_text("trusted-user-context-marker")
    (project / "AGENTS.md").write_text("origin-workspace-instructions")
    (project.parent / "AGENTS.md").write_text("outside-ancestor-private-context")
    other = project.parent / "outside-context-workspace"
    other.mkdir()
    (other / "AGENTS.md").write_text("replacement-cwd-private-context")
    session = broker_session(
        settings={"profile": "minimal", "execution": {"mode": "workspace-edit"}},
        refresh_models=False,
        extensions=False,
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


def test_native_workspace_write_and_indirect_escape(native, project):
    native_cases.workspace_write_and_indirect_escape(native, project)
    # The direct payload is not enough: a child process must not gain an
    # outside write through a workspace symlink either.
    with tempfile.TemporaryDirectory(prefix="virgo-boundary-", dir=sys.prefix) as directory:
        outside = Path(directory) / "external.txt"
        outside.write_text("original")
        (project / "escape-link").symlink_to(outside)
        source = (
            "from pathlib import Path; import subprocess, sys; "
            "Path('local.txt').write_text('yes'); "
            "subprocess.run([sys.executable, '-c', "
            "\"from pathlib import Path; Path('escape-link').write_text('bad')\"], check=True)"
        )
        result = run(native, python_command(native, source))
        assert result.is_error, result.text()
        assert (project / "local.txt").read_text() == "yes"
        assert outside.read_text() == "original"
        assert result.details["boundary"]["payload_started"] is True


def test_native_live_workspace_reader_observes_edit(native, project):
    native_cases.live_workspace_reader_observes_edit(native, project)


def test_posix_readonly_shell_grant_writes_one_existing_file(native, project):
    native_cases.readonly_shell_grant_writes_one_existing_file(native, project)


def test_native_readonly_including_child_shell(native, project):
    native_cases.readonly_including_child_shell(native, project)


def test_native_partial_execution_preserves_denied_file(native, project):
    native_cases.partial_execution_preserves_denied_file(native, project)


def test_native_network_and_environment_isolation(native, project, monkeypatch):
    native_cases.network_and_environment_isolation(native, project, monkeypatch)


def test_native_failure_and_timeout_are_not_reclassified(native):
    native_cases.failure_and_timeout_are_not_reclassified(native)


def test_shared_native_policy_is_rejected_before_payload(broker_session, project):
    session = broker_session(approval="never")
    result = run(session, "echo forbidden")
    assert result.is_error and result.details["status"] == "execution_unavailable"
    assert result.details["boundary"]["payload_started"] is False
    assert result.details["boundary"]["side_effects"] == "none"
    assert result.details["boundary"]["reason"] == "backend_unavailable"
    assert not session.ui.questions


def test_native_existing_metadata_is_readonly(native, project):
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("original")
    (project / "AGENTS.md").write_text("original")
    for name in (".git/config", "AGENTS.md"):
        result = run(native, python_command(native, f"from pathlib import Path; Path({name!r}).write_text('bad')"))
        assert result.is_error and (project / name).read_text() == "original"


@pytest.mark.parametrize("location", ["outside", "metadata"])
def test_native_cannot_create_hardlink_from_readonly_source(native, project, location):
    """A clean launch cannot acquire an outside inode through link creation.

    Failure modes: a readable runtime or metadata file can be hardlinked into
    the workspace after launch validation, then changed through the new alias.
    The outside source is visible in the existing interpreter read-only mount.
    """
    with tempfile.TemporaryDirectory(prefix="virgo-link-source-", dir=sys.prefix) as directory:
        source = (Path(directory) / "outside.txt" if location == "outside" else
                  project / ".git" / "config")
        source.parent.mkdir(exist_ok=True)
        source.write_text("original")
        result = run(native, python_command(native,
            f"import os; from pathlib import Path; os.link({str(source)!r}, 'new-alias.txt'); "
            "Path('new-alias.txt').write_text('changed')"
        ))
        assert source.read_text() == "original", result.text()
        assert result.is_error and not (project / "new-alias.txt").exists(), result.text()


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
            results.append(run(native, python_command(native, source), timeout=2 if finish == "timeout" else 15))
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
        assert result.details["boundary"]["cleanup"] == "executor_completed"
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


def test_native_posix_spawn_preserves_common_use_and_reaps_detached_child(native, project):
    """Ordinary posix_spawn must work without leaving a detached child behind.

    Failure modes: banning the setpgid syscall misses posix_spawn's setpgroup
    attribute; banning all posix_spawn breaks ordinary project commands;
    the spawned child retains workspace writes after command completion. Only
    detachment may be denied; every fixture process has a bounded lifetime.
    """
    import time

    normal_source = (
        "import signal; from pathlib import Path; signal.alarm(8); "
        "Path('ordinary-posix-spawn').write_text('spawned')"
    )
    detached_source = """import signal, time
from pathlib import Path
signal.alarm(8)
Path('spawn-detached-ready').write_text('ready')
time.sleep(4)
Path('spawn-detached-late-write').write_text('survived')
"""
    source = f"""import os, signal, sys, time
from pathlib import Path
signal.alarm(8)
pid = os.posix_spawn(sys.executable, [sys.executable, '-c', {normal_source!r}], os.environ)
assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 0
actions = [(os.POSIX_SPAWN_OPEN, fd, os.devnull, os.O_RDWR, 0) for fd in range(3)]
try:
    os.posix_spawn(sys.executable, [sys.executable, '-c', {detached_source!r}],
                   os.environ, file_actions=actions, setpgroup=0)
except PermissionError:
    Path('spawn-detachment-denied').write_text('denied')
    raise SystemExit(0)
deadline = time.monotonic() + 3
while not Path('spawn-detached-ready').exists():
    assert time.monotonic() < deadline, 'The spawned child did not become ready'
    time.sleep(0.02)
"""
    result = run(native, python_command(native, source), timeout=10)
    assert not result.is_error and result.details["status"] == "ok", result.text()
    assert result.details["boundary"]["cleanup"] == "executor_completed"
    assert (project / "ordinary-posix-spawn").read_text() == "spawned"
    late = project / "spawn-detached-late-write"
    if (project / "spawn-detachment-denied").exists():
        assert not (project / "spawn-detached-ready").exists() and not late.exists()
        return
    assert (project / "spawn-detached-ready").exists(), result.text()
    deadline = time.monotonic() + 4.2
    while not late.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not late.exists(), "A posix_spawn child in a new process group retained workspace access after completion"


def test_native_double_fork_descendant_is_reaped_after_intermediate_exit(native, project):
    """A short-lived intermediate must not hide an orphaned detached descendant.

    Failure modes: cleanup observes only the command's current process tree;
    a double-forked child escapes that tree and its original process group;
    completion removes scratch while the orphan can still write to the project.
    Detachment may be denied; forked fixture children terminate on a short alarm.
    """
    import time

    intermediate_source = """import os, signal, time
from pathlib import Path
signal.alarm(8)
if os.fork():
    os._exit(0)
signal.alarm(8)
try:
    os.setsid()
except PermissionError:
    Path('double-fork-detachment-denied').write_text('denied')
    os._exit(0)
if os.fork():
    os._exit(0)
signal.alarm(8)
Path('double-fork-ready').write_text('ready')
time.sleep(4)
Path('double-fork-late-write').write_text('survived')
os._exit(0)
"""
    source = f"""import signal, subprocess, sys, time
from pathlib import Path
signal.alarm(10)
subprocess.run([sys.executable, '-c', {intermediate_source!r}], check=True, timeout=4,
               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 3
while not Path('double-fork-ready').exists() and not Path('double-fork-detachment-denied').exists():
    assert time.monotonic() < deadline, 'The orphan did not reach a known state'
    time.sleep(0.02)
"""
    result = run(native, python_command(native, source), timeout=12)
    assert not result.is_error and result.details["status"] == "ok", result.text()
    assert result.details["boundary"]["cleanup"] == "executor_completed"
    late = project / "double-fork-late-write"
    if (project / "double-fork-detachment-denied").exists():
        assert not (project / "double-fork-ready").exists() and not late.exists()
        return
    assert (project / "double-fork-ready").exists(), result.text()
    deadline = time.monotonic() + 4.2
    while not late.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not late.exists(), "An orphaned double-fork descendant retained workspace access after completion"


def test_native_command_cleanup_preserves_independent_background_job(native, make_session, project):
    """Completion and cancellation must clean only their own command envelope.

    Failure modes: a shared containment identity kills another active command;
    a stale running-job record conceals that collateral termination; normal
    background completion leaves its process or private scratch behind. The
    background process acknowledges each completed cleanup and self-terminates
    on a short deadline even if an assertion or broker cleanup fails.
    """
    background_source = """import os, signal, time
from pathlib import Path
signal.alarm(15)
Path('independent-background-scratch').write_text(os.environ['TMPDIR'])
Path('independent-background-ready').write_text('ready')
for stage in ('exit', 'cancel'):
    while not Path('check-background-after-' + stage).exists():
        time.sleep(0.02)
    Path('background-alive-after-' + stage).write_text('alive')
while not Path('finish-independent-background').exists():
    time.sleep(0.02)
Path('independent-background-finished').write_text('finished')
"""
    session = make_session(
        settings={"profile": "minimal", "plugins": {"background": {"enabled": True}},
                  **native.settings.overrides},
        refresh_models=False, extensions=False,
    )
    jobs = session.services["jobs"]
    job = worker = None
    results, errors = [], []

    def cancel_foreground():
        try:
            results.append(run(session, python_command(native,
                "import signal,time; from pathlib import Path; signal.alarm(12); "
                "Path('independent-cancel-ready').write_text('ready'); time.sleep(10)"
            ), timeout=12))
        except Exception as error:
            errors.append(error)

    try:
        result = run(session, python_command(native, background_source), background=True, timeout=15)
        assert not result.is_error, result.text()
        assert len(jobs.jobs) == 1
        job = next(iter(jobs.jobs.values()))
        wait_until((project / "independent-background-ready").exists)

        result = run(session, "printf foreground-completed")
        assert not result.is_error and result.details["status"] == "ok", result.text()
        assert "foreground-completed" in result.text()
        (project / "check-background-after-exit").write_text('check')
        wait_until((project / "background-alive-after-exit").exists)
        assert not job.done.is_set() and job.proc.poll() is None

        worker = threading.Thread(target=cancel_foreground, daemon=True)
        worker.start()
        wait_until(lambda: (project / "independent-cancel-ready").exists() or not worker.is_alive())
        assert (project / "independent-cancel-ready").exists(), errors
        session.abort()
        worker.join(5)
        assert not worker.is_alive() and not errors, errors
        assert results and results[0].is_error and results[0].details["status"] == "aborted"
        (project / "check-background-after-cancel").write_text('check')
        wait_until((project / "background-alive-after-cancel").exists)
        assert not job.done.is_set() and job.proc.poll() is None

        (project / "finish-independent-background").write_text('finish')
        assert job.done.wait(5), "The independent background command did not finish"
        assert job.status == "completed" and job.exit_code == 0, jobs.output(job)
        assert (project / "independent-background-finished").read_text() == "finished"
        assert job.proc.poll() is not None
        scratch = Path((project / "independent-background-scratch").read_text())
        assert not scratch.exists(), "Normal background completion must remove its private scratch"
    finally:
        (project / "finish-independent-background").write_text('finish')
        if worker is not None and worker.is_alive():
            session.abort()
            worker.join(5)
        if job is not None and not job.done.is_set():
            jobs.kill(job)
            job.done.wait(5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX metadata symlinks")
@pytest.mark.parametrize("tool", ["write", "edit"])
def test_sandbox_file_mutation_rejects_metadata_symlink(broker_session, project, tool):
    """Atomic staging never touches .virgo, and writes through a .virgo symlink are denied."""
    outside = project.parent / "outside-stage"
    outside.mkdir()
    (project / ".virgo").symlink_to(outside, target_is_directory=True)
    target = project / "ordinary.txt"
    target.write_text("original")
    session = broker_session(
        settings={"profile": "minimal", "execution": {"mode": "workspace-edit"}},
        refresh_models=False, extensions=False,
    )
    arguments = ({"path": target.name, "content": "changed"} if tool == "write" else
                 {"path": target.name, "edits": [{"old_text": "original", "new_text": "changed"}]})
    faux.script("test", faux.reply(tools=[
        (tool, arguments),
        ("write", {"path": ".virgo/settings.json", "content": "{}"}),
    ]), faux.reply("done"))
    session.prompt("Update an ordinary file without touching the .virgo symlink")
    ordinary_result, meta_result = tool_results(session)
    assert not ordinary_result.is_error and target.read_text() == "changed"
    assert meta_result.is_error and meta_result.details["status"] == "resource_denied"
    assert not list(outside.iterdir())


@pytest.mark.parametrize("background", [False, True])
def test_sandbox_rejects_changed_cwd_before_creating_output(broker_session, project, background):
    """A late cwd change must not create host directories or logs outside the owner.

    Failure modes: foreground spooling creates an external log before rejection;
    background start failure removes its log but leaves external directories;
    changed cwd replaces the originating workspace; a failed launch is replayed.
    """
    outside = project.parent / "outside-cwd"
    outside.mkdir()
    session = broker_session(
        settings={"profile": "minimal", "plugins": {"background": {"enabled": True}},
                  "execution": {"mode": "workspace-edit"}},
        refresh_models=False, extensions=False,
    )

    def change_cwd(event, _session):
        event.cwd = outside

    ExtensionAPI(session, "sandbox-cwd-boundary").on("tool_call", change_cwd)
    command = ("Set-Content escaped.txt bad" if selected(session).tool_name == "powershell"
               else "echo bad > escaped.txt")
    result = run(session, command, background=background)
    assert result.is_error and result.details["status"] == "execution_unavailable"
    assert result.details["boundary"]["workspace"] == str(project.resolve())
    assert not (outside / "escaped.txt").exists()
    assert not (outside / ".virgo").exists(), "Rejected cwd must not receive broker output files"
    if background:
        assert not session.services["jobs"].jobs


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX native sandbox metadata paths")
@pytest.mark.parametrize("background", [False, True])
def test_sandbox_rejects_external_output_symlink_before_spooling(broker_session, project, background):
    """Project metadata must not redirect the broker's file writes outside its owner.

    Failure modes: an existing .virgo symlink redirects output mkdir/mkstemp to a
    host directory; background startup cleanup leaves redirected directories;
    a valid cwd hides an invalid output destination until after a host write.
    """
    outside = project.parent / "outside-metadata"
    outside.mkdir()
    (project / ".virgo").symlink_to(outside, target_is_directory=True)
    session = broker_session(
        settings={"profile": "minimal", "plugins": {"background": {"enabled": True}},
                  "execution": {"mode": "workspace-edit"}},
        refresh_models=False, extensions=False,
    )
    result = run(session, "echo sandbox-output", background=background)
    assert result.is_error and result.details["status"] == "execution_unavailable"
    assert list(outside.iterdir()) == [], "Invalid output aliases must be rejected before mkdir or mkstemp"
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
        result = run(native, python_command(native,
            f"from pathlib import Path; Path('AGENTS.md').symlink_to({str(secret)!r})"
        ))
        assert not result.is_error, result.text()
    else:
        # macOS forbids creation of absent metadata names inside its envelope;
        # an existing project symlink must still not grant its target a read.
        link.symlink_to(secret)
    result = run(native, python_command(native, "from pathlib import Path; print(Path('AGENTS.md').read_text())"))
    assert result.is_error, result.text()
    assert "credential-that-must-stay-outside" not in result.text()
    assert secret.read_text() == "credential-that-must-stay-outside"


def test_native_existing_metadata_symlink_cannot_be_replaced(native, project):
    """S3-02: Symlinked control metadata protects its real target (and directory entry on macOS) without breaking sessions."""
    target = project / "instructions.txt"
    target.write_text("original instructions")
    link = project / "AGENTS.md"
    link.symlink_to(target)
    if sys.platform == "darwin":
        result = run(native, python_command(native,
            "from pathlib import Path; p=Path('AGENTS.md'); p.unlink(); p.write_text('replaced')"
        ))
        assert result.is_error, result.text()
        assert link.is_symlink()
        assert link.read_text() == target.read_text() == "original instructions"
    else:
        # On Linux bwrap, symlinked control files (e.g. CLAUDE.md -> AGENTS.md)
        # bind-mount their readable target read-only so sessions work without false
        # positives while writes through the symlink fail with EROFS.
        read_res = run(native, python_command(native, "from pathlib import Path; print(Path('AGENTS.md').read_text())"))
        assert not read_res.is_error and "original instructions" in read_res.text(), read_res.text()
        write_res = run(native, python_command(native, "from pathlib import Path; Path('AGENTS.md').write_text('replaced')"))
        assert write_res.is_error, write_res.text()
        assert link.is_symlink()
        assert link.read_text() == target.read_text() == "original instructions"


def test_missing_backend_is_fail_closed_in_real_session(make_session, project, monkeypatch):
    from virgo.execution import ExecutionUnavailable

    def unavailable():
        raise ExecutionUnavailable("Missing native backend")

    monkeypatch.setattr("virgo.sandbox.native.platform_backend", unavailable)
    session = make_session(settings={"execution": {"mode": "workspace-edit"}}, refresh_models=False, extensions=False)
    result = run(session, python_command(session, "from pathlib import Path; Path('payload-ran').write_text('bad')"))
    assert result.is_error and result.details["status"] == "execution_unavailable"
    assert result.details["boundary"]["payload_started"] is False
    assert "Missing native backend" in result.text()
    assert not (project / "payload-ran").exists()


def test_required_provider_initialization_failure_aborts_session(make_session, project, monkeypatch):
    import virgo.sandbox as extension

    def broken(api, executor):
        raise RuntimeError('required provider setup failed')

    monkeypatch.setattr(extension, 'SandboxExecutionProvider', broken)
    with pytest.raises(RuntimeError, match='required provider setup failed'):
        make_session(settings={'execution': {'mode': 'workspace-edit'}}, extensions=False, refresh_models=False)
    assert not (project / 'payload-ran').exists()


def test_project_cannot_disable_user_sandbox(make_session, project):
    import json

    metadata = project / '.virgo'
    metadata.mkdir()
    (metadata / 'settings.json').write_text(json.dumps({'execution': {'mode': 'no-limit'}}))
    with pytest.raises(ValueError, match='[Pp]roject'):
        make_session(settings={'execution': {'mode': 'workspace-edit'}}, refresh_models=False)


@pytest.mark.parametrize('background', [False, True])
@pytest.mark.parametrize('payload', [False, True], ids=['wrapper-failed', 'ack-lost'])
def test_unacknowledged_native_launch_keeps_unknown_facts(broker_session, project, monkeypatch, background, payload):
    """A lost startup acknowledgement cannot authorize replay."""
    from virgo.execution import ExecutionFailed
    from virgo.sandbox.native import NativeExecutor

    attempts = []

    def uncertain(self, request, policy, cancellation):
        attempts.append(request.argv)
        if payload:
            (project / "payload-ran").write_text("once")
        raise ExecutionFailed("The native launcher could not confirm execution")

    session = broker_session()
    monkeypatch.setattr(NativeExecutor, "run_process", uncertain)
    result = run(session, "synthetic workload", background=background)
    assert result.is_error and result.details["status"] == "execution_failed"
    if background:
        assert not session.services["jobs"].jobs
    boundary = result.details["boundary"]
    assert len(attempts) == 1
    assert boundary["payload_started"] == "unknown"
    assert boundary["side_effects"] == "possible"
    assert (project / "payload-ran").exists() is payload


@pytest.mark.parametrize("route", ["child", "main-rebuild", "new-main"])
def test_automatic_context_cannot_borrow_another_conversations_no_limit_mode(broker_session, project, route):
    """Prompt construction must use its owner before any provider request.

    Failure modes: a child of an inactive restricted origin, its rebuilt lead,
    or a newly constructed restricted conversation inherits the visible lead's
    No-Limit setting and automatically reads ancestor or replacement-cwd files.
    """
    from tests.support.harness import calls

    (project.parent / "home" / "AGENTS.md").write_text("trusted-user-context-marker")
    (project / "AGENTS.md").write_text("origin-workspace-instructions")
    (project.parent / "AGENTS.md").write_text("outside-ancestor-private-context")
    other = project.parent / "outside-context-workspace"
    other.mkdir()
    (other / "CLAUDE.md").write_text("replacement-cwd-private-context")
    session = broker_session(
        settings={"profile": "minimal", "execution": {"mode": "workspace-edit"}},
        refresh_models=False, extensions=False,
    )
    origin = session.main
    session.services["execution"].select_mode("workspace-edit")
    session.new_session()
    session.services["execution"].select_mode("no-limit")
    assert session.main.execution_context.provider == "host"
    api = ExtensionAPI(session, "context-origin-profile")
    child = None
    try:
        if route == "child":
            runtime = child = api.create_runtime("origin-profile-child", "subagent", parent=origin, cwd=other)
        elif route == "main-rebuild":
            runtime = origin
            runtime.rebuild_system()
        else:
            session.new_session()
            runtime = session.main
        faux.script("test", faux.reply("done"))
        runtime.agent.prompt("Inspect the original task's instructions")
        assert str(project.parent) not in runtime.execution_context.resources.read_paths
        assert calls()
        for request in calls():
            assert "origin-workspace-instructions" in request["system"]
            assert "trusted-user-context-marker" in request["system"]
            assert "outside-ancestor-private-context" not in request["system"]
            assert "replacement-cwd-private-context" not in request["system"]
    finally:
        if child is not None:
            api.release_runtime(child)


def test_native_background_and_child_preserve_workspace(native, project):
    result = run(native, python_command(native, "from pathlib import Path; Path('background.txt').write_text('yes')"), background=True)
    assert not result.is_error, result.text()
    job = next(iter(native.services['jobs'].jobs.values()))
    assert job.done.wait(10) and job.status == 'completed'
    assert (project / 'background.txt').read_text() == 'yes'
    api = ExtensionAPI(native, 'native-child-origin')
    child = api.create_runtime('outside-child', 'subagent', parent=native.main, cwd=project.parent)
    try:
        faux.script('test', faux.reply(tools=[(selected(native).tool_name, {
            'command': python_command(native, "from pathlib import Path; Path('outside-child.txt').write_text('bad')")})]), faux.reply('done'))
        child.agent.prompt('Attempt a cwd escape')
        assert not (project.parent / 'outside-child.txt').exists()
        assert any(getattr(message, 'is_error', False) for message in child.messages)
    finally:
        api.release_runtime(child)


@pytest.mark.parametrize('mode,relative', [('read-only', 'ordinary.txt'), ('workspace-write', '.git/config')])
def test_native_readonly_hardlinks_remain_usable(native, project, mode, relative):
    source = project.parent / 'readonly-shared.txt'
    source.write_text('original')
    alias = project / relative
    alias.parent.mkdir(exist_ok=True)
    os.link(source, alias)
    native.services['execution'].select_mode('read-only' if mode == 'read-only' else 'workspace-edit')
    result = run(native, python_command(native, f'from pathlib import Path; print(Path({relative!r}).read_text())'))
    assert not result.is_error and 'original' in result.text(), result.text()
    result = run(native, python_command(native, f'from pathlib import Path; Path({relative!r}).write_text("bad")'))
    assert result.is_error and source.read_text() == 'original'


def test_native_network_permission_supports_system_name_resolution(native):
    import os
    import socket

    host = os.environ.get("VIRGO_SANDBOX_NETWORK_TEST_HOST", "www.microsoft.com")
    with socket.create_connection((host, 443), timeout=10):
        pass
    source = (f"import socket; connection=socket.create_connection(({host!r},443),timeout=10); "
              "connection.close(); print('connected')")
    result = run(native, python_command(native, source))
    assert not result.is_error and 'connected' in result.text(), result.text()


@pytest.mark.parametrize('read_only', [False, True])
@pytest.mark.parametrize('workspace_only', [False, True])
@pytest.mark.parametrize('network_allowed', [False, True])
def test_native_independent_permission_combinations(native, project, short_workspace, read_only, workspace_only, network_allowed):
    import socket

    extra = project.parent / 'extra-resource'
    extra.mkdir()
    (extra / 'readable').write_text('extra')
    inside = project / 'inside.txt'
    outside = extra / 'outside.txt'
    inside.write_text('original')
    outside.write_text('original')
    secret = native.paths.user / 'private-secret.txt'
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('secret')
    before = {path: (path.stat().st_dev, path.stat().st_ino, path.read_text()) for path in (inside, outside)}
    # Exercise the effective-policy boundary below the fixed user mode selector.
    from dataclasses import replace
    from threading import Event

    from virgo.execution_policy import PathRule, PermissionPolicy
    from virgo.tools.bash import run_command

    roots = (project,) if workspace_only else (project, extra)
    context = native.main.execution_context
    policy = PermissionPolicy((
        *(rule for rule in context.policy.filesystem if rule.effect == "deny"),
        *(PathRule(path, "read" if read_only else "write") for path in roots),
        PathRule(short_workspace, "read"),
    ), network_allowed)
    context = replace(context, resources=replace(context.resources, policy=policy))
    with socket.socket() as listener, socket.socket(socket.AF_UNIX) as host_socket:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        host_socket.bind(str(short_workspace / 'host.sock'))
        host_socket.listen()
        with socket.socket(socket.AF_UNIX) as control:
            control.connect(str(short_workspace / 'host.sock'))
            connected, _ = host_socket.accept()
            connected.close()
        source = f'''import socket
from pathlib import Path
def attempt(fn):
    try: fn(); return True
    except OSError: return False
assert attempt(lambda: Path('created').write_text('yes')) is {not read_only!r}
assert attempt(lambda: Path({str(extra / 'readable')!r}).read_text()) is {not workspace_only!r}
assert attempt(lambda: Path({str(extra / 'created')!r}).write_text('yes')) is {not read_only and not workspace_only!r}
assert attempt(lambda: Path({str(inside)!r}).write_text('changed')) is {not read_only!r}
assert attempt(lambda: Path({str(outside)!r}).write_text('changed')) is {not read_only and not workspace_only!r}
assert attempt(lambda: Path({str(secret)!r}).read_text()) is False
assert attempt(lambda: socket.create_connection({listener.getsockname()!r}, 2).close()) is {network_allowed!r}
assert attempt(lambda: socket.socket(socket.AF_UNIX).connect({str(short_workspace / 'host.sock')!r})) is False
print('independent dimensions checked')
'''
        if sys.platform == "linux":
            probe = Path(__file__).parents[1] / "support" / "socketpair_probe.py"
            source += probe.read_text() + f"\nverify_socketpairs({str(short_workspace / 'host.sock')!r})\n"
        output, code, status = run_command(python_command(native, source), project, 15, Event(),
                                           interpreter=selected(native), execution_context=context)
        host_socket.setblocking(False)
        with pytest.raises(BlockingIOError):
            host_socket.accept()
    assert code == 0 and status == "ok" and "independent dimensions checked" in output.text, output.text
    for path, allowed in ((inside, not read_only),
                          (outside, not read_only and not workspace_only)):
        current = path.stat()
        original_device, original_inode, original_text = before[path]
        assert (current.st_dev, current.st_ino) == (original_device, original_inode)
        assert path.read_text() == ("changed" if allowed else original_text), path


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
import os
import shlex
import sys
from pathlib import Path
from virgo.ai.providers import faux
from virgo.harness.session import AgentSession
from tests.support.harness import RecordingUI, tool_results
from tests.support.shell import selected

session = AgentSession(
    Path.cwd(), model="faux/test", session=None, approval_policy="never",
    settings={"execution": {"mode": "workspace-edit"}},
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


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX executable shell discovery")
@pytest.mark.parametrize("route", ["project", "path", "selection"])
def test_sandbox_shell_discovery_does_not_execute_candidate_on_host(broker_session, project, monkeypatch, route):
    """Finding or selecting a shell must not run it outside the requested sandbox.

    Failure modes: untrusted project shell.executable runs a version probe before
    permissions exist; PATH discovery executes candidates that are never selected;
    an explicit /shell command probes natively despite an unavailable backend.
    The real fixture executable only writes a disposable local sentinel and
    prints a valid version. It never reads credentials or accesses an external path.
    """
    directory = project / "candidate-shell"
    directory.mkdir()
    candidate = directory / "bash"
    sentinel = directory / "executed-on-host.txt"
    candidate.write_text(
        "#!/bin/sh\n"
        f"printf 'version probe executed\\n' >> {shlex.quote(str(sentinel))}\n"
        "printf '5.2.0'\n",
        encoding="utf-8",
    )
    candidate.chmod(0o755)

    monkeypatch.delenv("VIRGO_SHELL", raising=False)
    if route == "project":
        config = project / ".virgo"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({"shell": {"executable": str(candidate)}}))
    elif route == "path":
        monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))
    session = broker_session(
        settings={"profile": "minimal", "execution": {"mode": "workspace-edit"}},
        refresh_models=False, extensions=False, trust_project_extensions=False,
    )
    assert session.services["execution"].name == "sandbox"
    if route == "selection":
        assert not sentinel.exists()
        session.prompt(f"/shell {shlex.quote(str(candidate))}")
    assert not sentinel.exists(), "Shell discovery/selection executed an unisolated candidate"


def test_sandbox_status_reports_effective_read_only(broker_session):
    session = broker_session(preset='read-only')
    session.prompt('/permissions show')
    state = json.JSONDecoder().raw_decode(session.ui.text[session.ui.text.index('{'):])[0]
    assert state['provider'] == 'sandbox'
    assert state['requested']['write_paths'] == []
    assert state['effective']['file_tools']['write_paths'] == []
    assert state['effective']['native_process'] is None
    assert state['native_support'] == 'checked_at_launch'
    assert state['state'] != 'established'


@pytest.mark.skipif(os.name != "posix", reason="Linux resolver metadata and POSIX account lookup")
def test_linux_resolver_configuration_accepts_only_trusted_service_metadata(project, monkeypatch):
    """Systemd-owned DNS data is not an executable runtime.

    Ubuntu CI reproduced rejection of its actual service-owned stub-resolv.conf.
    This focused envelope check substitutes only fixed system-path metadata; the
    separate native test still has to establish actual name resolution and TCP.
    """
    import pwd

    from virgo.sandbox import platform as sandbox

    entry = Path("/etc/resolv.conf")
    target = Path("/run/systemd/resolve/stub-resolv.conf")
    service_uid = 42424
    metadata = {"entry_uid": 0, "target_uid": service_uid, "target_mode": stat.S_IFREG | 0o644,
                "directory_uid": service_uid}
    root_paths = {*entry.parents, *target.parents}
    root_files = {Path("/etc/hosts"), Path("/etc/nsswitch.conf")}
    original_stat, original_resolve, original_account = Path.stat, Path.resolve, pwd.getpwnam

    def info(mode, uid):
        return os.stat_result((mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))

    def system_stat(path, *, follow_symlinks=True):
        if path == entry and not follow_symlinks:
            return info(stat.S_IFLNK | 0o777, metadata["entry_uid"])
        if path in {entry, target}:
            return info(metadata["target_mode"], metadata["target_uid"])
        if path == target.parent:
            return info(stat.S_IFDIR | 0o755, metadata["directory_uid"])
        if path in root_paths:
            return info(stat.S_IFDIR | 0o755, 0)
        if path in root_files:
            return info(stat.S_IFREG | 0o644, 0)
        return original_stat(path, follow_symlinks=follow_symlinks)

    def system_resolve(path, *args, **kwargs):
        if path == entry:
            return target
        if path in root_files:
            return path
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", system_stat)
    monkeypatch.setattr(Path, "resolve", system_resolve)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=service_uid)
                        if name == "systemd-resolve" else original_account(name))
    policy = sandbox.NativePolicy(project, True, True, (), ())

    def envelope():
        return policy._linux(Path("/usr/bin/bwrap"), project.parent / "scratch", project, 99, ())

    args = envelope()
    mounts = [args[index:index + 3] for index, value in enumerate(args) if value == "--ro-bind"]
    assert ["--ro-bind", str(target), str(target)] in mounts
    assert ["--ro-bind", str(target.parent), str(target.parent)] not in mounts
    with pytest.raises(sandbox.SandboxUnavailable, match="not system-owned"):
        sandbox._system_file(entry)  # Executable/library trust must stay strict.
    for key, value in (("entry_uid", service_uid), ("target_uid", service_uid + 1),
                       ("directory_uid", service_uid + 1), ("target_mode", stat.S_IFREG | 0o666),
                       ("target_mode", stat.S_IFLNK | 0o444)):
        previous, metadata[key] = metadata[key], value
        with pytest.raises(sandbox.SandboxUnavailable):
            envelope()
        metadata[key] = previous


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
    from virgo.sandbox import native as dispatcher
    from virgo.sandbox import platform as sandbox

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
    monkeypatch.setattr(dispatcher, "platform_backend", sandbox.platform_backend)
    system_file = sandbox._system_file
    monkeypatch.setattr(
        sandbox, "_system_file",
        lambda path: path if path == Path("/usr/bin/true") else system_file(path),
    )
    monkeypatch.setattr(
        sandbox.NativePolicy, "_macos",
        lambda _self, executable, _scratch: [str(executable), str(launcher)],
    )
    session = make_session(
        settings={"profile": "minimal", "plugins": {"background": {"enabled": True}},
                  "execution": {"mode": "workspace-edit"}},
        refresh_models=False, extensions=False,
    )
    result = run(session, "echo bad > payload-ran.txt", background=background)
    assert result.is_error and result.details["status"] == "execution_unavailable", result.text()
    assert not (project / "payload-ran.txt").exists()
    assert attempts.read_text().splitlines() == ["probe", "launch"], "Never retry a rejected payload"
    if background:
        assert not session.services["jobs"].jobs


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX native launch protocol")
@pytest.mark.parametrize("background", [False, True])
def test_missing_exec_acknowledgement_does_not_claim_no_effects(make_session, project, monkeypatch, background):
    """A delayed broker can miss an acknowledgement after the payload ran.

    The synthetic wrapper exercises real exec/pipe/lifecycle behavior, not OS
    isolation. Only acknowledgement observation and the broker deadline clock
    are controlled; the payload writes a disposable marker exactly once.
    """
    from virgo.sandbox import native as dispatcher
    from virgo.sandbox import platform as sandbox

    launcher = project.parent / "forward_payload.py"
    marker = project / "payload-ran.txt"
    launcher.write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "if len(sys.argv) == 2 and Path(sys.argv[1]).name == 'true':\n"
        "    raise SystemExit(0)\n"
        "os.execv(sys.argv[1], sys.argv[1:])\n"
    )
    monkeypatch.setattr(sandbox, "platform_backend", lambda: ("protocol-test", Path(sys.executable)))
    monkeypatch.setattr(dispatcher, "platform_backend", sandbox.platform_backend)
    system_file = sandbox._system_file
    monkeypatch.setattr(sandbox, "_system_file",
                        lambda path: path if path == Path("/usr/bin/true") else system_file(path))
    monkeypatch.setattr(sandbox.NativePolicy, "_macos",
                        lambda _self, executable, _scratch: [str(executable), str(launcher)])
    delayed = False

    def lose_ack(*_args):
        nonlocal delayed
        wait_until(lambda: marker.exists() and marker.read_text() == "once", timeout=3)
        delayed = True
        return [], [], []

    monkeypatch.setattr(sandbox, "select", SimpleNamespace(select=lose_ack))
    monkeypatch.setattr(sandbox, "time", SimpleNamespace(
        monotonic=lambda: time.monotonic() + (10 if delayed else 0), sleep=time.sleep))
    session = make_session(settings={"profile": "minimal", "plugins": {"background": {"enabled": True}},
                                     "execution": {"mode": "workspace-edit"}}, refresh_models=False, extensions=False)
    source = "from pathlib import Path\nwith Path('payload-ran.txt').open('a') as stream: stream.write('once')"
    result = run(session, python_command(session, source), background=background)
    assert marker.read_text() == "once"
    assert result.is_error and result.details.get("status") == "execution_failed", result.text()
    assert result.details["boundary"]["side_effects"] == "possible"


def test_macos_env_override_not_exposed_in_ps_and_option_name_rejected(native, project):
    """VER-03: GITHUB_TOKEN override is never in process argv/plist/supervisor env; -S... is rejected."""
    import hashlib
    import shutil
    import subprocess as sp

    from virgo.execution import ExecutionUnavailable, ProcessRequest, cleanup_process

    secret = "ghp_virgo_ver03_secret_token_987654"
    digest = hashlib.sha256(secret.encode()).hexdigest()
    with pytest.raises(ExecutionUnavailable, match="identifier"):
        native.main.execution_context.run_process(ProcessRequest(
            (sys.executable, "-I", "-S", "-c", "print('bad')"), project, dict(os.environ),
            env_overrides={"-S/bin/echo INJECTED #": "1"},
        ))

    script = (
        "import hashlib, os, sys, time\n"
        "assert hashlib.sha256(os.environ['GITHUB_TOKEN'].encode()).hexdigest() == sys.argv[1]\n"
        "print('ready', flush=True)\n"
        "time.sleep(5)\n"
    )
    proc = native.main.execution_context.run_process(ProcessRequest(
        (sys.executable, "-I", "-S", "-c", script, digest), project, dict(os.environ),
        stdout=sp.PIPE, stderr=sp.PIPE,
        env_overrides={"GITHUB_TOKEN": secret},
    ))
    try:
        assert proc.stdout.readline().strip() == b"ready"
        ps_bin = shutil.which("ps") or "/bin/ps"
        ps_cmd = [ps_bin, "-axww", "-o", "args="] if sys.platform == "darwin" else [ps_bin, "-eo", "args="]
        ps_args = sp.check_output(ps_cmd, text=True, errors="replace")
        assert secret not in ps_args
        if sys.platform == "darwin":
            supervisor_env = sp.check_output([ps_bin, "eww", "-p", str(proc.pid)], text=True, errors="replace")
            assert secret not in supervisor_env
            plist_text = proc._process._plist.read_text(encoding="utf-8", errors="replace")
            assert secret not in plist_text
    finally:
        cleanup_process(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def test_startup_rejects_cwd_inside_private_virgo_dir(broker_session, project):
    from virgo.execution import ExecutionUnavailable

    private_dir = project.parent / "home"
    nested = private_dir / "nested-cwd"
    nested.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ExecutionUnavailable, match="private Virgo storage"):
        broker_session(cwd=nested, preset="workspace-edit", extensions=False, refresh_models=False)


def test_native_sandbox_runs_asyncio_event_loop(native, project):
    """VER-06: asyncio programs (requiring AF_UNIX SOCK_STREAM socketpair) run inside the sandbox."""
    source = (
        "import asyncio\n"
        "async def main():\n"
        "    await asyncio.sleep(0)\n"
        "    return 'asyncio-ok'\n"
        "print(asyncio.run(main()))\n"
    )
    result = run(native, python_command(native, source))
    assert not result.is_error and "asyncio-ok" in result.text(), result.text()
