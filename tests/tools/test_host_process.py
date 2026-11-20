"""Host command ownership through the public execution context."""
import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, call, sentinel

import pytest

from virgo.execution import HostContext, HostProcess, ProcessRequest, cleanup_process


def test_host_cleanup_after_group_leader_exit_preserves_other_job(tmp_path):
    host = HostContext(tmp_path)
    waiting = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
               "print('ready', flush=True); time.sleep(30)")
    other = host.run_process(ProcessRequest((sys.executable, "-c", waiting), tmp_path, dict(os.environ)))
    code = ("import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], stdout=sys.stdout); "
            "sys.exit(17)")
    try:
        assert other.stdout.readline().strip() == b"ready"
        command = host.run_process(ProcessRequest((sys.executable, "-c", code), tmp_path, dict(os.environ)))
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
            "virgo.tools.windows_sandbox_files", "virgo.sandbox",
        )
        class ObserveImports:
            def find_spec(self, fullname, path, target=None):
                if fullname in sandbox_modules:
                    imports.append(fullname)
        sys.meta_path.insert(0, ObserveImports())

        from virgo.execution import HostContext, ProcessRequest, cleanup_process

        project = Path(sys.argv[1])
        host = HostContext(project)
        source = project / "message.txt"
        with host.open_mutation(source, create=True) as (stream, _, commit):
            stream.write(b"from the editor")
            commit()
        command = "from pathlib import Path; print(Path('message.txt').read_text())"
        process = host.run_process(ProcessRequest((sys.executable, "-c", command), project, dict(os.environ)))
        try:
            assert process.wait(timeout=5) == 0
            assert process.stdout.read().strip() == b"from the editor"
        finally:
            cleanup_process(process)
            process.stdout.close()
        assert not imports, imports
    """)
    subprocess.run([sys.executable, "-c", check, str(tmp_path)], check=True, timeout=15)


def test_native_policy_rejects_empty_argv_and_host_overrides_case_colliding_env(tmp_path):
    from virgo.execution import ExecutionUnavailable, _merge_environment, environment_overrides
    from virgo.sandbox.platform import NativePolicy

    policy = NativePolicy(tmp_path, True, False, (), ())
    with pytest.raises(ExecutionUnavailable, match="nonempty command argv"):
        policy.spawn([], tmp_path, {}, subprocess.DEVNULL)

    host = HostContext(tmp_path)
    with pytest.raises(ExecutionUnavailable, match="nonempty command argv"):
        host.run_process(ProcessRequest((), tmp_path, {}))

    with pytest.raises(ExecutionUnavailable, match="identifier"):
        environment_overrides({"-S/bin/echo hi #": "1"})
    with pytest.raises(ExecutionUnavailable, match="control characters"):
        environment_overrides({"GOOD_KEY": "bad\x01value"})

    merged = _merge_environment(
        {"foo": "old_lower", "FOO": "old_upper", "KEEP": "1"},
        {"foo": "override"},
        case_insensitive=True,
    )
    assert merged == {"KEEP": "1", "foo": "override"}

    code = "import os; print(os.environ.get('CUSTOM_OVERRIDE', os.environ.get('custom_override', '')))"
    env = {**os.environ, "CUSTOM_OVERRIDE": "base"}
    process = host.run_process(ProcessRequest(
        (sys.executable, "-c", code), tmp_path, env, env_overrides={"custom_override": "override"},
    ))
    try:
        assert process.wait(timeout=10) == 0
        expected = b"override" if os.name == "nt" else b"base"
        assert process.stdout.read().strip() == expected
    finally:
        cleanup_process(process)
        process.stdout.close()


def test_native_process_cleanup_releases_scratch_after_drain_error(monkeypatch):
    from types import SimpleNamespace

    from virgo.execution import ExecutionFailed
    from virgo.sandbox import native as native_mod
    from virgo.sandbox.native import NativeProcess

    # Exercise the non-HostProcess wrapper path (macOS LaunchdProcess / WindowsProcess) on all OSes.
    monkeypatch.setattr(native_mod.sys, "platform", "darwin")
    cleaned = []

    class FakeScratch:
        def cleanup(self):
            cleaned.append(True)

    class FakeProc:
        pid = 123
        tree_drained = True
        _virgo_sandbox_tmp = FakeScratch()

        def cleanup_tree(self):
            raise RuntimeError("diagnostic error after drain")

    fake_context = SimpleNamespace(
        describe=lambda: {"provider": "sandbox"},
        resources=SimpleNamespace(flags=lambda: {}),
    )
    handle = NativeProcess(FakeProc(), fake_context)
    with pytest.raises(ExecutionFailed, match="diagnostic error after drain"):
        handle.cleanup_tree()
    assert cleaned == [True]
    assert handle._scratch is None
