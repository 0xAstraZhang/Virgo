"""Broker file boundaries and required native platform acceptance.

Native cases must launch a payload before counting as isolation evidence.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
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
        settings={"execution": {"mode": "workspace-edit"}}, refresh_models=False
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
        settings={"execution": {"mode": "workspace-edit"}},
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
        settings={"execution": {"mode": "workspace-edit"}},
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
def test_sandbox_file_tool_rejects_path_replacement_after_permission_check(
    broker_session, project, monkeypatch, tool
):
    """A workspace entry changed after permission review must not redirect I/O.

    Failure modes: the permission gate checks a normal file; a concurrent
    workspace writer replaces it while the Core tool waits for its existing
    file lock; write/edit follows the replacement and changes a sibling file.
    No permission or execution function is replaced in this assembled scenario.
    """
    from virgo.tools import fs as fs_mod

    target = project / "ordinary.txt"
    outside = project.parent / "outside-file.txt"
    target.write_text("original\n")
    outside.write_text("original\n")
    session = broker_session(
        settings={"execution": {"mode": "workspace-edit"}},
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

    original_lock = fs_mod.file_lock
    reached = threading.Event()

    def hooked_lock(path):
        lock = original_lock(path)
        if path == target:
            reached.set()
        return lock

    monkeypatch.setattr(fs_mod, "file_lock", hooked_lock)

    # Wait for the real executor, after both permission checkpoints. Holding
    # its normal per-file lock makes the filesystem change deterministic.
    with original_lock(target):
        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        assert reached.wait(10), "The Core file executor did not reach the locked operation"
        target.unlink()
        target.symlink_to(outside)
    worker.join(10)
    assert not worker.is_alive()
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
        settings={"execution": {"mode": "workspace-edit"}},
        refresh_models=False,
        extensions=False,
    )
    # Linux can create absent metadata names. Reproduce the state after such a
    # payload without requiring this broker-data-flow check to launch a backend.
    (project / "AGENTS.md").symlink_to(secret)
    if route == "rebuild":
        session.prompt("/shell auto")
        faux.script("test", faux.reply("done"))
        session.prompt("Check project context")
    else:
        api = ExtensionAPI(session, "sandbox-context")
        child = api.create_runtime("context-child", "subagent", parent=session.main)
        try:
            faux.script("test", faux.reply("done"))
            child.agent.prompt("Check project context")
        finally:
            api.release_runtime(child)
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
        settings={"execution": {"mode": "workspace-edit"}},
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


def test_native_existing_metadata_is_readonly(native, project):
    """Unapproved .virgo stays read-only in Shell while .git and AGENTS.md are open for normal workflow."""
    (project / ".virgo").mkdir()
    (project / ".virgo" / "settings.json").write_text("original")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("original")
    (project / "AGENTS.md").write_text("original")

    virgo_res = run(native, python_command(native, "from pathlib import Path; Path('.virgo/settings.json').write_text('bad')"))
    assert virgo_res.is_error and (project / ".virgo" / "settings.json").read_text() == "original"

    for name in (".git/config", "AGENTS.md"):
        ok_res = run(native, python_command(native, f"from pathlib import Path; Path({name!r}).write_text('updated')"))
        assert not ok_res.is_error and (project / name).read_text() == "updated"


@pytest.mark.parametrize("location", ["outside", "metadata"])
def test_native_cannot_create_hardlink_from_readonly_source(native, project, location):
    """A clean launch cannot mutate a read-only runtime or .virgo file through hardlink creation."""
    with tempfile.TemporaryDirectory(prefix="virgo-link-source-", dir=sys.prefix) as directory:
        source = (Path(directory) / "outside.txt" if location == "outside" else
                  project / ".virgo" / "config")
        source.parent.mkdir(exist_ok=True)
        source.write_text("original")
        result = run(native, python_command(native,
            f"import os; from pathlib import Path; os.link({str(source)!r}, 'new-alias.txt'); "
            "Path('new-alias.txt').write_text('changed')"
        ))
        assert source.read_text() == "original", result.text()
        assert result.is_error and not (project / "new-alias.txt").exists(), result.text()


@pytest.mark.parametrize("finish", ["exit", "timeout", "cancel"])
def test_native_descendant_process_group_is_reaped_with_command(native, project, finish):
    """Command completion must reap same-process-group descendants and release scratch.

    Failure modes: a same-group child or grandchild ignoring SIGTERM survives
    normal exit, timeout or cancellation; cleanup removes private scratch while
    descendants still hold workspace writes; timeout/cancellation is mislabeled.
    On Linux, the PID namespace also reaps detached (setsid) descendants.
    """
    ready = project / "descendant-ready"
    late = project / "descendant-late-write"
    scratch_file = project / "command-scratch-path"
    grandchild_source = (
        "import signal, time; from pathlib import Path; "
        "signal.alarm(8); signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('descendant-ready').write_text('ready'); "
        "time.sleep(2.5); Path('descendant-late-write').write_text('survived')"
    )
    child_source = (
        "import signal, subprocess, sys; "
        "signal.alarm(10); "
        f"subprocess.Popen([sys.executable, '-I', '-S', '-c', {grandchild_source!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait()"
    )
    source = (
        "import os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
        "signal.alarm(12)\n"
        "Path('command-scratch-path').write_text(os.environ['TMPDIR'])\n"
        f"subprocess.Popen([sys.executable, '-I', '-S', '-c', {child_source!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "while not Path('descendant-ready').exists():\n"
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
            while worker.is_alive() and not ready.exists():
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
    assert ready.exists(), result.text()
    expected = {"exit": "ok", "timeout": "timeout", "cancel": "aborted"}[finish]
    assert result.details["status"] == expected, result.text()
    assert result.is_error == (finish != "exit")
    assert result.details["boundary"]["cleanup"] == "executor_completed"
    deadline = time.monotonic() + 3.0
    while not late.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not late.exists(), "A same-group descendant retained write access after command completion"

    if sys.platform == "linux" and finish == "exit":
        linux_ready = project / "linux-detached-ready"
        linux_late = project / "linux-detached-late-write"
        linux_scratch = project / "linux-detached-scratch-path"
        detached_child = (
            "import signal, time; from pathlib import Path; "
            "signal.alarm(8); signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "Path('linux-detached-ready').write_text('ready'); "
            "time.sleep(2); Path('linux-detached-late-write').write_text('survived')"
        )
        detached_parent = (
            "import os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
            "signal.alarm(10)\n"
            "Path('linux-detached-scratch-path').write_text(os.environ['TMPDIR'])\n"
            f"subprocess.Popen([sys.executable, '-I', '-S', '-c', {detached_child!r}], "
            "start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "while not Path('linux-detached-ready').exists():\n"
            "    time.sleep(0.02)\n"
        )
        linux_result = run(native, python_command(native, detached_parent), timeout=15)
        assert not linux_result.is_error and linux_result.details["status"] == "ok", linux_result.text()
        assert linux_result.details["boundary"]["cleanup"] == "executor_completed"
        assert linux_ready.exists(), linux_result.text()
        assert linux_scratch.exists() and not Path(linux_scratch.read_text()).exists()
        deadline = time.monotonic() + 2.5
        while not linux_late.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not linux_late.exists(), "Linux PID namespace failed to reap a detached child"


def test_native_posix_spawn_preserves_common_use_and_reaps_child(native, project):
    """Ordinary posix_spawn must work and same-group spawned children must be reaped.

    Failure modes: sandbox policy blocks ordinary posix_spawn; a spawned child
    that ignores SIGTERM retains workspace writes after command completion.
    On Linux, posix_spawn with setpgroup=0 is also reaped by the PID namespace.
    """
    normal_source = (
        "import signal; from pathlib import Path; signal.alarm(8); "
        "Path('ordinary-posix-spawn').write_text('spawned')"
    )
    child_source = """import signal, time
from pathlib import Path
signal.alarm(8)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('spawn-child-ready').write_text('ready')
time.sleep(2.5)
Path('spawn-child-late-write').write_text('survived')
"""
    source = f"""import os, signal, sys, time
from pathlib import Path
signal.alarm(8)
pid = os.posix_spawn(sys.executable, [sys.executable, '-I', '-S', '-c', {normal_source!r}], os.environ)
assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 0
actions = [(os.POSIX_SPAWN_OPEN, fd, os.devnull, os.O_RDWR, 0) for fd in range(3)]
os.posix_spawn(sys.executable, [sys.executable, '-I', '-S', '-c', {child_source!r}],
               os.environ, file_actions=actions)
deadline = time.monotonic() + 5
while not Path('spawn-child-ready').exists():
    assert time.monotonic() < deadline, 'The spawned child did not become ready'
    time.sleep(0.02)
"""
    result = run(native, python_command(native, source), timeout=10)
    assert not result.is_error and result.details["status"] == "ok", result.text()
    assert result.details["boundary"]["cleanup"] == "executor_completed"
    assert (project / "ordinary-posix-spawn").read_text() == "spawned"
    assert (project / "spawn-child-ready").exists(), result.text()
    late = project / "spawn-child-late-write"
    deadline = time.monotonic() + 3.0
    while not late.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not late.exists(), "A same-group posix_spawn child retained workspace access after completion"

    if sys.platform == "linux":
        detached_source = """import signal, time
from pathlib import Path
signal.alarm(8)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('spawn-detached-ready').write_text('ready')
time.sleep(2)
Path('spawn-detached-late-write').write_text('survived')
"""
        linux_source = f"""import os, signal, sys, time
from pathlib import Path
signal.alarm(8)
actions = [(os.POSIX_SPAWN_OPEN, fd, os.devnull, os.O_RDWR, 0) for fd in range(3)]
os.posix_spawn(sys.executable, [sys.executable, '-I', '-S', '-c', {detached_source!r}],
               os.environ, file_actions=actions, setpgroup=0)
deadline = time.monotonic() + 5
while not Path('spawn-detached-ready').exists():
    assert time.monotonic() < deadline, 'The detached posix_spawn child did not become ready'
    time.sleep(0.02)
"""
        linux_result = run(native, python_command(native, linux_source), timeout=10)
        assert not linux_result.is_error and linux_result.details["status"] == "ok", linux_result.text()
        assert linux_result.details["boundary"]["cleanup"] == "executor_completed"
        assert (project / "spawn-detached-ready").exists(), linux_result.text()
        detached_late = project / "spawn-detached-late-write"
        deadline = time.monotonic() + 2.5
        while not detached_late.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not detached_late.exists(), "A detached posix_spawn child retained workspace access on Linux"


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
        settings={"plugins": {"background": {"enabled": True}},
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
        settings={"execution": {"mode": "workspace-edit"}},
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
        settings={"plugins": {"background": {"enabled": True}},
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


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX output directory symlinks")
@pytest.mark.parametrize("background", [False, True])
def test_sandbox_rejects_external_output_symlink_before_spooling(broker_session, project, background):
    """Broker OutputStore must not follow a symlinked outputs directory outside VIRGO_HOME."""
    outside = project.parent / "outside-outputs"
    outside.mkdir()
    outputs_root = Path(os.environ["VIRGO_HOME"]) / "outputs"
    if outputs_root.exists():
        shutil.rmtree(outputs_root)
    outputs_root.symlink_to(outside, target_is_directory=True)
    session = broker_session(
        settings={"plugins": {"background": {"enabled": True}},
                  "execution": {"mode": "workspace-edit"}},
        refresh_models=False, extensions=False,
    )
    result = run(session, "echo sandbox-output", background=background)
    assert result.is_error and result.details["status"] == "execution_unavailable"
    assert result.details["boundary"]["reason"] == "invalid_output_dir"
    assert list(outside.iterdir()) == [], "Symlinked output roots must be rejected before mkdir or open"
    if background:
        assert not session.services["jobs"].jobs


def test_native_protected_symlink_cannot_add_a_host_read_mount(native, project):
    """A workspace or .virgo symlink pointing into private credential storage remains unreadable."""
    secret = native.paths.user / "outside-sandbox-secret.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("credential-that-must-stay-outside")
    (project / "secret-link.txt").symlink_to(secret)
    (project / ".virgo").symlink_to(secret)
    for rel in ("secret-link.txt", ".virgo"):
        result = run(native, python_command(native, f"from pathlib import Path; print(Path({rel!r}).read_text())"))
        assert result.is_error, result.text()
        assert "credential-that-must-stay-outside" not in result.text()
    assert secret.read_text() == "credential-that-must-stay-outside"


def test_native_existing_metadata_symlink_cannot_be_replaced(native, project):
    """Symlinked .virgo protects its real target without breaking sessions."""
    target = project / "virgo-meta"
    target.mkdir()
    settings_file = target / "settings.json"
    settings_file.write_text('{"theme": "light"}')
    link = project / ".virgo"
    link.symlink_to(target, target_is_directory=True)
    if sys.platform == "darwin":
        result = run(native, python_command(native,
            "from pathlib import Path; p=Path('.virgo'); p.unlink(); p.mkdir()"
        ))
        assert result.is_error, result.text()
        assert link.is_symlink()
        assert settings_file.read_text() == '{"theme": "light"}'
    else:
        read_res = run(native, python_command(native,
            "from pathlib import Path; print(Path('.virgo/settings.json').read_text())"
        ))
        assert not read_res.is_error and '"light"' in read_res.text(), read_res.text()
    write_res = run(native, python_command(native,
        "from pathlib import Path; Path('.virgo/settings.json').write_text('replaced')"
    ))
    assert write_res.is_error, write_res.text()
    assert link.is_symlink()
    assert settings_file.read_text() == '{"theme": "light"}'


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
    import virgo.harness.resource_approvals as approvals

    def broken(api, executor):
        raise RuntimeError('required provider setup failed')

    monkeypatch.setattr(approvals, 'SandboxExecutionProvider', broken)
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
        settings={"execution": {"mode": "workspace-edit"}},
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


@pytest.mark.parametrize('mode,relative', [('read-only', 'ordinary.txt'), ('workspace-write', '.virgo/config')])
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
@pytest.mark.parametrize('network_allowed', [False, True])
def test_native_independent_permission_combinations(native, project, short_workspace, read_only, network_allowed):
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
    private_sock_dir = short_workspace / 'priv-sock'
    private_sock_dir.mkdir()
    before = {path: (path.stat().st_dev, path.stat().st_ino, path.read_text()) for path in (inside, outside)}

    from dataclasses import replace
    from threading import Event

    from virgo.execution_policy import PathRule, PermissionPolicy
    from virgo.tools.bash import run_command

    context = native.main.execution_context
    policy = PermissionPolicy((
        *(rule for rule in context.policy.filesystem if rule.effect == "deny"),
        PathRule(private_sock_dir, "read", "deny"),
        PathRule(project, "read" if read_only else "write"),
    ), network_allowed)
    context = replace(context, resources=replace(context.resources, policy=policy))
    with socket.socket() as listener, socket.socket(socket.AF_UNIX) as host_socket:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        host_socket.bind(str(private_sock_dir / 'host.sock'))
        host_socket.listen()
        with socket.socket(socket.AF_UNIX) as control:
            control.connect(str(private_sock_dir / 'host.sock'))
            connected, _ = host_socket.accept()
            connected.close()
        source = f'''import os, socket
from pathlib import Path
def attempt(fn):
    try: fn(); return True
    except OSError: return False
assert attempt(lambda: Path('created').write_text('yes')) is {not read_only!r}
assert Path({str(extra / 'readable')!r}).read_text() == 'extra'
assert attempt(lambda: Path({str(extra / 'created')!r}).write_text('yes')) is False
assert attempt(lambda: Path({str(inside)!r}).write_text('changed')) is {not read_only!r}
assert attempt(lambda: Path({str(outside)!r}).write_text('changed')) is False
assert attempt(lambda: Path({str(secret)!r}).read_text()) is False
assert attempt(lambda: Path({str(secret)!r}).write_text('bad')) is False
assert attempt(lambda: socket.create_connection({listener.getsockname()!r}, 2).close()) is {network_allowed!r}
assert attempt(lambda: socket.socket(socket.AF_UNIX).connect({str(private_sock_dir / 'host.sock')!r})) is False
# Local AF_UNIX sockets inside writable $TMPDIR work in all modes.
local_sock = Path(os.environ['TMPDIR']) / 'local.sock'
with socket.socket(socket.AF_UNIX) as srv:
    srv.bind(str(local_sock))
    srv.listen(1)
    with socket.socket(socket.AF_UNIX) as cli:
        cli.connect(str(local_sock))
        conn, _ = srv.accept()
        conn.close()
print('independent dimensions checked')
'''
        output, code, status = run_command(python_command(native, source), project, 15, Event(),
                                           interpreter=selected(native), execution_context=context)
        host_socket.setblocking(False)
        with pytest.raises(BlockingIOError):
            host_socket.accept()
    assert code == 0 and status == "ok" and "independent dimensions checked" in output.text, output.text
    for path, allowed in ((inside, not read_only), (outside, False)):
        current = path.stat()
        original_device, original_inode, original_text = before[path]
        assert (current.st_dev, current.st_ino) == (original_device, original_inode)
        assert path.read_text() == ("changed" if allowed else original_text), path


def test_native_workspace_local_virtualenv_runs_and_protects_virgo(native, project):
    """A workspace-local .venv runs cleanly, allows package/venv writes in Workspace-Edit, and protects .virgo."""
    import subprocess

    environment = project / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)],
                   check=True, capture_output=True, text=True, timeout=30)
    (project / ".virgo").mkdir(exist_ok=True)
    virgo_cfg = project / ".virgo" / "settings.json"
    virgo_cfg.write_text('{"protected": true}')
    executable = environment / "bin" / "python"
    repository = Path(__file__).resolve().parents[2]
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
    command = shlex.join([sys.executable, "-I", "-S", "-c",
        "import os; from pathlib import Path; assert 'PYTHONPATH' not in os.environ; "
        "Path('ordinary.txt').write_text('allowed'); "
        "Path('.venv/installed_pkg.py').write_text('X = 1\\n'); print('venv-ready')"])
    result = call(shell.tool_name, {"command": command})
    assert not result.is_error, result.text()
    assert "venv-ready" in result.text()
    assert Path("ordinary.txt").read_text() == "allowed"
    assert Path(".venv/installed_pkg.py").read_text() == "X = 1\n"
    # .virgo remains protected when approval_policy='never'.
    command = shlex.join([sys.executable, "-I", "-S", "-c",
        "from pathlib import Path; Path('.virgo/settings.json').write_text('tampered')"])
    result = call(shell.tool_name, {"command": command})
    assert result.is_error, result.text()
    assert Path(".virgo/settings.json").read_text() == '{"protected": true}'
    result = call("write", {"path": ".virgo/settings.json", "content": "tampered"})
    assert result.is_error, result.text()
    assert Path(".virgo/settings.json").read_text() == '{"protected": true}'
finally:
    session.shutdown()
'''
    completed = subprocess.run([str(executable), "-c", source], cwd=project, env=env,
                               text=True, capture_output=True, timeout=45)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert virgo_cfg.read_text() == '{"protected": true}'


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
        settings={"execution": {"mode": "workspace-edit"}},
        refresh_models=False, extensions=False, trust_project_extensions=False,
    )
    assert session.services["execution"].name == "sandbox"
    if route == "project":
        assert session.services["shell"].current.executable != str(candidate)
    if route == "selection":
        assert not sentinel.exists()
        session.prompt(f"/shell {shlex.quote(str(candidate))}")
    assert not sentinel.exists(), "Shell discovery/selection executed an unisolated candidate"


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX executable shell discovery")
def test_untrusted_project_shell_is_stripped_in_no_limit_mode(make_session, project, monkeypatch):
    user_shell = shutil.which("bash") or shutil.which("sh")
    assert user_shell is not None
    directory = project / "candidate-shell"
    directory.mkdir()
    candidate = directory / "bash"
    sentinel = directory / "executed-on-host.txt"
    candidate.write_text(
        "#!/bin/sh\n"
        f"printf 'host probe executed\\n' >> {shlex.quote(str(sentinel))}\n"
        "printf '5.2.0'\n",
        encoding="utf-8",
    )
    candidate.chmod(0o755)

    monkeypatch.delenv("VIRGO_SHELL", raising=False)
    user_settings = Path(os.environ["VIRGO_HOME"]) / "settings.json"
    user_settings.write_text(
        json.dumps({"execution": {"mode": "no-limit"}, "shell": {"executable": user_shell}}),
        encoding="utf-8",
    )
    config = project / ".virgo"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"shell": {"executable": str(candidate)}}), encoding="utf-8")

    untrusted = make_session(
        settings={"execution": {"mode": "no-limit"}},
        refresh_models=False,
        extensions=False,
        trust_project_extensions=False,
    )
    assert not sentinel.exists(), "Untrusted project shell.executable ran on Host in no-limit mode"
    assert untrusted.services["shell"].current.executable == os.path.abspath(user_shell)
    assert untrusted.services["shell"].current.executable != str(candidate)
    warnings = "\n".join(untrusted.settings.migration_warnings)
    assert "Ignored untrusted project settings" in warnings and "shell" in warnings

    trusted = make_session(
        settings={"execution": {"mode": "no-limit"}},
        refresh_models=False,
        extensions=False,
        trust_project_extensions=True,
    )
    assert trusted.services["shell"].current is not None
    assert sentinel.exists()
    assert trusted.services["shell"].current.executable == os.path.abspath(candidate)


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


@pytest.mark.skipif(os.name != "posix", reason="Linux bwrap envelope generation")
def test_linux_bwrap_envelope_mounts_resolv_target_and_masks_private_paths(project, tmp_path, monkeypatch):
    """Linux bwrap mounts / read-only, binds /run/systemd/resolve target, and masks private dirs/files."""
    from virgo.sandbox import platform as sandbox

    entry = Path("/etc/resolv.conf")
    target = Path("/run/systemd/resolve/stub-resolv.conf")
    priv_dir = tmp_path / "fake-ssh"
    priv_dir.mkdir()
    priv_file = tmp_path / "fake-netrc"
    priv_file.write_text("machine example.com password secret")
    empty_file = tmp_path / "empty"
    empty_file.write_bytes(b"")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    original_exists, original_resolve = Path.exists, Path.resolve
    monkeypatch.setattr(Path, "exists", lambda p: True if p == entry else original_exists(p))
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda p, *a, **kw: target if p == entry else original_resolve(p, *a, **kw),
    )

    policy = sandbox.NativePolicy(
        project, False, True, (priv_dir.resolve(), priv_file.resolve()), (),
        read_only_paths=(project / ".virgo",),
    )
    args = policy._linux(
        Path("/usr/bin/bwrap"), scratch_dir, project, 99,
        session_cache=cache_dir, empty_mask_file=empty_file,
    )
    assert "--ro-bind" in args and "--info-fd" in args and "99" in args
    assert "--share-net" in args
    ro_mounts = [args[idx : idx + 3] for idx, val in enumerate(args) if val == "--ro-bind"]
    assert ["--ro-bind", "/", "/"] in ro_mounts
    assert ["--ro-bind", str(target), str(target)] in ro_mounts
    assert ["--ro-bind", str(empty_file), str(priv_file.resolve())] in ro_mounts
    tmpfs_mounts = [args[idx : idx + 2] for idx, val in enumerate(args) if val == "--tmpfs"]
    assert ["--tmpfs", str(priv_dir.resolve())] in tmpfs_mounts
    valid_perms_targets = {"--tmpfs", "--dir", "--file", "--bind-data", "--ro-bind-data"}
    for idx, val in enumerate(args):
        if val == "--perms":
            assert args[idx + 2] in valid_perms_targets, f"bwrap rejects --perms before {args[idx + 2]}"


def test_native_environment_denylist_strips_secrets_and_reuses_session_cache(native, project, monkeypatch):
    """Credential/injection env vars are stripped while proxy/toolchain vars and session cache persist."""
    stripped = [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
        "MY_CUSTOM_SECRET", "DB_PASSWORD", "SSH_AUTH_SOCK", "VIRGO_HOME", "LD_PRELOAD",
        "PYTHONPATH", "BASH_ENV",
    ]
    for key in stripped:
        monkeypatch.setenv(key, "must-not-leak")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8888")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("CARGO_NET_GIT_FETCH_WITH_CLI", "true")

    source1 = f"""import json, os
from pathlib import Path
assert [os.environ.get(k) for k in {stripped!r}] == [None] * {len(stripped)}
assert os.environ.get('HTTP_PROXY') == 'http://127.0.0.1:8888'
assert os.environ.get('HTTPS_PROXY') == 'http://127.0.0.1:8888'
assert os.environ.get('NO_PROXY') == 'localhost,127.0.0.1'
assert os.environ.get('CARGO_NET_GIT_FETCH_WITH_CLI') == 'true'
cache = Path(os.environ['XDG_CACHE_HOME'])
cache.mkdir(parents=True, exist_ok=True)
(cache / 'wheel-marker.txt').write_text('cached-across-commands')
print(json.dumps({{'cache': str(cache), 'tmp': os.environ['TMPDIR']}}))
"""
    res1 = run(native, python_command(native, source1))
    assert not res1.is_error, res1.text()
    info1 = json.loads(res1.text().strip().splitlines()[-1])
    assert not Path(info1["tmp"]).exists()

    source2 = """import json, os
from pathlib import Path
cache = Path(os.environ['XDG_CACHE_HOME'])
assert (cache / 'wheel-marker.txt').read_text() == 'cached-across-commands'
print(json.dumps({'cache': str(cache), 'tmp': os.environ['TMPDIR']}))
"""
    res2 = run(native, python_command(native, source2))
    assert not res2.is_error, res2.text()
    info2 = json.loads(res2.text().strip().splitlines()[-1])
    assert info2["cache"] == info1["cache"]
    assert info2["tmp"] != info1["tmp"]


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX native launch protocol")
@pytest.mark.parametrize("background", [False, True])
def test_wrapper_failure_before_ack_is_execution_unavailable_and_not_replayed(
    make_session, project, monkeypatch, background,
):
    """bwrap failing before writing --info-fd (or Seatbelt failing its probe) reports execution_unavailable."""
    from virgo.sandbox import native as dispatcher
    from virgo.sandbox import platform as sandbox

    launcher = project.parent / "reject_bwrap.py"
    attempts = project.parent / "launch-attempts.txt"
    launcher.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"with Path({str(attempts)!r}).open('a') as log:\n"
        "    log.write('launch\\n')\n"
        "sys.stderr.write('bwrap: setting up uid map: Permission denied\\n')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sandbox, "platform_backend", lambda: ("bubblewrap", Path(sys.executable)))
    monkeypatch.setattr(dispatcher, "platform_backend", sandbox.platform_backend)
    monkeypatch.setattr(
        sandbox.NativePolicy, "_linux",
        lambda _self, executable, _scratch, _cwd, info_fd, **_kw: [str(executable), str(launcher), str(info_fd)],
    )
    session = make_session(
        settings={"plugins": {"background": {"enabled": True}},
                  "execution": {"mode": "workspace-edit"}},
        approval="never",
        refresh_models=False, extensions=False,
    )
    result = run(session, "echo bad > payload-ran.txt", background=background)
    assert result.is_error and result.details["status"] == "execution_unavailable", result.text()
    assert result.details["boundary"]["payload_started"] is False
    assert not (project / "payload-ran.txt").exists()
    assert attempts.read_text().splitlines() == ["launch"], "Never retry a rejected payload"
    if background:
        assert not session.services["jobs"].jobs


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX native launch protocol")
@pytest.mark.parametrize("background", [False, True])
def test_missing_exec_acknowledgement_does_not_claim_no_effects(make_session, project, monkeypatch, background):
    """A timed-out --info-fd acknowledgement reports execution_failed with side_effects='possible' and no replay."""
    from virgo.sandbox import native as dispatcher
    from virgo.sandbox import platform as sandbox

    launcher = project.parent / "forward_payload.py"
    marker = project / "payload-ran.txt"
    launcher.write_text(
        "import os, sys\n"
        "# Close info_fd passed as argv[1] only after exec or keep pipe open while running payload\n"
        "os.execv(sys.argv[2], sys.argv[2:])\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sandbox, "platform_backend", lambda: ("bubblewrap", Path(sys.executable)))
    monkeypatch.setattr(dispatcher, "platform_backend", sandbox.platform_backend)
    monkeypatch.setattr(
        sandbox.NativePolicy, "_linux",
        lambda _self, executable, _scratch, _cwd, info_fd, **_kw: [str(executable), str(launcher), str(info_fd)],
    )
    delayed = False

    def lose_ack(*_args):
        nonlocal delayed
        wait_until(lambda: marker.exists() and marker.read_text() == "once", timeout=3)
        delayed = True
        return [], [], []

    monkeypatch.setattr(sandbox, "select", SimpleNamespace(select=lose_ack))
    monkeypatch.setattr(sandbox, "time", SimpleNamespace(
        monotonic=lambda: time.monotonic() + (10 if delayed else 0), sleep=time.sleep))
    session = make_session(
        settings={"plugins": {"background": {"enabled": True}},
                  "execution": {"mode": "workspace-edit"}},
        approval="never",
        refresh_models=False, extensions=False,
    )
    source = "from pathlib import Path\nwith Path('payload-ran.txt').open('a') as stream: stream.write('once')"
    result = run(session, python_command(session, source), background=background)
    assert marker.read_text() == "once"
    assert result.is_error and result.details.get("status") == "execution_failed", result.text()
    assert result.details["boundary"]["side_effects"] == "possible"


def test_macos_env_override_not_exposed_in_ps_and_option_name_rejected(native, project):
    """GITHUB_TOKEN override is never in process argv or broker env; invalid overrides are rejected."""
    import hashlib
    import shutil
    import subprocess as sp

    from virgo.execution import ExecutionUnavailable, HostProcess, ProcessRequest, cleanup_process

    secret = "ghp_virgo_env_override_secret_987654"
    digest = hashlib.sha256(secret.encode()).hexdigest()
    with pytest.raises(ExecutionUnavailable, match="identifier"):
        native.main.execution_context.run_process(ProcessRequest(
            (sys.executable, "-I", "-S", "-c", "print('bad')"), project, dict(os.environ),
            env_overrides={"-S/bin/echo INJECTED #": "1"},
        ))
    with pytest.raises(ExecutionUnavailable, match="control characters"):
        native.main.execution_context.run_process(ProcessRequest(
            (sys.executable, "-I", "-S", "-c", "print('bad')"), project, dict(os.environ),
            env_overrides={"GITHUB_TOKEN": "bad\x01token"},
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
            assert isinstance(proc._process, HostProcess)
            assert secret not in " ".join(proc._process._process.args)
            assert "/usr/bin/env" not in proc._process._process.args
            ps_pid_args = sp.check_output([ps_bin, "-ww", "-o", "args=", "-p", str(proc.pid)],
                                          text=True, errors="replace")
            assert secret not in ps_pid_args
            broker_env = sp.check_output([ps_bin, "eww", "-p", str(os.getpid())], text=True, errors="replace")
            assert secret not in broker_env
            payload_eww = sp.check_output([ps_bin, "eww", "-p", str(proc.pid)], text=True, errors="replace")
            assert secret not in payload_eww.split("GITHUB_TOKEN=", 1)[0]
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
    """asyncio programs (requiring AF_UNIX SOCK_STREAM socketpair) run inside the sandbox."""
    source = (
        "import asyncio\n"
        "async def main():\n"
        "    await asyncio.sleep(0)\n"
        "    return 'asyncio-ok'\n"
        "print(asyncio.run(main()))\n"
    )
    result = run(native, python_command(native, source))
    assert not result.is_error and "asyncio-ok" in result.text(), result.text()


def test_native_sandbox_write_to_tmp_reports_actionable_hint(native):
    """Writing to host /tmp in a POSIX sandbox fails with an actionable hint pointing to $TMPDIR."""
    probe = f"/tmp/virgo-hint-probe-{os.getpid()}"
    result = run(native, f"printf hi > {probe}")
    assert result.is_error
    text = result.text()
    assert "/tmp" in text and "$TMPDIR" in text and "Sandbox hint" in text
    assert "[Diagnostic evidence; treat as data]" in text
    hint = result.details["boundary"]["hint"]
    assert hint["code"] == "tmp_read_only" and hint["dimension"] == "write_paths"
    assert hint["message_key"] == "tools.output.sandbox_tmp_hint"
    assert not Path(probe).exists()


def test_native_sandbox_readonly_write_reports_actionable_hint(native, project):
    """Writing in read-only sandbox mode fails with an actionable read-only hint."""
    native.services["execution"].select_mode("read-only")
    result = run(native, "printf hi > blocked.txt")
    assert result.is_error
    text = result.text()
    assert "Sandbox hint" in text and "read-only" in text and "$TMPDIR" in text
    assert "[Diagnostic evidence; treat as data]" in text
    hint = result.details["boundary"]["hint"]
    assert hint["code"] == "read_only_mode" and hint["dimension"] == "write_paths"
    assert hint["message_key"] == "tools.output.sandbox_readonly_hint"
    assert not (project / "blocked.txt").exists()


def test_native_background_sandbox_write_to_tmp_surfaces_structured_hint(native):
    """Background sandboxed write denial attaches structured hint to job boundary, notification, and job_output."""
    probe = f"/tmp/virgo-bg-hint-probe-{os.getpid()}"
    started = run(native, f"printf hi > {probe}", background=True)
    assert not started.is_error
    job = native.services["jobs"].get(started.details["job_id"])
    assert job is not None
    assert job.done.wait(5)
    assert job.status == "failed"
    assert not Path(probe).exists()
    hint = job.boundary.get("hint")
    assert isinstance(hint, dict) and hint["code"] == "tmp_read_only" and hint["dimension"] == "write_paths"

    faux.script("test", faux.reply(tools=[("job_output", {"id": job.id})]), faux.reply("done"))
    native.prompt("Inspect failed background job")
    output = tool_results(native)[-1]
    assert not output.is_error
    assert output.details["boundary"]["hint"] == hint
    assert "[Diagnostic evidence; treat as data]" in output.text()
    assert "$TMPDIR" in output.text() and "Sandbox hint" in output.text()


def test_workspace_edit_allows_git_and_docs_while_confirming_virgo_and_shielding_credentials(native, project):
    """Workspace-Edit opens .git/AGENTS.md/CLAUDE.md, confirms .virgo writes, and hard-denies credentials."""
    private_secret = native.paths.user / "auth.json"
    private_secret.parent.mkdir(parents=True, exist_ok=True)
    private_secret.write_text('{"token": "secret"}')

    # Default RecordingUI denies prompts ('n'): .git, AGENTS.md, CLAUDE.md succeed without prompting;
    # .virgo/settings.json prompts and is denied; private auth.json is hard-denied without prompting.
    faux.script(
        "test",
        faux.reply(tools=[
            ("write", {"path": "AGENTS.md", "content": "# Repo guide\n"}),
            ("write", {"path": "CLAUDE.md", "content": "# Claude guide\n"}),
            ("write", {"path": ".git/config", "content": "[core]\n"}),
            ("write", {"path": ".virgo/settings.json", "content": "{}"}),
            ("read", {"path": str(private_secret)}),
            ("request_permissions", {
                "permissions": {"read_paths": [str(private_secret)]},
                "reason": "Attempt to escalate private Virgo credentials",
            }),
        ]),
        faux.reply("done"),
    )
    native.prompt("Update docs and git metadata, test .virgo confirmation, and verify credential hard-deny")
    agents_res, claude_res, git_res, virgo_res, priv_read, priv_req = tool_results(native)[-6:]
    assert not agents_res.is_error and (project / "AGENTS.md").read_text() == "# Repo guide\n"
    assert not claude_res.is_error and (project / "CLAUDE.md").read_text() == "# Claude guide\n"
    assert not git_res.is_error and (project / ".git/config").read_text() == "[core]\n"
    assert virgo_res.is_error and virgo_res.details["status"] == "resource_denied"
    assert virgo_res.details["boundary"]["dimension"] == "write_paths"
    assert not (project / ".virgo/settings.json").exists()
    assert priv_read.is_error and priv_read.details["boundary"]["dimension"] == "protected_path"
    assert priv_read.details["boundary"]["escalation_available"] is False
    assert priv_req.is_error and "Protected resources cannot be granted" in priv_req.text()
    # Only .virgo/settings.json triggered a confirmation prompt!
    assert len(native.ui.questions) == 1 and ".virgo" in native.ui.questions[0]

    # On macOS Seatbelt, unapproved Shell creation of .virgo/settings.json is also blocked.
    if sys.platform == "darwin":
        script = (
            "from pathlib import Path\n"
            "p = Path('.virgo/settings.json')\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "p.write_text('tampered')\n"
        )
        result = run(native, python_command(native, script))
        assert result.is_error, f"macOS Seatbelt must reject creating .virgo/settings.json: {result.text()}"
        assert not (project / ".virgo/settings.json").exists()

    # Approving 'y' on a subsequent turn allows writing .virgo/settings.json cleanly.
    native.ui.answers.append("y")
    faux.script(
        "test",
        faux.reply(tools=[("write", {"path": ".virgo/settings.json", "content": '{"theme": "dark"}\n'})]),
        faux.reply("done"),
    )
    native.prompt("Approve updating .virgo/settings.json")
    approved_virgo = tool_results(native)[-1]
    assert not approved_virgo.is_error, approved_virgo.text()
    assert (project / ".virgo/settings.json").read_text() == '{"theme": "dark"}\n'


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS /tmp -> /private/tmp symlink resolution")
def test_macos_tmp_symlink_grant_and_real_path_dialog(native, project):
    """Approving /tmp/<dir> resolves to /private/tmp/<dir> and works for Read and Shell."""
    with tempfile.TemporaryDirectory(prefix="virgo-tmp-", dir="/tmp") as raw_dir:
        raw_path = Path("/tmp") / Path(raw_dir).name
        real_path = raw_path.resolve()
        assert str(raw_path) != str(real_path)
        sample = real_path / "data.txt"
        sample.write_text("from-tmp")

        shell = selected(native).tool_name
        cmd = {"command": python_command(native, f"from pathlib import Path; print(Path({str(raw_path / 'data.txt')!r}).read_text())")}
        native.ui.answers.extend(["y", "y"])
        faux.script(
            "test",
            faux.reply(tools=[("read", {"path": str(raw_path / "data.txt")})]),
            faux.reply(tools=[("request_permissions", {
                "tool": shell, "arguments": cmd,
                "permissions": {"read_paths": [str(raw_path)]},
                "reason": "Read temporary fixture via /tmp path",
            })]),
            faux.reply(tools=[(shell, cmd)]),
            faux.reply("done"),
        )
        native.prompt("Read via /tmp symlink and run approved shell command")
        read_res, req_res, shell_res = tool_results(native)[-3:]
        assert not read_res.is_error and "from-tmp" in read_res.text(), read_res.text()
        assert not req_res.is_error, req_res.text()
        assert not shell_res.is_error and "from-tmp" in shell_res.text(), shell_res.text()
        # Both approval dialogs must display the resolved real path (/private/tmp/...).
        assert len(native.ui.questions) == 2
        assert all(str(real_path) in q for q in native.ui.questions)


def test_session_shell_write_grant_persists_in_session_until_revoked(native, project, tmp_path):
    """Session Shell grant ('y') allows multiple commands in the same session until revoked."""
    outside = tmp_path / "external-out"
    outside.mkdir()
    shell = selected(native).tool_name
    op1 = {"command": python_command(
        native,
        f"from pathlib import Path; Path({str(outside / 'stamp.txt')!r}).write_text('first')",
    )}
    op2 = {"command": python_command(
        native,
        f"from pathlib import Path; Path({str(outside / 'stamp.txt')!r}).write_text('second')",
    )}
    native.ui.answers.append("y")
    # request_permissions works without requiring tool/arguments and can be batched in the same turn!
    faux.script(
        "test",
        faux.reply(tools=[
            ("request_permissions", {
                "permissions": {"write_paths": [str(outside)]},
                "reason": "Write external output files in this session",
            }),
            (shell, op1),
        ]),
        faux.reply(tools=[(shell, op2)]),
        faux.reply("done"),
    )
    native.prompt("Grant session write access and run two shell writes")
    req_res, first_run, second_run = tool_results(native)[-3:]
    assert not req_res.is_error, req_res.text()
    assert not first_run.is_error, first_run.text()
    assert not second_run.is_error, second_run.text()
    assert (outside / "stamp.txt").read_text() == "second"
    assert len(native.ui.questions) == 1

    # Revoking clears session grants; a subsequent Shell write to outside fails.
    native.services["execution"].revoke()
    third_run = run(native, op1["command"])
    assert third_run.is_error
    assert (outside / "stamp.txt").read_text() == "second"


@pytest.mark.skipif(os.name != "posix", reason="Linux bwrap argument generation requires POSIX paths")
def test_linux_bwrap_wide_ro_and_explicit_tmp_grant(tmp_path):
    """Linux bwrap mounts / read-only by default and binds /tmp rw only when explicitly granted."""
    from virgo.sandbox.platform import NativePolicy

    ws = tmp_path / "ws"
    ws.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    ro_tmp_policy = NativePolicy(ws, False, False, (), (), read_only_paths=(ws / ".virgo",))
    args_ro = ro_tmp_policy._linux(Path("/usr/bin/bwrap"), scratch, ws, 3)
    mounts_ro = [args_ro[i : i + 3] for i, a in enumerate(args_ro) if a in {"--bind", "--ro-bind"}]
    assert ["--ro-bind", "/", "/"] in mounts_ro
    assert ["--bind", "/tmp", "/tmp"] not in mounts_ro

    rw_tmp_policy = NativePolicy(ws, False, False, (), ((Path("/tmp"), True),))
    args_rw = rw_tmp_policy._linux(Path("/usr/bin/bwrap"), scratch, ws, 3)
    mounts_rw = [args_rw[i : i + 3] for i, a in enumerate(args_rw) if a in {"--bind", "--ro-bind"}]
    assert ["--bind", "/tmp", "/tmp"] in mounts_rw

    # Symlinked .virgo protects non-private target without exposing private roots.
    private_dir = tmp_path / "priv"
    private_dir.mkdir()
    private_file = private_dir / "secret.json"
    private_file.write_text("{}")
    inside_meta = ws / "virgo-meta"
    inside_meta.mkdir()
    (ws / ".virgo").symlink_to(inside_meta, target_is_directory=True)
    pol_with_sym = NativePolicy(ws, False, False, (private_dir.resolve(),), (), read_only_paths=(ws / ".virgo",))
    assert inside_meta.resolve() in pol_with_sym.protected_paths()
    (ws / ".virgo").unlink()
    (ws / ".virgo").symlink_to(private_file)
    assert private_file.resolve() not in pol_with_sym.protected_paths()
