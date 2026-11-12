"""Process/log acceptance tests written before the runtime implementation.

Failures to cover: output retained in RAM or missing before exit; huge single lines;
invalid/split UTF-8; missing final bytes; cancellation/timeout output loss; failed log
creation starting a command anyway; descendants surviving parent exit, job cancellation,
session shutdown or supervisor crash; inherited job handles preventing kill-on-close;
concurrent jobs affecting each other; resource leakage across repeated commands.
These drive real sessions/shells/processes. Windows-specific assertions require Windows.
"""
from __future__ import annotations

import base64
import gc
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.tools.truncate import MAX_BYTES

from .test_shell_selection_e2e import invoke, selected

ENGINES = [n for n in ("pwsh", "powershell") if shutil.which(n)] if os.name == "nt" else ["bash"]


def python_command(shell, code):
    payload = base64.b64encode(code.encode()).decode()
    command = f"import base64;exec(base64.b64decode('{payload}'))"
    if shell.tool_name == "powershell":
        return "& '" + sys.executable.replace("'", "''") + "' -c '" + command.replace("'", "''") + "'"
    return shlex.join([sys.executable, "-c", command])


def wait_until(predicate, seconds=15):
    end = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < end, "process did not reach expected state"
        time.sleep(0.02)


@pytest.mark.parametrize("engine", ENGINES)
def test_foreground_spools_before_exit_with_bounded_memory(make_session, engine):
    session = make_session()
    session.prompt(f"/shell {engine}")
    size = 32 * 1024 * 1024
    command = python_command(selected(session), (
        "import sys,time; from pathlib import Path; "
        "sys.stdout.buffer.write(b'BEGIN\\n'); "
        f"sys.stdout.buffer.write(b'x'*{size}); "
        "sys.stdout.buffer.write('\\n中文尾部\\n'.encode()); sys.stdout.buffer.flush(); "
        "Path('ready').write_text('yes'); "
        "\nwhile not Path('release').exists(): time.sleep(0.02)\n"
        "sys.stdout.buffer.write(b'FINAL\\n'); sys.stdout.buffer.flush()"
    ))
    results = []
    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    worker = threading.Thread(target=lambda: results.append(invoke(session, command)), daemon=True)
    worker.start()
    try:
        wait_until(lambda: (session.cwd / "ready").exists())
        logs = list((session.cwd / ".virgo" / "outputs").glob("*.log"))
        assert len(logs) == 1, "output must exist on disk while the command is still running"
        assert logs[0].stat().st_size >= size
        assert tracemalloc.get_traced_memory()[1] - baseline < 8 * 1024 * 1024
        (session.cwd / "release").touch()
        worker.join(15)
        assert not worker.is_alive()
        result = results[0]
        assert not result.is_error and "FINAL" in result.text() and "中文尾部" in result.text()
        assert len(result.text().encode()) < MAX_BYTES + 1024
        assert result.details["truncated"] is True
        assert Path(result.details["output_path"]) == logs[0]
        with logs[0].open("rb") as log:
            assert log.read(6) == b"BEGIN\n"
            log.seek(-6, 2)
            assert log.read() == b"FINAL\n"
        assert tracemalloc.get_traced_memory()[1] - baseline < 8 * 1024 * 1024
    finally:
        tracemalloc.stop()
        session.abort()
        (session.cwd / "release").touch()
        worker.join(15)


@pytest.mark.parametrize("engine", ENGINES)
def test_output_bytes_and_tail_are_preserved_on_failure_and_timeout(make_session, engine):
    session = make_session()
    session.prompt(f"/shell {engine}")
    for suffix, expected in (("sys.exit(7)", "ok"), ("time.sleep(20)", "timeout")):
        command = python_command(selected(session), (
            "import sys,time; sys.stdout.buffer.write(b'\\xff\\xe4'); sys.stdout.buffer.flush(); "
            "time.sleep(0.01); sys.stdout.buffer.write(b'\\xb8\\xad\\nTAIL\\n'); sys.stdout.buffer.flush(); " + suffix
        ))
        result = invoke(session, command, timeout=3)
        assert result.is_error and result.details["status"] == expected
        assert "中\nTAIL" in result.text()
        assert Path(result.details["output_path"]).read_bytes() == b'\xff\xe4\xb8\xad\nTAIL\n'


@pytest.mark.parametrize("engine", ENGINES)
def test_unwritable_output_destination_does_not_start_command(make_session, engine):
    session = make_session()
    session.prompt(f"/shell {engine}")
    root = session.cwd / ".virgo"
    root.mkdir(exist_ok=True)
    (root / "outputs").write_text("not a directory")
    result = invoke(session, python_command(selected(session), "from pathlib import Path; Path('started').touch()"))
    assert result.is_error and not (session.cwd / "started").exists()


@pytest.mark.parametrize("engine", ENGINES)
def test_background_output_reader_and_notification_are_bounded(make_session, engine):
    session = make_session()
    session.prompt(f"/shell {engine}")
    invoke(session, python_command(selected(session), "import sys; sys.stdout.write('x'*32000000+'\\nFINAL\\n')"), background=True)
    job = next(iter(session.services["jobs"].jobs.values()))
    wait_until(lambda: job.reported)
    assert job.status == "completed" and job.log.stat().st_size > 32000000
    gc.collect()
    tracemalloc.start()
    try:
        faux.push("test", faux.reply(tools=[("job_output", {"id": job.id})]), faux.reply("done"))
        session.prompt("show the job output")
        result = next(m for m in reversed(session.main.messages) if isinstance(m, ToolResultMessage))
        assert "FINAL" in result.text() and "truncated" in result.text().lower()
        assert tracemalloc.get_traced_memory()[1] < 8 * 1024 * 1024
    finally:
        tracemalloc.stop()


def tree_command(session, *, parent_exits=False):
    child = "import time; from pathlib import Path; time.sleep(3); Path('orphan').touch(); time.sleep(30)"
    code = ("import subprocess,sys,time; from pathlib import Path; "
            f"p=subprocess.Popen([sys.executable,'-c',{child!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            "Path('child-pid').write_text(str(p.pid)); " + ("" if parent_exits else "time.sleep(30)"))
    return python_command(selected(session), code)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object acceptance")
@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("finish", ["parent-exit", "kill", "shutdown", "timeout"])
def test_windows_job_owns_children_for_every_completion_path(make_session, engine, finish):
    session = make_session()
    session.prompt(f"/shell {engine}")
    command = tree_command(session, parent_exits=finish == "parent-exit")
    invoke(session, command, background=True, **({"timeout": 2} if finish == "timeout" else {}))
    job = next(iter(session.services["jobs"].jobs.values()))
    wait_until(lambda: (session.cwd / "child-pid").exists())
    if finish == "shutdown":
        session.shutdown()
    elif finish == "kill":
        faux.push("test", faux.reply(tools=[("job_kill", {"id": job.id})]), faux.reply("done"))
        session.prompt("stop")
    wait_until(lambda: job.reported)
    time.sleep(3.2)
    assert not (session.cwd / "orphan").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows kill-on-close acceptance")
@pytest.mark.parametrize("engine", ENGINES)
def test_windows_supervisor_crash_cleans_tree_without_inherited_job_handle(project, engine):
    code = (
        "import os,time; from pathlib import Path; "
        "from virgo.harness.session import AgentSession; "
        f"s=AgentSession({str(project)!r}, model='faux/demo', session=None, permission_mode='accept-all', "
        f"settings={{'shell':{{'executable':{shutil.which(engine)!r}}}}}); "
        "shell=s.services['shell'].current; "
    )
    child = "import time; from pathlib import Path; Path('started').touch(); time.sleep(3); Path('orphan').touch(); time.sleep(30)"
    from virgo.tools.shell import probe_shell
    command = python_command(probe_shell(engine), child)
    code += (f"s.services['jobs'].start_command({command!r}, s.cwd); "
             "\nwhile not (s.cwd/'started').exists(): time.sleep(0.02)\n"
             "os._exit(0)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, error = proc.communicate(timeout=20)
        assert proc.returncode == 0, (out, error)
        assert (project / "started").exists()
        time.sleep(3.2)
        assert not (project / "orphan").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle lifetime acceptance")
def test_windows_repeated_commands_release_handles_and_isolate_jobs(make_session):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    def count():
        value = wintypes.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value))
        return value.value
    session = make_session()
    before = count()
    for _ in range(20):
        assert not invoke(session, "Write-Output done").is_error
    gc.collect()
    assert count() - before < 12
    jobs = session.services['jobs']
    first = jobs.start_command("Start-Sleep 20", session.cwd)
    second = jobs.start_command("Start-Sleep 1; Write-Output independent", session.cwd)
    jobs.kill(first)
    wait_until(lambda: first.reported and second.reported)
    assert first.status == "killed" and second.status == "completed"
    assert "independent" in jobs.output(second)
