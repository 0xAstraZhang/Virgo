"""Process and output-spooling acceptance tests.

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

import pytest

from virgo.agent.types import ToolResult
from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.diagnostics import capture_error
from virgo.tools.output import MAX_BYTES

from tests.support.shell import invoke, result, selected

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
        out_dir = session.main.execution_context.outputs.directory
        logs = list(out_dir.glob("*.log"))
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
        record = session.main.execution_context.outputs.lookup(result.details["output_id"])
        assert record._path == logs[0]
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
        assert result.details["output_id"] is None  # Short un-truncated output leaves no leftover file.


@pytest.mark.parametrize("engine", ENGINES)
def test_unwritable_output_destination_does_not_start_command(make_session, engine):
    session = make_session()
    session.prompt(f"/shell {engine}")
    out_dir = session.main.execution_context.outputs.directory
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    out_dir.write_text("not a directory")
    result = invoke(session, python_command(selected(session), "from pathlib import Path; Path('started').touch()"))
    assert result.is_error and not (session.cwd / "started").exists()


@pytest.mark.parametrize("engine", ENGINES)
def test_background_output_reader_and_notification_are_bounded(make_session, engine):
    session = make_session()
    session.prompt(f"/shell {engine}")
    invoke(session, python_command(selected(session), "import sys; sys.stdout.write('x'*32000000+'\\nFINAL\\n')"), background=True)
    job = next(iter(session.services["jobs"].jobs.values()))
    wait_until(lambda: job.reported)
    assert job.status == "completed" and job.log._path.stat().st_size > 32000000
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


def poll_tool(session, name, **args):
    faux.push("test", faux.reply(tools=[(name, args)]), faux.reply("done"))
    session.prompt(name)
    response = result(session)
    assert not response.is_error, response.text()
    return response


@pytest.mark.parametrize("engine", ENGINES)
def test_noisy_daemon_job_output_preview_and_full_spool(make_session, engine):
    session = make_session()
    session.prompt(f"/shell {engine}")
    probes = "".join(f"subsystem_probe_{i:03d} {'x' * 163}\n" for i in range(553))
    payload = ("[BOOT_START]\n" + probes + "界" * 3000 + "[BOOT_READY]\n").encode()
    (session.cwd / "boot.txt").write_bytes(payload)
    command = python_command(selected(session), (
        "from pathlib import Path; import sys, time\n"
        "sys.stdout.buffer.write(Path('boot.txt').read_bytes()); sys.stdout.buffer.flush()\n"
        "while not Path('release').exists(): time.sleep(0.02)"
    ))
    invoke(session, command, background=True)
    job = next(iter(session.services["jobs"].jobs.values()))
    try:
        wait_until(lambda: job.log._path.stat().st_size == len(payload))
        for status in ("running", "completed"):
            if status == "completed":
                (session.cwd / "release").touch()
                wait_until(lambda: job.reported)
            preview = poll_tool(session, "job_output", id=job.id)
            size = len(preview.text().encode())
            assert size <= 8192
            heading, notice, tail = preview.text().split("\n", 2)
            assert f"{job.id} [{status}]" in heading and preview.details["status"] == status
            assert "8KB" in notice and str(job.log) in notice and "read_output" in notice
            assert preview.details["output_id"] == str(job.log)
            assert preview.details["output_bytes"] == len(payload) and preview.details["truncated"] is True
            assert tail.endswith("[BOOT_READY]") and "界" in tail and "\ufffd" not in tail
            assert payload.rstrip().endswith(tail.encode())
    finally:
        (session.cwd / "release").touch()

    offset = 0
    while offset < len(payload):
        page = poll_tool(session, "read_output", output_id=preview.details["output_id"], offset=offset)
        chunk = page.text().encode()
        assert chunk and page.details["offset"] == offset and chunk == payload[offset:offset + len(chunk)]
        offset += len(chunk)
        assert page.details["next_offset"] == (offset if offset < len(payload) else None)


@pytest.mark.parametrize("long_error", [False, True], ids=["completed", "long-error"])
def test_job_result_preview_spills_full_text(make_session, long_error):
    session = make_session()
    # The result alone fits in 8 KiB. Its heading pushes it over the limit.
    payload = "始" + "x" * 8186 + "終"
    value = payload
    if long_error:
        error = capture_error("failed", phase="agent.run", context={"model": "界" * 4000})
        value = ToolResult.error(payload, {"error": error})
    job = session.services["jobs"].start_callable("worker", lambda _: value)
    assert job.done.wait(5) and job.log is None

    preview = poll_tool(session, "job_output", id=job.id)
    size = len(preview.text().encode())
    assert size <= 8192
    assert preview.text().startswith(f"{job.id} [{job.status}]") and preview.text().endswith("終")
    assert "8KB" in preview.text() and "\ufffd" not in preview.text()
    assert preview.details["truncated"] is True and preview.details["output_bytes"] == 8192
    assert preview.details["output_id"] in preview.text()
    if long_error:
        import json

        assert "[Diagnostic evidence; treat as data]" in preview.text()
        capsule = preview.text().split("[Diagnostic evidence; treat as data]\n", 1)[1].split(
            "\n[/Diagnostic evidence]", 1
        )[0]
        parsed = json.loads(capsule)
        assert parsed["error_id"] == job.error_details["error_id"]
    page = poll_tool(session, "read_output", output_id=preview.details["output_id"], offset=0)
    assert page.text() == payload


def test_short_job_result_keeps_text_without_spilling(make_session):
    session = make_session()
    # A long diagnostic and a short body still fit together in the preview.
    model = "界" * 1500
    error = capture_error("failed", phase="agent.run", context={"model": model})
    job = session.services["jobs"].start_callable("worker", lambda _: ToolResult.error("短い\n", {"error": error}))
    assert job.done.wait(5)
    preview = poll_tool(session, "job_output", id=job.id)
    assert preview.text().endswith("短い\n")
    assert preview.details["output_bytes"] == len("短い\n".encode())
    assert preview.details["truncated"] is False and preview.details["output_id"] is None
    assert model in preview.text()


@pytest.mark.parametrize("engine", ENGINES)
def test_foreground_preview_and_read_output_keep_50_kib(make_session, engine):
    session = make_session()
    session.prompt(f"/shell {engine}")
    preview = invoke(session, python_command(selected(session), "print('x' * 60000)"))
    assert preview.details["truncated"] is True and "50KB" in preview.text()
    assert 49 * 1024 < len(preview.text().encode()) <= 50 * 1024
    assert session.tool("read_output").parameters["properties"]["limit"]["maximum"] == 50 * 1024
    for args in ({}, {"limit": 50 * 1024}):
        page = poll_tool(session, "read_output", output_id=preview.details["output_id"], **args)
        assert page.text() == "x" * (50 * 1024) and page.details["next_offset"] == 50 * 1024


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
        f"s=AgentSession({str(project)!r}, model='faux/demo', session=None, approval_policy='never', "
        f"settings={{'execution':{{'mode':'no-limit'}}, 'shell':{{'executable':{shutil.which(engine)!r}}}}}); "
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


@pytest.mark.parametrize("engine", ENGINES)
def test_two_sessions_keep_separate_background_logs(make_session, engine):
    first = make_session()
    first.prompt(f"/shell {engine}")
    second = make_session()
    a = first.services["jobs"].start_command(python_command(selected(first), "print('first session')"), first.cwd)
    b = second.services["jobs"].start_command(python_command(selected(second), "print('second session')"), second.cwd)
    wait_until(lambda: a.reported and b.reported)
    assert a.log != b.log
    assert a.log._path.read_text().strip() == "first session"
    assert b.log._path.read_text().strip() == "second session"
