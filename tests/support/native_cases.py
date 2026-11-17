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
        provider.save_mode("offline", "Offline", ["."], ["."], False, persist=False)
        provider.select_mode("offline")
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
    result = run(native, python_command(native, "raise SystemExit(7)"))
    assert result.is_error and result.details["exit_code"] == 7
    result = run(native, python_command(native, "import time; time.sleep(10)"), timeout=0.2)
    assert result.is_error
    assert result.details["status"] in {"timeout", "execution_failed"}
    assert result.details["boundary"]["cleanup"] in {"best_effort", "executor_completed", "failed", "unknown"}
