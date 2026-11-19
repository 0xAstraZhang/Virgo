"""Native LPAC, file boundary and process-lifetime acceptance on Windows."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage, UserMessage
from virgo.harness.extensions import ExtensionAPI
from virgo.harness.store import SessionStore

from tests.support import native_cases
from tests.support.harness import tool_results, wait_until
from tests.support.sandbox import native_session, python_command, run
from tests.support.shell import selected

# Teardown drains the process tree and takes the ACL broker lock in sequence.
_BACKGROUND_CLEANUP_TIMEOUT = 30


@pytest.fixture
def native(make_session):
    return native_session(make_session, {"win32"})


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_core_mutation_preserves_live_background_workspace_writes(make_session, project, tool):
    (project / ".virgo").mkdir()
    native = native_session(make_session, {"win32"})
    target = project / "ordinary.txt"
    target.write_text("original\n")
    baseline_dacl = dacl(target)
    created = project / "created-by-core.txt"
    jobs = native.services["jobs"]
    result = run(native, command(f"""import time
from pathlib import Path
with Path('ordinary.txt').open('a') as stream:
    stream.write('before\\n')
Path('mutation-ready').touch()
deadline = time.monotonic() + 45
while not Path('release-mutation').exists():
    assert time.monotonic() < deadline
    time.sleep(.02)
print('opening committed file for read', flush=True)
assert Path('ordinary.txt').read_text() == 'changed\\n'
print('opening committed file for append', flush=True)
with Path('ordinary.txt').open('a') as stream:
    stream.write('after\\n')
print('committed file append complete', flush=True)
if {tool == "write"}:
    assert Path('created-by-core.txt').read_text() == 'created\\n'
    with Path('created-by-core.txt').open('a') as stream:
        stream.write('after\\n')
"""), background=True)
    assert not result.is_error, result.text()
    job = list(jobs.jobs.values())[-1]
    try:
        wait_until(lambda: (project / "mutation-ready").exists() or job.done.is_set(), timeout=25)
        assert (project / "mutation-ready").exists(), jobs.output(job)
        assert target.read_text() == "original\nbefore\n"
        restricting, = process_sids(job.proc.pid, 11)
        package, = process_sids(job.proc.pid, 31)
        original_dacl = dacl(target)
        metadata_dacl = dacl(project / ".virgo")
        arguments = ({"path": target.name, "content": "changed\n"} if tool == "write" else
                     {"path": target.name, "edits": [{"old_text": "original\nbefore\n",
                                                       "new_text": "changed\n"}]})
        faux.script("test", faux.reply(tools=[(tool, arguments)]), faux.reply("done"))
        native.prompt("Update the ordinary file while its background command is waiting")
        mutation = tool_results(native)[-1]
        assert not mutation.is_error, mutation.text()
        assert target.read_text() == "changed\n"
        committed_dacl = dacl(target)
        created_dacl = None
        if tool == "write":
            faux.script("test", faux.reply(tools=[("write", {"path": created.name,
                                                             "content": "created\n"})]), faux.reply("done"))
            native.prompt("Create another ordinary file while the background command is waiting")
            mutation = tool_results(native)[-1]
            assert not mutation.is_error, mutation.text()
            assert created.read_text() == "created\n"
            created_dacl = dacl(created)
        assert dacl(project / ".virgo") == metadata_dacl
        assert not list((project / ".virgo").glob("mutation-*.tmp"))
        (project / "release-mutation").touch()
        assert job.done.wait(15), "background command did not drain after Core mutation"
        assert job.status == "completed", (
            f"Background status: {job.status}\nRestricting SID: {restricting}\nPackage SID: {package}\n"
            f"DACL before Core mutation:\n{original_dacl}\nDACL after Core mutation:\n{committed_dacl}\n"
            f"DACL of Core-created file:\n{created_dacl}\n"
            f"Background output:\n{jobs.output(job)}"
        )
        assert target.read_text() == "changed\nafter\n"
        # Atomic helper replacement inherits the parent's temporary grants;
        # their inherited ACE flags need not match the old inode's explicit ACEs.
        assert restricting in committed_dacl and package in committed_dacl, committed_dacl
        assert dacl(target) == baseline_dacl
        if tool == "write":
            assert created.read_text() == "created\nafter\n"
        for path in (project, project / ".virgo", target, *([created] if tool == "write" else [])):
            cleaned = dacl(path)
            assert restricting not in cleaned and package not in cleaned, cleaned
    finally:
        if not job.done.is_set():
            jobs.kill(job)
            assert job.done.wait(15), "fixture must drain its background command"

def test_background_cancel_preserves_other_job_and_origin_after_switch(native, project):
    from tests.support.harness import wait_until

    # Native setup and session rebuilding can outlast a fixed sleep. Keep
    # both payloads alive until the test cancels them, with bounded waits below.
    command = python_command(
        native, "from pathlib import Path; from threading import Event; Path('ready').touch(); Event().wait()"
    )
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
        assert first.done.wait(_BACKGROUND_CLEANUP_TIMEOUT), first.result
        assert first.status == "killed", first.result
        assert second.proc.poll() is None and not second.done.is_set()
        assert first.accounting_store is second.accounting_store
        assert first.accounting_store is not native.main.origin.root
    finally:
        jobs.kill(second)
        assert second.done.wait(_BACKGROUND_CLEANUP_TIMEOUT), second.result
        assert second.status == "killed", second.result


def test_windows_filesystem_helper_operations(native, project, monkeypatch):
    native_cases.filesystem_helper_operations(native, project, monkeypatch)


def test_windows_generic_process_stdio(native, project):
    native_cases.generic_process_stdio(native, project)


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


def test_windows_live_workspace_reader_observes_edit(native, project):
    native_cases.live_workspace_reader_observes_edit(native, project)


def test_windows_readonly_shell_grant_writes_one_existing_file(native, project):
    native_cases.readonly_shell_grant_writes_one_existing_file(native, project)


def test_windows_partial_execution_preserves_denied_file(native, project):
    native_cases.partial_execution_preserves_denied_file(native, project)


def test_network_permission_grants_real_egress_without_host_fallback(native, project, monkeypatch):
    import socket

    host = os.environ.get("VIRGO_SANDBOX_NETWORK_TEST_HOST", "www.microsoft.com")
    with socket.create_connection((host, 443), timeout=10) as control:
        endpoint = control.getpeername()[:2]
    monkeypatch.setenv("ISOLATION_TEST_SECRET", "private")
    source = f"""import os, socket
assert 'ISOLATION_TEST_SECRET' not in os.environ
with socket.create_connection({endpoint!r}, timeout=10):
    print('egress connected')
"""
    allowed = run(native, python_command(native, source))
    assert not allowed.is_error and "egress connected" in allowed.text(), allowed.text()
    native.services["execution"].select_mode("read-only")
    denied = run(native, python_command(native, source))
    assert denied.is_error and "egress connected" not in denied.text(), denied.text()


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
    keys = ["OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
            "HTTP_PROXY", "HTTPS_PROXY", "SSH_AUTH_SOCK", "VIRGO_HOME", "PYTHONPATH", "BASH_ENV", "ENV"]
    for key in keys:
        monkeypatch.setenv(key, 'must-not-reach-payload')
    with secret.open('rb') as stream:
        handle = msvcrt.get_osfhandle(stream.fileno())
        os.set_handle_inheritable(handle, True)
        source = f'''import ctypes,json,os
from ctypes import wintypes
assert [os.environ.get(key) for key in {keys!r}] == [None] * {len(keys)}
assert os.environ.get('USERPROFILE')
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.ReadFile.argtypes=[wintypes.HANDLE,wintypes.LPVOID,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),wintypes.LPVOID]
content=ctypes.create_string_buffer(128); count=wintypes.DWORD()
try:
    success=kernel.ReadFile({handle},content,128,ctypes.byref(count),None)
except OSError as error:
    if (getattr(error, 'winerror', 0) & 0xffffffff) != 0xC0000008:
        raise
    success=False
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


def test_live_ipv4_ipv6_and_nonloopback_connections_are_denied(native, record_property):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(('192.0.2.1', 9))
        address = route.getsockname()[0]
    targets = [(socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1'), (socket.AF_INET, address)]
    provider = native.services['execution']
    for family, address in targets:
        with socket.socket(family) as listener:
            listener.bind((address, 0))
            listener.listen()
            destination = listener.getsockname()
            source = f"""import json, socket
try:
    with socket.socket({int(family)}) as s:
        s.settimeout(2)
        s.connect({destination!r})
except OSError as error:
    print(json.dumps({{'error': error.winerror}}))
else:
    print(json.dumps({{'connected': True}}))
"""
            provider.select_mode('workspace-edit')
            allowed = run(native, python_command(native, source))
            assert not allowed.is_error and allowed.details["boundary"]["payload_started"] is True, allowed.text()
            # Workspace-Edit allows public egress, not host loopback or the host interface.
            assert "error" in data(allowed), allowed.text()
            record_property(f"workspace_edit_connect_{address}", json.dumps(data(allowed)))
            provider.select_mode('read-only')
            denied = run(native, python_command(native, source))
            assert data(denied) == {"error": 10013}, denied.text()


def test_workspace_edit_inbound_from_host_interface(native, project, record_property):
    """Observe host-to-container TCP independently of external egress and loopback."""
    import json
    import socket

    from tests.support.harness import wait_until

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(("192.0.2.1", 9))
        address = route.getsockname()[0]
    command = f"""import json, socket
from pathlib import Path
marker = Path('listener.json')
def publish(data):
    stage = marker.with_suffix('.tmp')
    stage.write_text(json.dumps(data))
    stage.replace(marker)
with socket.socket() as listener:
    try:
        listener.bind(({address!r}, 0))
        listener.listen()
    except OSError as error:
        publish({{'bind_error': error.winerror}})
    else:
        listener.settimeout(5)
        publish({{'port': listener.getsockname()[1]}})
        try:
            peer, _ = listener.accept()
        except OSError as error:
            print('accept:', error)
        else:
            with peer:
                peer.sendall(b'accepted')
"""
    result = run(native, python_command(native, command), background=True)
    assert not result.is_error, result.text()
    job = list(native.services["jobs"].jobs.values())[-1]
    marker = project / "listener.json"
    wait_until(marker.exists, timeout=10)
    details = json.loads(marker.read_text())
    if "port" in details:
        try:
            with socket.create_connection((address, details["port"]), timeout=3) as peer:
                assert peer.recv(32) == b"accepted"
        except OSError as error:
            details["host_connection_error"] = str(error)
        else:
            details["host_received"] = "accepted"
    assert job.done.wait(_BACKGROUND_CLEANUP_TIMEOUT), job.result
    assert job.status == "completed", job.result
    assert "bind_error" not in details, details
    assert "port" in details, details
    assert "host_connection_error" in details and "host_received" not in details, details
    record_property("host_interface_ingress", json.dumps(details))
    assert not native.ui.questions


def test_repeated_commands_release_owned_acls_scratch_and_handles(native, project):
    """Each command owns its scratch and kernel handles."""
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
        scratch = json.loads(result.text().rstrip().rsplit('\n', 1)[-1])['scratch']
        assert scratch not in scratches and not Path(scratch).exists()
        scratches.add(scratch)
        assert (project/f'new-{index}.txt').read_text() == 'ordinary'
    assert count()-before < 12, 'Repeated executor launches leaked broker handles'


def command(source: str) -> str:
    encoded = base64.b64encode(source.encode()).decode()
    bootstrap = f"import base64;exec(base64.b64decode('{encoded}'))"
    return "& " + " ".join("'" + value.replace("'", "''") + "'" for value in
                            [sys.executable, "-I", "-S", "-c", bootstrap])

def data(result):
    assert not result.is_error, result.text()
    return payload_json(result.text())

def payload_json(output: str):
    # These fixed payloads emit one final JSON record. Shell retains combined
    # stdout/stderr, including CPython's measured LPAC DOS-path warning; that
    # diagnostic is not the payload record and must not be filtered by Shell.
    return json.loads(output.rstrip().rsplit("\n", 1)[-1])

def grant(path: Path, sid: str) -> None:
    """Host fixture setup, using a controlled argv and a well-known SID."""
    result = subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"),
                             str(path), "/grant", f"*{sid}:F"], capture_output=True, check=False)
    assert result.returncode == 0, (result.stdout, result.stderr)

def dacl(path: Path) -> str:
    from ctypes import wintypes as w

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [w.LPWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = w.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ctypes.c_void_p,
    ]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None,
                                        ctypes.byref(descriptor))
    assert status == 0, status
    text = w.LPWSTR()
    try:
        assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 4, ctypes.byref(text), None,
        ), ctypes.get_last_error()
        return text.value
    finally:
        if text:
            kernel.LocalFree(text)
        kernel.LocalFree(descriptor)

def process_sids(pid, information):
    """Read the actual host or sandbox process token, without broker internals."""
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
                                          ctypes.POINTER(w.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]

    class Entry(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", w.DWORD)]

    class Groups(ctypes.Structure):
        _fields_ = [("count", w.DWORD), ("entries", Entry * 1)]

    with ExitStack() as resources:
        process = kernel.OpenProcess(0x1000, False, pid)
        assert process, ctypes.get_last_error()
        resources.callback(kernel.CloseHandle, process)
        token = w.HANDLE()
        assert advapi.OpenProcessToken(process, 8, ctypes.byref(token)), ctypes.get_last_error()
        resources.callback(kernel.CloseHandle, token)
        size = w.DWORD()
        advapi.GetTokenInformation(token, information, None, 0, ctypes.byref(size))
        assert size.value, ctypes.get_last_error()
        buffer = ctypes.create_string_buffer(size.value)
        assert advapi.GetTokenInformation(token, information, buffer, len(buffer), ctypes.byref(size)), ctypes.get_last_error()
        if information == 31:  # TOKEN_APPCONTAINER_INFORMATION contains one PSID.
            sids = [ctypes.c_void_p.from_buffer(buffer).value]
        else:
            count = 1 if information == 1 else w.DWORD.from_buffer(buffer).value
            entries = (Entry * count).from_buffer(buffer, 0 if information == 1 else Groups.entries.offset)
            sids = [entry.sid for entry in entries]
        result = []
        for sid in sids:
            text = w.LPWSTR()
            assert advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)), ctypes.get_last_error()
            result.append(text.value)
            kernel.LocalFree(text)
        return result

DEVICE_ACCESS = {"KsecDD": 0x100003, "Null": 0x12019F}

TOKEN_SOURCE = r'''
import ctypes, json, os
from ctypes import wintypes as w
k = ctypes.WinDLL('kernel32', use_last_error=True)
a = ctypes.WinDLL('advapi32', use_last_error=True)
k.GetCurrentProcess.restype = w.HANDLE
k.CloseHandle.argtypes = [w.HANDLE]
k.LocalFree.argtypes = [ctypes.c_void_p]
a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
a.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
class GenericMapping(ctypes.Structure):
    _fields_ = [(name, w.DWORD) for name in ('read', 'write', 'execute', 'all')]
a.DuplicateToken.argtypes = [w.HANDLE, ctypes.c_int, ctypes.POINTER(w.HANDLE)]
a.AccessCheck.argtypes = [ctypes.c_void_p, w.HANDLE, w.DWORD, ctypes.POINTER(GenericMapping),
                         ctypes.c_void_p, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD),
                         ctypes.POINTER(w.BOOL)]
a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
token = w.HANDLE()
assert a.OpenProcessToken(k.GetCurrentProcess(), 10, ctypes.byref(token)), ctypes.get_last_error()
def info(kind):
    size = w.DWORD()
    a.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
    assert size.value, (kind, ctypes.get_last_error())
    value = ctypes.create_string_buffer(size.value)
    assert a.GetTokenInformation(token, kind, value, len(value), ctypes.byref(size)), ctypes.get_last_error()
    return value
def lpac_info():
    # Class 46 is unavailable through GetTokenInformation on these runners.
    # Independently check the real token's AAP denial and ARAP positive control.
    duplicate = w.HANDLE()
    assert a.DuplicateToken(token, 2, ctypes.byref(duplicate)), ctypes.get_last_error()
    try:
        outcomes = []
        mapping = GenericMapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
        for package in ('S-1-15-2-1', 'S-1-15-2-2'):
            descriptor = ctypes.c_void_p()
            assert a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                'O:SYG:SYD:(A;;0x1;;;WD)(A;;0x1;;;' + package + ')',
                1, ctypes.byref(descriptor), None), ctypes.get_last_error()
            try:
                privileges = ctypes.create_string_buffer(256)
                size, granted, allowed = w.DWORD(len(privileges)), w.DWORD(), w.BOOL()
                assert a.AccessCheck(descriptor, duplicate, 1, ctypes.byref(mapping), privileges,
                                     ctypes.byref(size), ctypes.byref(granted), ctypes.byref(allowed)), ctypes.get_last_error()
                outcomes.append((bool(allowed.value), granted.value))
            finally:
                k.LocalFree(descriptor)
        return int(outcomes == [(False, 0), (True, 1)])
    finally:
        k.CloseHandle(duplicate)
try:
    ac = ctypes.cast(info(29), ctypes.POINTER(w.DWORD)).contents.value
    lpac = lpac_info()
    sid_info = info(31)
    sid = ctypes.cast(sid_info, ctypes.POINTER(ctypes.c_void_p)).contents.value
    text = w.LPWSTR()
    assert sid and a.ConvertSidToStringSidW(sid, ctypes.byref(text)), ctypes.get_last_error()
    try:
        token_result = {'appcontainer': ac, 'lpac': lpac, 'sid': text.value, 'pid': os.getpid()}
    finally:
        k.LocalFree(text)
finally:
    k.CloseHandle(token)
'''

DEVICE_SOURCE = r'''
import ctypes, json
from ctypes import wintypes as w
k = ctypes.WinDLL('kernel32', use_last_error=True)
n = ctypes.WinDLL('ntdll')
class Name(ctypes.Structure):
    _fields_ = [('length', w.USHORT), ('maximum', w.USHORT), ('buffer', w.LPWSTR)]
class Attributes(ctypes.Structure):
    _fields_ = [('length', w.ULONG), ('root', w.HANDLE), ('name', ctypes.POINTER(Name)),
                ('flags', w.ULONG), ('descriptor', ctypes.c_void_p), ('qos', ctypes.c_void_p)]
class IoStatus(ctypes.Structure):
    _fields_ = [('status', ctypes.c_void_p), ('information', ctypes.c_size_t)]
n.NtOpenFile.argtypes = [ctypes.POINTER(w.HANDLE), w.DWORD, ctypes.POINTER(Attributes),
                         ctypes.POINTER(IoStatus), w.DWORD, w.DWORD]
n.NtOpenFile.restype = ctypes.c_int32
k.CloseHandle.argtypes = [w.HANDLE]
device_result = {}
for device, required in (('KsecDD', 0x100003), ('Null', 0x12019f)):
    path = '\\Device\\' + device
    name = Name(len(path.encode('utf-16-le')), len(path.encode('utf-16-le')) + 2, path)
    attributes = Attributes(ctypes.sizeof(Attributes), None, ctypes.pointer(name), 0x40, None, None)
    results = []
    for access in (required, 0x40000, 0x80000, 0x10000, 0x10000000):
        handle, io = w.HANDLE(), IoStatus()
        status = n.NtOpenFile(ctypes.byref(handle), access, ctypes.byref(attributes), ctypes.byref(io), 7, 0)
        results.append(status & 0xffffffff)
        if status == 0:
            assert k.CloseHandle(handle), ctypes.get_last_error()
    device_result[device] = results
'''



@pytest.mark.parametrize("mode", ["workspace-write", "read-only"])
def test_ambient_package_acls_do_not_widen_write_ceiling(native, project, mode):
    aap, arap = (project.parent / name for name in ("aap-secret.txt", "arap-readable.txt"))
    aap.write_text("aap-canary")
    arap.write_text("arap-canary")
    grant(aap, "S-1-15-2-1")
    grant(arap, "S-1-15-2-2")
    source = f"""import ctypes, json
from ctypes import wintypes as w
from pathlib import Path
a = ctypes.WinDLL('advapi32', use_last_error=True)
a.SetNamedSecurityInfoW.argtypes = [w.LPWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
a.SetNamedSecurityInfoW.restype = w.DWORD
outcome = {{'arap_read': Path({str(arap)!r}).read_text(), 'denied': []}}
for action in [lambda: Path({str(aap)!r}).read_text(),
               lambda: Path({str(arap)!r}).write_text('bad'),
               lambda: Path({str(arap)!r}).unlink()]:
    try:
        action()
    except PermissionError:
        outcome['denied'].append(True)
    else:
        outcome['denied'].append(False)
outcome['acl_denied'] = a.SetNamedSecurityInfoW({str(arap)!r}, 1, 4, None, None, None, None) == 5
print(json.dumps(outcome))
"""
    native.services["execution"].select_mode("read-only" if mode == "read-only" else "workspace-edit")
    assert data(run(native, command(source))) == {"arap_read": "arap-canary", "denied": [True] * 3, "acl_denied": True}
    assert aap.read_text() == "aap-canary" and arap.read_text() == "arap-canary"


def test_existing_metadata_and_runtime_acls_cannot_be_replaced(native, project):
    paths = [project / name for name in (".git/config", ".virgo/protected.txt", "AGENTS.md", "CLAUDE.md")]
    with tempfile.TemporaryDirectory(prefix="virgo-native-readonly-", dir=sys.prefix) as directory:
        paths.append(Path(directory) / "runtime-canary.txt")
        for path in paths:
            path.parent.mkdir(exist_ok=True)
            path.write_text("protected")
        source = f"""import ctypes, json
from ctypes import wintypes as w
from pathlib import Path
a = ctypes.WinDLL('advapi32', use_last_error=True)
a.SetNamedSecurityInfoW.argtypes = [w.LPWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
a.SetNamedSecurityInfoW.restype = w.DWORD
results = []
Path('ordinary-control.txt').write_text('allowed')
Path('ordinary-directory').mkdir()
Path('ordinary-directory').rmdir()
for name in {[str(path) for path in paths]!r}:
    p = Path(name)
    assert p.read_text() == 'protected'
    denied = []
    for action in [lambda: p.write_text('bad'), lambda: p.unlink(), lambda: p.rename(str(p) + '.moved')]:
        try:
            action()
        except PermissionError:
            denied.append(True)
        else:
            denied.append(False)
    denied.append(a.SetNamedSecurityInfoW(name, 1, 4, None, None, None, None) == 5)
    results.append(denied)
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
k.CreateFileW.restype = w.HANDLE
k.CloseHandle.argtypes = [w.HANDLE]
directories = []
for name in ['.git', '.virgo']:
    # Request DELETE on the directory itself; this checks authority without
    # conflating a nonempty directory with an access-denied result.
    handle = k.CreateFileW(name, 0x10000, 7, None, 3, 0x02000000, None)
    denied = [handle == ctypes.c_void_p(-1).value and ctypes.get_last_error() == 5]
    if handle != ctypes.c_void_p(-1).value:
        k.CloseHandle(handle)
    try:
        Path(name).rename(name + '-moved')
    except PermissionError:
        denied.append(True)
    else:
        denied.append(False)
    denied.append(a.SetNamedSecurityInfoW(name, 1, 4, None, None, None, None) == 5)
    directories.append(denied)
print(json.dumps({{'files': results, 'directories': directories}}))
"""
        assert data(run(native, command(source))) == {"files": [[True] * 4 for _ in paths],
                                             "directories": [[True] * 3, [True] * 3]}
        assert all(path.read_text() == "protected" for path in paths)
        assert (project / "ordinary-control.txt").read_text() == "allowed"


def test_child_lpac_token_and_host_process_access(native):
    child_code = TOKEN_SOURCE + "\nprint(json.dumps(token_result))\n"
    source = TOKEN_SOURCE + f"""
import subprocess, sys
parent = token_result
child = json.loads(subprocess.check_output([sys.executable, '-I', '-S', '-c', {child_code!r}]))
k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k.OpenProcess.restype = w.HANDLE
denied = []
for access in (0x2, 0x8, 0x10, 0x20, 0x40):
    handle = k.OpenProcess(access, False, {os.getpid()})
    denied.append(not handle and ctypes.get_last_error() == 5)
    if handle:
        k.CloseHandle(handle)
print(json.dumps({{'parent': parent, 'child': child, 'host_process_denied': denied}}))
"""
    outcome = data(run(native, command(source)))
    for token in (outcome["parent"], outcome["child"]):
        assert token["appcontainer"] == token["lpac"] == 1
        assert token["sid"].startswith("S-1-15-2-")
    assert outcome["parent"]["sid"] == outcome["child"]["sid"]
    assert outcome["host_process_denied"] == [True] * 5
    # Separate payload keeps the encoded PowerShell command below the native
    # command-line limit. The root and its child must inherit the same narrow
    # device authority, never permission to rewrite/delete the kernel object.
    child_code = DEVICE_SOURCE + "\nprint(json.dumps(device_result))\n"
    source = DEVICE_SOURCE + f"""
import subprocess, sys
child = json.loads(subprocess.check_output([sys.executable, '-I', '-S', '-c', {child_code!r}]))
print(json.dumps([device_result, child]))
"""
    expected = {name: [0, *([0xC0000022] * 4)] for name in DEVICE_ACCESS}  # STATUS_ACCESS_DENIED.
    assert data(run(native, command(source))) == [expected, expected]


def test_core_create_during_command_preflight_preserves_workspace_writes(native, project, monkeypatch):
    from virgo.sandbox import windows_acl
    from virgo.tools.bash import run_command

    target = project / "created-during-preflight.txt"
    entered, release = threading.Event(), threading.Event()
    dispatched, completed = threading.Event(), threading.Event()
    ready = project / "reader-ready"
    observations = {}
    grant_tree = windows_acl.AclGrants.grant_tree
    executor = native.main.execution_context.executor
    run_file_operation = executor.run_file_operation

    def observed_preflight(owner, root, *args, **kwargs):
        if root == project:
            entered.set()
            assert release.wait(20)
        return grant_tree(owner, root, *args, **kwargs)

    def observed_operation(operation, arguments, **kwargs):
        mutation = operation == "write" and arguments.get("path") == target.name
        if mutation:
            dispatched.set()
        result = run_file_operation(operation, arguments, **kwargs)
        if mutation:
            completed.set()
        return result

    monkeypatch.setattr(windows_acl.AclGrants, "grant_tree", observed_preflight)
    monkeypatch.setattr(executor, "run_file_operation", observed_operation)
    source = """from pathlib import Path
import time
Path('reader-ready').touch()
deadline = time.monotonic() + 20
p = Path('created-during-preflight.txt')
while not p.exists():
    assert time.monotonic() < deadline
    time.sleep(.05)
assert p.read_text() == 'created'
with p.open('a') as stream:
    stream.write(' and updated')
"""

    def launch():
        observations["launch"] = run_command(command(source), project, 40, threading.Event(),
                                             interpreter=selected(native), execution_context=native.main.execution_context)

    def write():
        faux.script("test", faux.reply(tools=[("write", {"path": target.name, "content": "created"})]),
                    faux.reply("done"))
        native.prompt("Save the new workspace file")
        observations["write"] = tool_results(native)[-1]

    shell = threading.Thread(target=launch)
    writer = threading.Thread(target=write)
    shell.start()
    try:
        assert entered.wait(20)
        writer.start()
        assert dispatched.wait(15), "The file tool must dispatch its helper while native preflight owns the mutex"
        assert not completed.wait(0.25), "The helper must wait for workspace grant classification before launching"
    finally:
        release.set()
        if writer.ident is not None:
            writer.join(30)
        shell.join(45)
    out, code, status = observations["launch"]
    assert code == 0 and status == "ok" and ready.exists(), out.text
    assert completed.is_set() and not observations["write"].is_error
    assert target.read_text() == "created and updated"
