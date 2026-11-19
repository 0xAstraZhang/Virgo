"""Shared native acceptance bodies; each platform owns its test identities."""
from tests.support.sandbox import python_command, run


def generic_process_stdio(native, project):
    """A non-Shell process exchanges separate protocol and diagnostic streams."""
    import os
    import subprocess
    import sys

    from virgo.execution import ProcessRequest, cleanup_process

    outside = project.parent / "stdio-outside.txt"
    outside.write_text("unchanged")
    source = f"""import os, sys
from pathlib import Path
assert 'UNGRANTED_TEST_SECRET' not in os.environ
assert os.environ['MCP_TEST_CREDENTIAL'] == 'explicit-grant'
value = sys.stdin.buffer.read()
sys.stdout.buffer.write(b'protocol:' + value)
sys.stdout.buffer.flush()
sys.stderr.buffer.write(b'diagnostic:' + value)
sys.stderr.buffer.flush()
try: Path({str(outside)!r}).write_text('bad')
except OSError: pass
else: raise AssertionError('outside write succeeded')
"""
    request = ProcessRequest((sys.executable, "-I", "-S", "-c", source), project,
                             {**os.environ, "UNGRANTED_TEST_SECRET": "must-not-inherit"},
                             stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                             env_overrides={"MCP_TEST_CREDENTIAL": "explicit-grant"})
    process = native.main.execution_context.run_process(request)
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(b"hello stdio")
        process.stdin.close()
        assert process.wait(timeout=15) == 0
        stdout, stderr = process.stdout.read(), process.stderr.read()
        assert stdout == b"protocol:hello stdio", {"stdout": stdout, "stderr": stderr}
        expected_stderr = {b"diagnostic:hello stdio"}
        if sys.platform == "win32":
            # CPython can continue after failing to canonicalize its executable
            # under LPAC. Keep that exact diagnostic on stderr, never stdout.
            warning = f"Failed to find real location of {sys._base_executable}\n".encode()
            expected_stderr.add(warning + b"diagnostic:hello stdio")
        assert stderr in expected_stderr, {"stdout": stdout, "stderr": stderr}
        assert outside.read_text() == "unchanged"
    finally:
        cleanup_process(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def workspace_write_and_indirect_escape(native, project):
    outside = project.parent / "outside.txt"
    outside.write_text("unchanged")
    source = f"""from pathlib import Path
Path('allowed.txt').write_text('allowed')
try: Path({str(outside)!r}).write_text('bad')
except OSError: print('denied')
else: raise AssertionError('outside write succeeded')
"""
    result = run(native, python_command(native, source))
    assert not result.is_error and "denied" in result.text(), result.text()
    assert (project / "allowed.txt").read_text() == "allowed"
    assert outside.read_text() == "unchanged"


def readonly_including_child_shell(native, project):
    native.services["execution"].select_mode("read-only")
    (project / "readable.txt").write_text("readable")
    result = run(
        native,
        python_command(
            native,
            "from pathlib import Path; print(Path('readable.txt').read_text()); Path('blocked.txt').touch()",
        ),
    )
    assert result.is_error and "readable" in result.text()
    assert not (project / "blocked.txt").exists()


def readonly_shell_grant_writes_one_existing_file(native, project):
    from virgo.ai.providers import faux

    from tests.support.harness import tool_results
    from tests.support.shell import selected

    target = project.parent / "approved.txt"
    target.write_text("before")
    operation = {"command": python_command(native, f"""from pathlib import Path
path = Path({str(target)!r})
assert path.read_text() == 'before'
path.write_text('after')
assert path.read_text() == 'after'
""")}
    shell = selected(native).tool_name
    native.services["execution"].select_mode("read-only")
    native.ui.answers.append("y")
    before = len(tool_results(native))
    faux.script("test", faux.reply(tools=[("request_permissions", {
        "tool": shell, "arguments": operation, "permissions": {"write_paths": [str(target)]},
        "reason": "Update this existing data file.",
    })]), faux.reply(tools=[(shell, operation)]), faux.reply("done"))
    native.prompt("Use one Shell operation to update the approved data file")
    approval, result = tool_results(native)[before:]
    assert not approval.is_error, approval.text()
    assert not result.is_error, result.text()
    boundary = result.details["boundary"]
    assert boundary["state"] == "established" and boundary["payload_started"] is True
    assert boundary["effective"]["native_process"]["write_paths"] == [str(target)]
    assert target.read_text() == "after" and len(native.ui.questions) == 1
    assert native.main.permission_snapshot.mode.id == "read-only"
    assert not native.main.execution_context.resources.write_paths


def live_workspace_reader_observes_edit(native, project):
    from virgo.ai.providers import faux

    from tests.support.harness import tool_results, wait_until

    source = project / "watched.txt"
    source.write_text("before")
    command = """import time
from pathlib import Path
source = Path('watched.txt')
assert source.read_text() == 'before'
Path('reader-ready').touch()
deadline = time.monotonic() + 15
while source.read_text() != 'after':
    assert time.monotonic() < deadline
    time.sleep(.05)
Path('reader-observed').write_text(source.read_text())
"""
    result = run(native, python_command(native, command), background=True)
    assert not result.is_error, result.text()
    jobs = native.services["jobs"]
    job = list(jobs.jobs.values())[-1]
    try:
        wait_until((project / "reader-ready").exists, timeout=15)
        arguments = {"path": "watched.txt", "edits": [{"old_text": "before", "new_text": "after"}]}
        faux.script("test", faux.reply(tools=[("edit", arguments)]), faux.reply("done"))
        native.prompt("Save the edit while the reader is running")
        edited = tool_results(native)[-1]
        assert not edited.is_error, edited.text()
        assert source.read_text() == "after"
        assert job.done.wait(20) and job.status == "completed", job.result
        assert (project / "reader-observed").read_text() == "after"
        assert not native.ui.questions
    finally:
        if not job.done.is_set():
            jobs.kill(job)
            assert job.done.wait(10)


def partial_execution_preserves_denied_file(native, project):
    outside = project.parent / "protected.txt"
    outside.write_text("unchanged")
    result = run(
        native,
        python_command(
            native,
            f"from pathlib import Path; Path('first.txt').touch(); Path({str(outside)!r}).write_text('bad')",
        ),
    )
    assert result.is_error and (project / "first.txt").exists()
    assert outside.read_text() == "unchanged"
    assert result.details["boundary"]["side_effects"] == "possible"


def network_and_environment_isolation(native, project, monkeypatch):
    import socket
    import threading

    monkeypatch.setenv("ISOLATION_TEST_SECRET", "must-not-inherit")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(5)
        source = f"import os,socket; assert 'ISOLATION_TEST_SECRET' not in os.environ; socket.create_connection({server.getsockname()!r},2).close(); print('connected')"
        provider = native.services["execution"]
        provider.select_mode("read-only")
        denied = run(native, python_command(native, source))
        assert denied.is_error
        provider.select_mode("workspace-edit")
        accepted = []

        def accept():
            connection, _ = server.accept()
            connection.close()
            accepted.append(True)

        worker = threading.Thread(target=accept)
        worker.start()
        allowed = run(native, python_command(native, source))
        worker.join(6)
        assert not allowed.is_error and accepted and "connected" in allowed.text(), allowed.text()


def failure_and_timeout_are_not_reclassified(native):
    # The process contract reports the selected shell's exit status.
    marker = native.cwd / "nonzero-once.txt"
    payload = "from pathlib import Path\nwith Path('nonzero-once.txt').open('a') as f: f.write('once')"
    result = run(native, python_command(native, payload) + "\nexit 7")
    assert result.is_error and result.details["exit_code"] == 7
    assert marker.read_text() == "once" and result.details["boundary"]["payload_started"] is True
    result = run(native, python_command(native, "import time; time.sleep(10)"), timeout=0.2)
    assert result.is_error
    assert result.details["status"] in {"timeout", "execution_failed"}
    assert result.details["boundary"]["cleanup"] in {"best_effort", "executor_completed", "failed", "unknown"}
