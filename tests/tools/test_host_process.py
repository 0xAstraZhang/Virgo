"""Host command ownership through the public execution context."""
import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, call, sentinel

from virgo.execution import HostContext, HostProcess, cleanup_process


def test_host_cleanup_after_group_leader_exit_preserves_other_job(tmp_path):
    host = HostContext(tmp_path)
    waiting = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
               "print('ready', flush=True); time.sleep(30)")
    other = host.spawn([sys.executable, "-c", waiting], tmp_path, os.environ, subprocess.PIPE)
    code = ("import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], stdout=sys.stdout); "
            "sys.exit(17)")
    try:
        assert other.stdout.readline().strip() == b"ready"
        command = host.spawn([sys.executable, "-c", code], tmp_path, os.environ, subprocess.PIPE)
        try:
            assert command.wait(timeout=5) == 17
            cleanup_process(command)
            cleanup_process(command)
            with ThreadPoolExecutor(max_workers=1) as reader:
                # EOF acknowledges that the same-group child has released stdout.
                assert reader.submit(command.stdout.read).result(timeout=1) == b""
            assert command.poll() == 17
            assert other.poll() is None
            # A cancellation request can race the owner that finalizes the same job.
            with ThreadPoolExecutor(max_workers=2) as pool:
                cancel = pool.submit(other.kill_tree)
                finalize = pool.submit(other.cleanup_tree)
                cancel.result(timeout=5)
                finalize.result(timeout=5)
            assert other.poll() is not None
        finally:
            cleanup_process(command)
            command.stdout.close()
    finally:
        cleanup_process(other)
        other.stdout.close()


def test_host_cleanup_waits_for_group_exit_after_darwin_permission_error(monkeypatch):
    process = Mock(pid=42, returncode=None)

    def reap():
        process.returncode = 17
        return process.returncode

    process.wait.side_effect = reap
    handle = HostProcess(process)
    monkeypatch.setattr(handle, "_leader_exited", lambda: True)
    monkeypatch.setattr(signal, "SIGKILL", sentinel.kill_signal, raising=False)
    kill_group = Mock(side_effect=[PermissionError(), None, ProcessLookupError()])
    monkeypatch.setattr(os, "killpg", kill_group, raising=False)

    assert handle.poll() == 17
    assert handle.poll() == 17
    process.wait.assert_called_once_with()
    assert kill_group.call_args_list == [call(42, sentinel.kill_signal), call(42, 0), call(42, 0)]


def test_host_command_observes_committed_file_without_loading_sandbox(tmp_path):
    # Start with fresh imports so modules loaded by pytest cannot hide a probe.
    from textwrap import dedent

    check = dedent("""
        import os
        import subprocess
        import sys
        from pathlib import Path

        imports = []
        sandbox_modules = (
            "virgo.harness.sandbox", "virgo.tools.sandbox", "virgo.tools.sandbox_files",
            "virgo.tools.windows_sandbox", "virgo.tools.windows_sandbox_acl",
            "virgo.tools.windows_sandbox_files", "virgo.extensions.sandbox",
        )
        class ObserveImports:
            def find_spec(self, fullname, path, target=None):
                if fullname in sandbox_modules:
                    imports.append(fullname)
        sys.meta_path.insert(0, ObserveImports())

        from virgo.execution import HostContext, cleanup_process

        project = Path(sys.argv[1])
        host = HostContext(project)
        source = project / "message.txt"
        with host.open_mutation(source, create=True) as (stream, _, commit):
            stream.write(b"from the editor")
            commit()
        command = "from pathlib import Path; print(Path('message.txt').read_text())"
        process = host.spawn([sys.executable, "-c", command], project, os.environ, subprocess.PIPE)
        try:
            assert process.wait(timeout=5) == 0
            assert process.stdout.read().strip() == b"from the editor"
        finally:
            cleanup_process(process)
            process.stdout.close()
        assert not imports, imports
    """)
    subprocess.run([sys.executable, "-c", check, str(tmp_path)], check=True, timeout=15)
