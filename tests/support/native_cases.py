"""Shared native acceptance bodies; each platform owns its test identities."""
from tests.support.sandbox import python_command, run


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
