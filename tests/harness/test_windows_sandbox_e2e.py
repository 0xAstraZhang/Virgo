"""Required MXC ProcessContainer acceptance on a real Windows host.

The former LPAC token/DACL backend is retired. PSEC absence and an unsupported
resource policy are failures in required runs, never successful isolation.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage, UserMessage
from virgo.harness.extensions import ExtensionAPI
from virgo.harness.store import SessionStore

from tests.support import native_cases
from tests.support.harness import tool_results
from tests.support.sandbox import native_session, python_command, run
from tests.support.shell import selected


@pytest.fixture
def native(make_session):
    return native_session(make_session, {"win32"})


@pytest.mark.skipif(os.name != "nt", reason="NTFS reparse-point broker test")
@pytest.mark.parametrize("kind", ["junction", "metadata-junction", "hardlink"])
def test_reparse_and_shared_file_grants_fail_before_payload(broker_session, project, kind):
    outside = project.parent / "private"
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_text("unchanged")
    if kind in {"junction", "metadata-junction"}:
        link = project / (".git" if kind == "metadata-junction" else "alias")
        command = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
        subprocess.run(
            [command, "/d", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
        )
        target = link / protected.name
    else:
        target = project / "alias.txt"
        os.link(protected, target)
    session = broker_session(
        faux.reply(tools=[("write", {"path": str(target), "content": "bad"})]), faux.reply("done")
    )
    session.prompt("Attempt the alias mutation")
    assert tool_results(session)[-1].is_error
    assert protected.read_text() == "unchanged"


def test_private_host_files_denied_with_positive_workspace_control(native, project):
    native_cases.workspace_write_and_indirect_escape(native, project)


def test_windows_readonly_resource_boundary(native, project):
    native_cases.readonly_including_child_shell(native, project)


def test_windows_partial_execution_preserves_denied_file(native, project):
    native_cases.partial_execution_preserves_denied_file(native, project)


def test_network_permission_grants_real_egress_without_host_fallback(native, project, monkeypatch):
    native_cases.network_and_environment_isolation(native, project, monkeypatch)


def test_actual_start_failure_is_fail_closed_and_nonzero_payload_is_not_replayed(native):
    native_cases.failure_and_timeout_are_not_reclassified(native)


def test_child_runtime_keeps_origin_with_canonical_cwd_and_rejects_escape(native, project):
    nested = project / "目录 with spaces"
    nested.mkdir()
    outside = project.parent / "other-workspace"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("outside")
    api = ExtensionAPI(native, "windows-origin")
    child = api.create_runtime("original-child", "subagent", parent=native.main,
                               cwd=Path(str(nested).swapcase()) / ".." / nested.name)
    escaped = api.create_runtime("escaped-child", "subagent", parent=native.main, cwd=outside)
    current = SessionStore.new(native.paths.sessions, outside)
    current.append_message(UserMessage("Selected workspace conversation"))
    native.open_session(current.path)
    try:
        source = f"""from pathlib import Path
Path('child-success.txt').write_text('origin')
try:
    Path({str(secret)!r}).write_text('bad')
except PermissionError:
    print('outside-denied')
else:
    raise RuntimeError('outside write escaped')
"""
        faux.script("test", faux.reply(tools=[(selected(native).tool_name, {"command": python_command(native, source)})]),
                    faux.reply("done"))
        child.agent.prompt("Exercise the original runtime workspace")
        result = next(m for m in reversed(child.messages) if isinstance(m, ToolResultMessage))
        assert not result.is_error and "outside-denied" in result.text(), result.text()
        assert result.details["boundary"]["workspace"] == str(project.resolve())
        assert (nested / "child-success.txt").read_text() == "origin" and secret.read_text() == "outside"
        faux.script("test", faux.reply(tools=[(selected(native).tool_name, {
            "command": python_command(native, "from pathlib import Path; Path('escaped.txt').touch()"),
        })]), faux.reply("done"))
        escaped.agent.prompt("Attempt to change the sandbox root using cwd")
        result = next(m for m in reversed(escaped.messages) if isinstance(m, ToolResultMessage))
        assert result.is_error and result.details["status"] == "execution_unavailable", result.text()
        assert not (outside / "escaped.txt").exists() and not (outside / ".virgo").exists()
    finally:
        api.release_runtime(child)
        api.release_runtime(escaped)


def test_credentials_and_unlisted_handles_are_not_inherited(native, project, monkeypatch):
    import msvcrt

    secret = project.parent / 'handle-secret.txt'
    secret.write_bytes(b'private-handle-canary')
    monkeypatch.setenv('VIRGO_PRIVATE_CREDENTIAL_CANARY', 'must-not-reach-payload')
    with secret.open('rb') as stream:
        handle = msvcrt.get_osfhandle(stream.fileno())
        os.set_handle_inheritable(handle, True)
        source = f'''import ctypes,os
from ctypes import wintypes
assert 'VIRGO_PRIVATE_CREDENTIAL_CANARY' not in os.environ
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.ReadFile.argtypes=[wintypes.HANDLE,wintypes.LPVOID,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),wintypes.LPVOID]
content=ctypes.create_string_buffer(128); count=wintypes.DWORD()
success=kernel.ReadFile({handle},content,128,ctypes.byref(count),None)
assert not success or b'private-handle-canary' not in content.raw
print('credentials and handle protected')
'''
        result = run(native, python_command(native, source))
    assert not result.is_error and 'credentials and handle protected' in result.text(), result.text()


@pytest.mark.parametrize('finish', ['exit', 'timeout', 'cancel'])
def test_completion_drains_descendants_before_scratch_cleanup(native, project, finish):
    import json
    import threading
    import time

    child = "import time; from pathlib import Path; Path('child-ready').touch(); time.sleep(6); Path('late-write').touch()"
    source = f'''import json,os,subprocess,sys,time
from pathlib import Path
p=subprocess.Popen([sys.executable,'-I','-S','-c',{child!r}],creationflags=0x200,
 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
Path('lifetime.json').write_text(json.dumps({{'pid':p.pid,'scratch':os.environ['TEMP']}}))
deadline=time.monotonic()+10
while not Path('child-ready').exists():
 assert time.monotonic()<deadline
 time.sleep(.02)
''' + ('time.sleep(25)' if finish != 'exit' else "print('complete')")
    results = []
    def execute():
        results.append(run(native, python_command(native, source), timeout=3 if finish == 'timeout' else 30))
    if finish == 'cancel':
        worker = threading.Thread(target=execute)
        worker.start()
        try:
            deadline = time.monotonic()+15
            while not (project/'child-ready').exists() and worker.is_alive():
                assert time.monotonic()<deadline
                time.sleep(.02)
            native.abort()
            worker.join(15)
            assert not worker.is_alive()
        finally:
            native.abort()
            worker.join(15)
    else:
        execute()
    assert results
    result = results[0]
    assert result.details['status'] == {'exit':'ok','timeout':'timeout','cancel':'aborted'}[finish], result.text()
    assert result.details['boundary']['cleanup'] == 'executor_completed'
    lifetime = json.loads((project/'lifetime.json').read_text())
    assert not Path(lifetime['scratch']).exists()
    deadline = time.monotonic()+6.2
    while not (project/'late-write').exists() and time.monotonic()<deadline:
        time.sleep(.05)
    assert not (project/'late-write').exists(), 'Descendant retained workspace access after cleanup'


def test_background_cancel_preserves_other_job_and_origin_after_switch(native, project):
    from tests.support.harness import wait_until

    command = python_command(native, "import time; from pathlib import Path; Path('ready').touch(); time.sleep(15)")
    result = run(native, command, background=True)
    assert not result.is_error, result.text()
    jobs = native.services['jobs']
    first = list(jobs.jobs.values())[-1]
    wait_until((project/'ready').exists)
    (project/'ready').unlink()
    result = run(native, command, background=True)
    assert not result.is_error, result.text()
    second = list(jobs.jobs.values())[-1]
    try:
        wait_until((project/'ready').exists)
        native.new_session()
        jobs.kill(first)
        assert first.done.wait(10)
        assert second.proc.poll() is None and not second.done.is_set()
        assert first.accounting_store is second.accounting_store
        assert first.accounting_store is not native.main.origin.root
    finally:
        jobs.kill(second)
        assert second.done.wait(10)


def test_live_ipv4_ipv6_and_nonloopback_connections_are_denied(native):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(('192.0.2.1', 9))
        address = route.getsockname()[0]
    targets = [(socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1'), (socket.AF_INET, address)]
    provider = native.services['execution']
    provider.save_mode('offline', 'Offline', ['.'], ['.'], False, persist=False)
    for family, address in targets:
        with socket.socket(family) as listener:
            listener.bind((address, 0))
            listener.listen()
            destination = listener.getsockname()
            source = f'import socket; s=socket.socket({int(family)}); s.settimeout(2); s.connect({destination!r}); s.close(); print("connected")'
            provider.select_mode('workspace-edit')
            allowed = run(native, python_command(native, source))
            assert not allowed.is_error and 'connected' in allowed.text(), allowed.text()
            provider.select_mode('offline')
            denied = run(native, python_command(native, source))
            assert denied.is_error and 'connected' not in denied.text(), denied.text()


def test_repeated_commands_release_owned_acls_scratch_and_handles(native, project):
    """MXC no longer borrows LPAC/DACL leases; retain scratch and handle checks."""
    import ctypes
    import json
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    def count():
        value = wintypes.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value))
        return value.value
    before, scratches = count(), set()
    for index in range(4):
        source = f'''import json,os
from pathlib import Path
Path('new-{index}.txt').write_text('ordinary')
print(json.dumps({{'scratch':os.environ['TEMP']}}))
'''
        result = run(native, python_command(native, source))
        assert not result.is_error, result.text()
        scratch = json.loads(result.text())['scratch']
        assert scratch not in scratches and not Path(scratch).exists()
        scratches.add(scratch)
        assert (project/f'new-{index}.txt').read_text() == 'ordinary'
    assert count()-before < 12, 'Repeated executor launches leaked broker handles'
