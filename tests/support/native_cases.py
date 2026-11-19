"""Shared native acceptance bodies; each platform owns its test identities."""
from tests.support.sandbox import python_command, run


def filesystem_helper_operations(native, project, monkeypatch):
    """File tools, direct OS denials, snapshots and opaque outputs share a boundary."""
    import errno
    import os
    import subprocess
    import sys
    import threading
    from dataclasses import replace

    from virgo.agent.types import ToolContext
    from virgo.execution import ProcessRequest, SandboxExecutor, cleanup_process
    from virgo.file_operations import _private_directory

    metadata = project / ".git"
    metadata.mkdir(exist_ok=True)
    protected = metadata / "config"
    protected.write_text("protected")
    # Bind an existing control path into this task's fixed deny rules.
    native.main.refresh_permission_snapshot()
    access = native.main.execution_context

    def operation(name, arguments, *, context=None):
        response = (context or access).run_file_operation(name, arguments, cwd=project)
        assert response["boundary"]["state"] == "established", response
        assert response["boundary"]["payload_started"] is True, response
        return response

    written = operation("write", {"path": "worker/file.txt", "content": "before\n"})
    assert written["ok"], written
    edited = operation("edit", {"path": "worker/file.txt", "edits": [
        {"old_text": "before", "new_text": "after"},
    ]})
    assert edited["ok"], edited
    read = operation("read", {"path": "worker/file.txt"})
    assert read["segments"] == ["after\n"], read
    globbed = operation("glob", {"path": "worker", "pattern": "*.txt"})
    assert globbed["ok"] and globbed["details"]["count"] == 1, globbed
    grepped = operation("grep", {"path": "worker", "pattern": "after"})
    assert grepped["ok"] and grepped["details"]["matches"] == 1, grepped
    assert not list(project.rglob(".virgo-write-*.tmp"))

    denied = operation("write", {"path": str(protected), "content": "changed"})
    assert not denied["ok"] and protected.read_text() == "protected", denied

    snapshot = access.executor.files.directory / "fs_worker.py"
    before = snapshot.read_bytes()
    denied = operation("write", {"path": str(snapshot), "content": "raise SystemExit(73)"})
    assert not denied["ok"] and snapshot.read_bytes() == before, denied

    # Place the actual trusted namespace inside a writable tree, as with HOME.
    # Both a normal command and the helper must retain its read-only carve-out.
    runtime_root = project / "trusted-runtime"
    _private_directory(runtime_root)
    with monkeypatch.context() as patch:
        patch.setattr("virgo.file_operations._runtime_root", lambda: runtime_root)
        nested_executor = SandboxExecutor()
    try:
        nested = replace(access, executor=nested_executor)
        worker = nested_executor.files.directory / "fs_worker.py"
        contents = worker.read_bytes()
        code = f"""import sys
from pathlib import Path
path = Path({str(worker)!r})
try:
    path.chmod(0o600)
    path.write_text('raise SystemExit(73)')
except OSError:
    print('snapshot-denied')
else:
    raise AssertionError('command modified its trusted filesystem worker')
print('runtime-ready', flush=True)
future = Path(sys.stdin.readline().strip())
worker = Path(sys.stdin.readline().strip())
assert future.read_text() == 'future-canary'
try:
    future.write_text('modified-by-earlier-executor')
except OSError:
    print('future-runtime-denied')
else:
    raise AssertionError('command modified a later executor runtime')
try:
    worker.chmod(0o600)
    worker.write_text('modified-by-earlier-executor')
except OSError:
    print('future-worker-denied')
else:
    raise AssertionError('command modified a later executor worker')
Path('future-runtime-control.txt').write_text('allowed')
"""
        command = nested.run_process(ProcessRequest((sys.executable, "-I", "-S", "-c", code),
                                                    project, dict(os.environ), stdin=subprocess.PIPE,
                                                    stderr=subprocess.PIPE))
        future_executor = None
        try:
            assert command.stdout.readline().rstrip(b"\r\n") == b"snapshot-denied"
            assert command.stdout.readline().rstrip(b"\r\n") == b"runtime-ready"
            # This snapshot did not exist when the older process received its
            # workspace grants. Its protection must include future instances.
            with monkeypatch.context() as patch:
                patch.setattr("virgo.file_operations._runtime_root", lambda: runtime_root)
                future_executor = SandboxExecutor()
            # A normal host-created canary inherits the parent's active ACEs;
            # Python's private Windows mkdtemp directory may suppress them.
            canary = runtime_root / "future-canary.txt"
            canary.write_text("future-canary")
            canary.chmod(0o600)
            future_worker = future_executor.files.directory / "fs_worker.py"
            future_contents = future_worker.read_bytes()
            command.stdin.write((str(canary) + "\n" + str(future_worker) + "\n").encode())
            command.stdin.close()
            code = command.wait(timeout=20)
            stdout, stderr = command.stdout.read(), command.stderr.read()
            assert code == 0, {"stdout": stdout, "stderr": stderr}
            assert b"future-runtime-denied" in stdout
            assert b"future-worker-denied" in stdout
            assert canary.read_text() == "future-canary"
            assert future_worker.read_bytes() == future_contents
            assert (project / "future-runtime-control.txt").read_text() == "allowed"
        finally:
            cleanup_process(command)
            for stream in (command.stdin, command.stdout, command.stderr):
                stream.close()
            if future_executor is not None:
                future_executor.close()
        denied = operation("write", {"path": str(worker), "content": "raise SystemExit(73)"}, context=nested)
        assert not denied["ok"] and worker.read_bytes() == contents, denied
        assert operation("read", {"path": "worker/file.txt"}, context=nested)["ok"]
    finally:
        nested_executor.close()
    assert runtime_root.is_dir(), "Closing snapshots must retain the namespace protected by older processes"

    record, stream = access.outputs.create("acceptance")
    with stream:
        stream.write(b"private-output-canary")
    owned = ToolContext("owned-output", project, threading.Event(), origin=native.main.origin)
    read_output = native.tool("read_output")
    visible = read_output.execute({"output_id": record.id}, owned)
    assert not visible.is_error and visible.as_text() == "private-output-canary"
    assert read_output.execute({"output_id": str(record._path)}, owned).is_error
    denied = operation("read", {"path": str(record._path)})
    assert not denied["ok"] and "private-output-canary" not in str(denied), denied

    native.services["execution"].select_mode("read-only")
    readonly = native.main.execution_context
    # Call the executor directly: success here must not depend on the Broker's
    # preflight refusing write/edit before the native payload starts.
    denied = operation("write", {"path": "worker/file.txt", "content": "forbidden"}, context=readonly)
    assert not denied["ok"] and denied["error"]["errno"] in {errno.EACCES, errno.EPERM, errno.EROFS}, denied
    assert (project / "worker/file.txt").read_text() == "after\n"
    assert operation("read", {"path": "worker/file.txt"}, context=readonly)["ok"]

    native.new_session()
    foreign = ToolContext("foreign-output", project, threading.Event(), origin=native.main.origin)
    assert read_output.execute({"output_id": record.id}, foreign).is_error
    assert not read_output.execute({"output_id": record.id}, owned).is_error


def explicit_read_deny(native, project, kind, prepare=None):
    """A real payload cannot reopen an explicit deny through a narrower allow."""
    import json
    import os
    import sys
    from dataclasses import replace

    from virgo.execution import ProcessRequest, cleanup_process
    from virgo.execution_policy import PathRule

    parent = project / "policy-parent"
    parent.mkdir()
    root = parent / "denied-resource"
    if kind == "directory":
        root.mkdir()
        secret = root / "secret.txt"
    else:
        secret = root
    secret.write_text("private-canary")
    if prepare is not None:
        prepare(secret)
    base = native.main.execution_context
    permissions = base.policy.with_rules(PathRule(root, "read", "deny"), PathRule(secret, "write"))
    context = replace(base, resources=replace(base.resources, policy=permissions))
    source = f"""import json
from pathlib import Path
secret = Path({str(secret)!r})
parent = Path({str(parent)!r})
denied = []
for operation in (secret.read_text, lambda: secret.write_text('bad'), secret.unlink,
                  lambda: parent.rename(parent.with_name('moved-parent'))):
    try: operation()
    except OSError: denied.append(True)
    else: denied.append(False)
(parent / 'public.txt').write_text('allowed')
print(json.dumps({{'denied': denied, 'public': (parent / 'public.txt').read_text()}}))
"""
    process = context.run_process(ProcessRequest((sys.executable, "-I", "-S", "-c", source), project, dict(os.environ)))
    try:
        assert process.wait(timeout=20) == 0
        output = process.stdout.read().decode("utf-8", errors="replace")
        assert json.loads(output.rstrip().rsplit("\n", 1)[-1]) == {"denied": [True] * 4, "public": "allowed"}, output
    finally:
        cleanup_process(process)
        process.stdout.close()
    assert secret.read_text() == "private-canary"


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
    assert [rule["path"] for rule in boundary["effective"]["native_process"]["filesystem"]
            if rule["effect"] == "allow" and rule["access"] == "write"] == [str(target)]
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
