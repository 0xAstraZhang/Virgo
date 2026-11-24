"""Native Standard AppContainer, file boundary, and process-lifetime acceptance on Windows."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import pytest

if sys.platform == "win32":
    from ctypes import wintypes as w

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage, UserMessage
from virgo.harness.extensions import ExtensionAPI
from virgo.harness.store import SessionStore

from tests.support import native_cases
from tests.support.harness import tool_results, wait_until
from tests.support.sandbox import native_session, python_command, run
from tests.support.shell import selected

_BACKGROUND_CLEANUP_TIMEOUT = 30


@pytest.fixture
def native(make_session):
    return native_session(make_session, {"win32"})


def _is_dos_drive_mapped(drive: str | None) -> bool:
    if not drive:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.QueryDosDeviceW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    kernel.QueryDosDeviceW.restype = w.DWORD
    buf = ctypes.create_unicode_buffer(4096)
    return bool(kernel.QueryDosDeviceW(drive, buf, len(buf)))


def command(source: str) -> str:
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    bootstrap = f"import base64;exec(base64.b64decode('{encoded}'))"
    return "& " + " ".join(
        "'" + value.replace("'", "''") + "'"
        for value in [sys.executable, "-I", "-S", "-c", bootstrap]
    )


def payload_json(output: str):
    return json.loads(output.rstrip().rsplit("\n", 1)[-1])


def data(result):
    assert not result.is_error, result.text()
    return payload_json(result.text())


def dacl(path: Path) -> str:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [
        w.LPWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetNamedSecurityInfoW.restype = w.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ctypes.c_void_p,
    ]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, None, None, ctypes.byref(descriptor),
    )
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


def process_sids(pid: int, information: int) -> list[str]:
    """Read the actual host or sandbox process token SIDs without broker internals."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [
        w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD),
    ]
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
        assert advapi.GetTokenInformation(
            token, information, buffer, len(buffer), ctypes.byref(size),
        ), ctypes.get_last_error()
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


STANDARD_APPCONTAINER_TOKEN_SOURCE = r"""
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
def check_sddl(sddl):
    dup = w.HANDLE()
    assert a.DuplicateToken(token, 2, ctypes.byref(dup)), ctypes.get_last_error()
    try:
        mapping = GenericMapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
        desc = ctypes.c_void_p()
        assert a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(desc), None), ctypes.get_last_error()
        try:
            privs = ctypes.create_string_buffer(256)
            size, granted, allowed = w.DWORD(len(privs)), w.DWORD(), w.BOOL()
            assert a.AccessCheck(desc, dup, 1, ctypes.byref(mapping), privs,
                                 ctypes.byref(size), ctypes.byref(granted), ctypes.byref(allowed)), ctypes.get_last_error()
            return bool(allowed.value)
        finally:
            k.LocalFree(desc)
    finally:
        k.CloseHandle(dup)
try:
    ac = ctypes.cast(info(29), ctypes.POINTER(w.DWORD)).contents.value
    restricted_count = ctypes.cast(info(11), ctypes.POINTER(w.DWORD)).contents.value
    aap_granted = check_sddl('O:SYG:SYD:(A;;0x1;;;WD)(A;;0x1;;;S-1-15-2-1)')
    everyone_only_denied = not check_sddl('O:SYG:SYD:(A;;0x1;;;WD)')
    sid_info = info(31)
    sid = ctypes.cast(sid_info, ctypes.POINTER(ctypes.c_void_p)).contents.value
    text = w.LPWSTR()
    assert sid and a.ConvertSidToStringSidW(sid, ctypes.byref(text)), ctypes.get_last_error()
    try:
        token_result = {
            'appcontainer': ac,
            'aap_granted': aap_granted,
            'everyone_only_denied': everyone_only_denied,
            'restricted_sids_count': restricted_count,
            'sid': text.value,
            'pid': os.getpid(),
        }
    finally:
        k.LocalFree(text)
finally:
    k.CloseHandle(token)
"""


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
        cmd_exe = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
        subprocess.run(
            [cmd_exe, "/d", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
        target = link / protected.name
    else:
        target = project / "alias.txt"
        os.link(protected, target)
    session = broker_session(
        faux.reply(tools=[("write", {"path": str(target), "content": "bad"})]),
        faux.reply("done"),
    )
    session.prompt("Attempt the alias mutation")
    if kind in {"junction", "metadata-junction"}:
        assert tool_results(session)[-1].is_error
    else:
        # Broker atomic replacement stages a temp file and replaces the workspace
        # link entry with a new inode, leaving external hardlink targets untouched.
        assert os.stat(target).st_ino != os.stat(protected).st_ino
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
    child = api.create_runtime(
        "original-child",
        "subagent",
        parent=native.main,
        cwd=Path(str(nested).swapcase()) / ".." / nested.name,
    )
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
        faux.script(
            "test",
            faux.reply(tools=[(selected(native).tool_name, {"command": python_command(native, source)})]),
            faux.reply("done"),
        )
        child.agent.prompt("Exercise the original runtime workspace")
        result = next(m for m in reversed(child.messages) if isinstance(m, ToolResultMessage))
        assert not result.is_error and "outside-denied" in result.text(), result.text()
        assert result.details["boundary"]["workspace"] == str(project.resolve())
        assert (nested / "child-success.txt").read_text() == "origin" and secret.read_text() == "outside"
        faux.script(
            "test",
            faux.reply(tools=[(selected(native).tool_name, {
                "command": python_command(native, "from pathlib import Path; Path('escaped.txt').touch()"),
            })]),
            faux.reply("done"),
        )
        escaped.agent.prompt("Attempt to change the sandbox root using cwd")
        result = next(m for m in reversed(escaped.messages) if isinstance(m, ToolResultMessage))
        assert result.is_error and result.details["status"] == "execution_unavailable", result.text()
        assert not (outside / "escaped.txt").exists() and not (outside / ".virgo").exists()
    finally:
        api.release_runtime(child)
        api.release_runtime(escaped)


def test_credentials_and_unlisted_handles_are_not_inherited(native, project, monkeypatch):
    import msvcrt

    secret = project.parent / "handle-secret.txt"
    secret.write_bytes(b"private-handle-canary")
    stripped = [
        "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
        "SSH_AUTH_SOCK", "VIRGO_HOME", "PYTHONPATH", "BASH_ENV", "ENV",
    ]
    for key in stripped:
        monkeypatch.setenv(key, "must-not-reach-payload")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    with secret.open("rb") as stream:
        handle = msvcrt.get_osfhandle(stream.fileno())
        os.set_handle_inheritable(handle, True)
        source = f"""import ctypes, json, os
from ctypes import wintypes
assert [os.environ.get(key) for key in {stripped!r}] == [None] * {len(stripped)}
assert os.environ.get('HTTP_PROXY') == 'http://127.0.0.1:8080'
assert os.environ.get('HTTPS_PROXY') == 'http://127.0.0.1:8080'
assert os.environ.get('USERPROFILE')
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
content = ctypes.create_string_buffer(128)
count = wintypes.DWORD()
try:
    success = kernel.ReadFile({handle}, content, 128, ctypes.byref(count), None)
except OSError as error:
    if (getattr(error, 'winerror', 0) & 0xffffffff) != 0xC0000008:
        raise
    success = False
assert not success or b'private-handle-canary' not in content.raw
print('credentials and handle protected')
"""
        result = run(native, python_command(native, source))
    assert not result.is_error and "credentials and handle protected" in result.text(), result.text()


@pytest.mark.parametrize("finish", ["exit", "timeout", "cancel"])
def test_completion_drains_descendants_before_scratch_cleanup(native, project, finish):
    child = (
        "import time; from pathlib import Path; Path('child-ready').touch(); "
        "time.sleep(6); Path('late-write').touch()"
    )
    source = f"""import json, os, subprocess, sys, time
from pathlib import Path
p = subprocess.Popen([sys.executable, '-I', '-S', '-c', {child!r}], creationflags=0x200,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path('lifetime.json').write_text(json.dumps({{'pid': p.pid, 'scratch': os.environ['TMPDIR']}}))
deadline = time.monotonic() + 10
while not Path('child-ready').exists():
    assert time.monotonic() < deadline
    time.sleep(.02)
""" + ("time.sleep(25)" if finish != "exit" else "print('complete')")
    results = []

    def execute():
        results.append(run(native, python_command(native, source), timeout=3 if finish == "timeout" else 30))

    if finish == "cancel":
        worker = threading.Thread(target=execute)
        worker.start()
        try:
            deadline = time.monotonic() + 15
            while not (project / "child-ready").exists() and worker.is_alive():
                assert time.monotonic() < deadline
                time.sleep(0.02)
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
    assert result.details["status"] == {"exit": "ok", "timeout": "timeout", "cancel": "aborted"}[finish], result.text()
    assert result.details["boundary"]["cleanup"] == "executor_completed"
    lifetime = json.loads((project / "lifetime.json").read_text())
    assert not Path(lifetime["scratch"]).exists()
    deadline = time.monotonic() + 6.2
    while not (project / "late-write").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not (project / "late-write").exists(), "Descendant retained workspace access after cleanup"


def test_background_cancel_preserves_other_job_and_origin_after_switch(native, project):
    jobs = native.services["jobs"]

    def start_job(marker):
        cmd = python_command(
            native,
            "import time\n"
            f"with open({marker!r}, 'w'):\n"
            f"    time.sleep({2 * _BACKGROUND_CLEANUP_TIMEOUT})",
        )
        result = run(native, cmd, background=True)
        assert not result.is_error, result.text()
        return list(jobs.jobs.values())[-1]

    first = start_job("first-ready")
    wait_until((project / "first-ready").exists)
    second = start_job("second-ready")
    try:
        wait_until((project / "second-ready").exists)
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


def test_readonly_network_and_workspace_edit_loopback_denied(native):
    """Read-Only denies TCP/DNS/UDP/loopback; Workspace-Edit denies loopback without CheckNetIsolation."""
    provider = native.services["execution"]
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv4,
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as srv6,
    ):
        srv4.bind(("127.0.0.1", 0))
        srv4.listen()
        srv6.bind(("::1", 0))
        srv6.listen()
        port4 = srv4.getsockname()[1]
        port6 = srv6.getsockname()[1]

        loopback_source = f"""import json, socket
def try_connect(family, dest, timeout=2.0):
    try:
        with socket.socket(family) as s:
            s.settimeout(timeout)
            s.connect(dest)
        return {{'allowed': True}}
    except OSError as exc:
        return {{'allowed': False, 'type': type(exc).__name__, 'winerror': getattr(exc, 'winerror', None), 'errno': exc.errno}}

out = {{
    'loopback_v4': try_connect(socket.AF_INET, ('127.0.0.1', {port4})),
    'loopback_v6': try_connect(socket.AF_INET6, ('::1', {port6})),
}}
print(json.dumps(out))
"""
        readonly_probe_source = f"""import json, socket
out = {{}}
def try_connect(family, dest, timeout=2.0):
    try:
        with socket.socket(family) as s:
            s.settimeout(timeout)
            s.connect(dest)
        return {{'allowed': True}}
    except OSError as exc:
        return {{'allowed': False, 'type': type(exc).__name__, 'winerror': getattr(exc, 'winerror', None), 'errno': exc.errno}}

out['loopback_v4'] = try_connect(socket.AF_INET, ('127.0.0.1', {port4}))
out['loopback_v6'] = try_connect(socket.AF_INET6, ('::1', {port6}))
out['tcp_1_1_1_1'] = try_connect(socket.AF_INET, ('1.1.1.1', 443), timeout=3.0)
try:
    socket.getaddrinfo('example.com', 443)
    out['dns'] = {{'allowed': True}}
except OSError as exc:
    out['dns'] = {{'allowed': False, 'type': type(exc).__name__, 'errno': exc.errno}}
try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
        u.settimeout(2.0)
        q = b'\\x12\\x34\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x07example\\x03com\\x00\\x00\\x01\\x00\\x01'
        u.sendto(q, ('1.1.1.1', 53))
        u.recvfrom(512)
    out['udp_dns'] = {{'allowed': True}}
except OSError as exc:
    out['udp_dns'] = {{'allowed': False, 'type': type(exc).__name__, 'winerror': getattr(exc, 'winerror', None)}}
print(json.dumps(out))
"""
        provider.select_mode("workspace-edit")
        we_out = data(run(native, python_command(native, loopback_source)))
        assert we_out["loopback_v4"]["allowed"] is False, we_out
        assert we_out["loopback_v6"]["allowed"] is False, we_out

        provider.select_mode("read-only")
        ro_out = data(run(native, python_command(native, readonly_probe_source)))
        assert ro_out["loopback_v4"]["allowed"] is False, ro_out
        assert ro_out["loopback_v6"]["allowed"] is False, ro_out
        assert ro_out["tcp_1_1_1_1"]["allowed"] is False, ro_out
        assert ro_out["dns"]["allowed"] is False, ro_out
        assert ro_out["udp_dns"]["allowed"] is False, ro_out


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native sandbox test")
def test_session_reuses_sid_releases_per_command_scratch_and_cleans_on_shutdown(make_session, project):
    """Repeated commands reuse one session SID, clean per-command TMPDIR, and leave zero residuals on shutdown."""
    (project / ".virgo").mkdir(exist_ok=True)
    (project / ".virgo" / "config.json").write_text("{}\n")
    session = native_session(make_session, {"win32"})
    from virgo.sandbox.windows_acl import scan_residual_aces
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.GetProcessHandleCount.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]

    def handle_count() -> int:
        value = w.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value))
        return value.value

    before = handle_count()
    scratches: set[str] = set()
    sids: set[str] = set()
    profile_temp: Path | None = None
    allocated_drives: list[str] = []
    for index in range(4):
        source = (
            STANDARD_APPCONTAINER_TOKEN_SOURCE
            + f"""
from pathlib import Path
Path('new-{index}.txt').write_text('ordinary')
print(json.dumps({{'sid': token_result['sid'], 'scratch': os.environ['TMPDIR'], 'session_temp': os.environ['TEMP']}}))
"""
        )
        rec = data(run(session, command(source)))
        sids.add(rec["sid"])
        scratch = rec["scratch"]
        profile_temp = Path(rec["session_temp"])
        assert scratch not in scratches and not Path(scratch).exists()
        scratches.add(scratch)
        assert (project / f"new-{index}.txt").read_text() == "ordinary"

    pool = session.services["execution"].executor.native._windows_pool
    if pool is not None:
        allocated_drives.extend(drive for drive, _target in pool._workspace_drives.values())

    assert len(sids) == 1, f"Expected session-level SID reuse across commands, got {sids}"
    session_sid = next(iter(sids))
    assert handle_count() - before < 16, "Repeated executor launches leaked broker handles"

    session.shutdown()
    assert scan_residual_aces(project, session_sid) == []
    assert profile_temp is not None and not profile_temp.exists()
    for drive in allocated_drives:
        assert not _is_dos_drive_mapped(drive), f"Virtual drive {drive} still mapped after shutdown"


@pytest.mark.parametrize("mode", ["workspace-edit", "read-only"])
def test_private_user_files_denied_and_aap_system_files_readonly(native, project, mode):
    """Non-AAP private user directories are unreadable & unwritable; AAP-RX system files stay read-only."""
    userprofile = Path(os.environ["USERPROFILE"])
    canary_dir = userprofile / f".virgo_e2e_private_{uuid.uuid4().hex[:8]}"
    canary_dir.mkdir(exist_ok=True)
    canary_file = canary_dir / "secret.txt"
    canary_file.write_text("private-secret")
    ssh_dir = userprofile / ".ssh"
    hosts_file = Path(os.environ["SystemRoot"]) / "System32" / "drivers" / "etc" / "hosts"

    try:
        source = f"""import json, os
from pathlib import Path

out = {{}}
def check_blocked(fn):
    try:
        fn()
    except PermissionError:
        return True
    return False

out['canary_dir_list_denied'] = check_blocked(lambda: os.listdir({str(canary_dir)!r}))
out['canary_read_denied'] = check_blocked(lambda: Path({str(canary_file)!r}).read_text())
out['canary_write_denied'] = check_blocked(lambda: Path({str(canary_file)!r}).write_text('bad'))
out['canary_create_denied'] = check_blocked(lambda: Path({str(canary_dir / 'escape.txt')!r}).write_text('bad'))
if {ssh_dir.exists()!r}:
    out['ssh_list_denied'] = check_blocked(lambda: os.listdir({str(ssh_dir)!r}))
else:
    out['ssh_list_denied'] = True
out['system_hosts_readable'] = len(Path({str(hosts_file)!r}).read_bytes()) > 0
out['system_hosts_write_denied'] = check_blocked(lambda: open({str(hosts_file)!r}, 'ab').write(b'#'))
print(json.dumps(out))
"""
        native.services["execution"].select_mode(mode)
        outcome = data(run(native, command(source)))
        assert outcome == {
            "canary_dir_list_denied": True,
            "canary_read_denied": True,
            "canary_write_denied": True,
            "canary_create_denied": True,
            "ssh_list_denied": True,
            "system_hosts_readable": True,
            "system_hosts_write_denied": True,
        }, outcome
        assert canary_file.read_text() == "private-secret"
    finally:
        shutil.rmtree(canary_dir, ignore_errors=True)


def test_existing_metadata_and_runtime_acls_cannot_be_replaced(native, project):
    """VER-01 protected .virgo & runtime roots stay read-only while .git/AGENTS.md and host atomic saves work."""
    paths = [project / ".virgo/protected.txt"]
    (project / ".git").mkdir(exist_ok=True)
    (project / ".git/config").write_text("git-ok")
    (project / "AGENTS.md").write_text("agents-ok")
    # Use default mode (0o777) rather than tempfile.TemporaryDirectory (0o700 sets SE_DACL_PROTECTED
    # in CPython 3.13, blocking inheritance of the session SID's read-only ACE from sys.prefix).
    runtime_dir = Path(sys.prefix) / f"virgo-native-readonly-{uuid.uuid4().hex[:8]}"
    runtime_dir.mkdir()
    try:
        paths.append(runtime_dir / "runtime-canary.txt")
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
Path('.git/config').write_text('git-updated')
Path('AGENTS.md').write_text('agents-updated')
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
for name in ['.virgo']:
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
        assert data(run(native, command(source))) == {
            "files": [[True] * 4 for _ in paths],
            "directories": [[True] * 3],
        }
        assert all(path.read_text() == "protected" for path in paths)
        assert (project / "ordinary-control.txt").read_text() == "allowed"
        assert (project / ".git/config").read_text() == "git-updated"
        assert (project / "AGENTS.md").read_text() == "agents-updated"

        # Ecosystem compatibility: while session ACEs remain active, host atomic os.replace
        # over normal files and protected .virgo metadata must succeed with zero WinError 5.
        tmp_normal = project / ".ordinary-control.tmp"
        tmp_normal.write_text("host-updated")
        os.replace(tmp_normal, project / "ordinary-control.txt")
        assert (project / "ordinary-control.txt").read_text() == "host-updated"

        tmp_virgo = project / ".virgo-protected.tmp"
        tmp_virgo.write_text("host-virgo-updated")
        os.replace(tmp_virgo, project / ".virgo/protected.txt")
        assert (project / ".virgo/protected.txt").read_text() == "host-virgo-updated"
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_standard_appcontainer_token_primitives_and_host_process_isolation(native):
    """Verify Standard AppContainer token, host OpenProcess denial, and RNG/TLS/NUL without device ACL edits."""
    child_code = STANDARD_APPCONTAINER_TOKEN_SOURCE + "\nprint(json.dumps(token_result))\n"
    source = (
        STANDARD_APPCONTAINER_TOKEN_SOURCE
        + f"""
import hashlib, secrets, ssl, subprocess, sys
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
bcrypt = ctypes.WinDLL('bcrypt.dll')
buf = (ctypes.c_ubyte * 32)()
bcrypt_status = bcrypt.BCryptGenRandom(None, buf, 32, 0x2)
ctx = ssl.create_default_context()
with open('NUL', 'wb') as nul:
    nul_bytes = nul.write(b'discard')
devnull_rc = subprocess.run(
    [sys.executable, '-I', '-S', '-c', 'print(1)'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
).returncode
print(json.dumps({{
    'parent': parent,
    'child': child,
    'host_process_denied': denied,
    'urandom_len': len(os.urandom(32)),
    'secrets_len': len(secrets.token_hex(16)),
    'bcrypt_status': bcrypt_status,
    'ssl_certs': ctx.cert_store_stats().get('x509', 0),
    'sha256_len': len(hashlib.sha256(b'virgo').hexdigest()),
    'nul_bytes': nul_bytes,
    'devnull_rc': devnull_rc,
}}))
"""
    )
    outcome = data(run(native, command(source)))
    for token in (outcome["parent"], outcome["child"]):
        assert token["appcontainer"] == 1
        assert token["aap_granted"] is True
        assert token["everyone_only_denied"] is True
        assert token["restricted_sids_count"] == 0
        assert token["sid"].startswith("S-1-15-2-")
    assert outcome["parent"]["sid"] == outcome["child"]["sid"]
    assert outcome["host_process_denied"] == [True] * 5
    assert outcome["urandom_len"] == 32
    assert outcome["secrets_len"] == 32
    assert outcome["bcrypt_status"] == 0
    assert outcome["ssl_certs"] > 0
    assert outcome["sha256_len"] == 64
    assert outcome["nul_bytes"] == 7
    assert outcome["devnull_rc"] == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native sandbox test")
@pytest.mark.parametrize("tool", ["write", "edit"])
def test_core_mutation_preserves_live_background_workspace_writes(make_session, project, tool):
    """Broker atomic write/edit inherits active session SID cleanly and leaves zero residuals after shutdown."""
    (project / ".virgo").mkdir(exist_ok=True)
    (project / ".virgo" / "config.json").write_text("{}\n")
    session = native_session(make_session, {"win32"})
    from virgo.sandbox.windows_acl import scan_residual_aces
    target = project / "ordinary.txt"
    target.write_text("original\n")
    created = project / "created-by-core.txt"
    jobs = session.services["jobs"]
    result = run(
        session,
        command(f"""import time
from pathlib import Path
with Path('ordinary.txt').open('a') as stream:
    stream.write('before\\n')
Path('mutation-ready').touch()
deadline = time.monotonic() + 45
while not Path('release-mutation').exists():
    assert time.monotonic() < deadline
    time.sleep(.02)
assert Path('ordinary.txt').read_text() == 'changed\\n'
with Path('ordinary.txt').open('a') as stream:
    stream.write('after\\n')
if {tool == "write"}:
    assert Path('created-by-core.txt').read_text() == 'created\\n'
    with Path('created-by-core.txt').open('a') as stream:
        stream.write('after\\n')
"""),
        background=True,
    )
    assert not result.is_error, result.text()
    job = list(jobs.jobs.values())[-1]
    package_sid = None
    try:
        wait_until(lambda: (project / "mutation-ready").exists() or job.done.is_set(), timeout=25)
        assert (project / "mutation-ready").exists(), jobs.output(job)
        assert target.read_text() == "original\nbefore\n"
        (package_sid,) = process_sids(job.proc.pid, 31)
        metadata_dacl = dacl(project / ".virgo")
        arguments = (
            {"path": target.name, "content": "changed\n"}
            if tool == "write"
            else {"path": target.name, "edits": [{"old_text": "original\nbefore\n", "new_text": "changed\n"}]}
        )
        faux.script("test", faux.reply(tools=[(tool, arguments)]), faux.reply("done"))
        session.prompt("Update the ordinary file while its background command is waiting")
        mutation = tool_results(session)[-1]
        assert not mutation.is_error, mutation.text()
        assert target.read_text() == "changed\n"
        if tool == "write":
            faux.script(
                "test",
                faux.reply(tools=[("write", {"path": created.name, "content": "created\n"})]),
                faux.reply("done"),
            )
            session.prompt("Create another ordinary file while the background command is waiting")
            mutation = tool_results(session)[-1]
            assert not mutation.is_error, mutation.text()
            assert created.read_text() == "created\n"
        assert dacl(project / ".virgo") == metadata_dacl
        (project / "release-mutation").touch()
        assert job.done.wait(15), "background command did not drain after Core mutation"
        assert job.status == "completed", jobs.output(job)
        assert target.read_text() == "changed\nafter\n"
        if tool == "write":
            assert created.read_text() == "created\nafter\n"
    finally:
        if not job.done.is_set():
            jobs.kill(job)
            assert job.done.wait(15), "fixture must drain its background command"

    session.shutdown()
    assert package_sid is not None
    assert scan_residual_aces(project, package_sid) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native sandbox test")
def test_crash_recovery_after_terminate_process(tmp_path):
    """Hard-killing a broker holding an active session leaves recoverable state cleaned idempotently."""
    from virgo.sandbox.windows_acl import scan_residual_aces
    from virgo.sandbox.windows_sandbox import is_process_alive, recover_stale_sessions

    ws = tmp_path / "crash_ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# protected\n", encoding="utf-8")
    (ws / ".virgo").mkdir()
    (ws / ".virgo" / "config.json").write_text("{}", encoding="utf-8")
    state_dir = tmp_path / "crash_state"
    state_dir.mkdir()
    # Malformed journals in the same directory must not block recovering valid stale sessions.
    (state_dir / "000-malformed-list.json").write_text("[]", encoding="utf-8")
    (state_dir / "001-malformed-pid.json").write_text('{"owner_pid": "bad"}', encoding="utf-8")

    worker = f"""import json, os, sys, time
from pathlib import Path
from virgo.sandbox.windows_sandbox import WindowsSessionPool
os.environ['VIRGO_WINDOWS_SANDBOX_STATE_DIR'] = {str(state_dir)!r}
ws = Path({str(ws)!r})
pool = WindowsSessionPool(journal_dir=Path({str(state_dir)!r}))
session = pool.get_or_create(ws, read_only=False, network_allowed=False)
session.ensure_protected_paths([ws / 'AGENTS.md', ws / '.virgo'])
print(json.dumps({{
    'pid': os.getpid(),
    'sid': session.package_name,
    'profile_name': session.profile_name,
    'profile_path': str(session.profile_path),
    'state_file': str(session.state_file),
    'dos_drive': session.dos_drive,
}}), flush=True)
time.sleep(120)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", worker],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        ready_line = proc.stdout.readline().strip()
        assert ready_line, proc.stderr.read() if proc.stderr else "worker exited before ready"
        info = json.loads(ready_line)
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert not is_process_alive(info["pid"])
    assert Path(info["state_file"]).exists()
    assert Path(info["profile_path"]).exists()
    assert len(scan_residual_aces(ws, info["sid"])) > 0

    recovered = recover_stale_sessions(state_dir)
    valid_entries = [item for item in recovered if item.get("sid_str") == info["sid"]]
    error_entries = [item for item in recovered if item.get("status") == "error"]
    assert len(valid_entries) == 1 and valid_entries[0]["status"] == "recovered", recovered
    assert valid_entries[0]["errors"] == [], valid_entries
    assert len(error_entries) == 2, recovered
    assert not Path(info["state_file"]).exists()
    assert not Path(info["profile_path"]).exists()
    assert scan_residual_aces(ws, info["sid"]) == []
    assert not _is_dos_drive_mapped(info["dos_drive"])
    # Remove synthetic malformed files and confirm idempotent empty recovery.
    for bad in ("000-malformed-list.json", "001-malformed-pid.json"):
        (state_dir / bad).unlink(missing_ok=True)
    assert recover_stale_sessions(state_dir) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native sandbox test")
def test_concurrent_sessions_preserve_protected_metadata_until_last_closes(tmp_path):
    """Closing one session on a shared workspace must not strip SE_DACL_PROTECTED from a surviving session."""
    from virgo.sandbox.windows_acl import (
        is_dacl_protected,
        is_protected_readonly_for_sid,
        scan_residual_aces,
    )
    from virgo.sandbox.windows_sandbox import WindowsSessionPool

    ws = tmp_path / "shared_ws"
    ws.mkdir()
    agents = ws / "AGENTS.md"
    agents.write_text("# shared policy\n", encoding="utf-8")
    git_dir = ws / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n", encoding="utf-8")
    journal_dir = tmp_path / "shared_journals"

    pool_a = WindowsSessionPool(journal_dir=journal_dir)
    pool_b = WindowsSessionPool(journal_dir=journal_dir)
    try:
        session_a = pool_a.get_or_create(ws, read_only=False, network_allowed=False)
        session_a.ensure_protected_paths([agents, git_dir])
        session_b = pool_b.get_or_create(ws, read_only=False, network_allowed=False)
        session_b.ensure_protected_paths([agents, git_dir])

        if session_a.dos_drive and session_b.dos_drive:
            assert session_a.dos_drive != session_b.dos_drive

        # Close Session A first while Session B remains active on the same workspace.
        pool_a.close()
        assert scan_residual_aces(ws, session_a.package_name) == []
        assert not _is_dos_drive_mapped(session_a.dos_drive)
        assert is_dacl_protected(agents) is True
        assert is_dacl_protected(git_dir) is True
        assert is_protected_readonly_for_sid(agents, session_b.package_name) is True
        assert is_protected_readonly_for_sid(git_dir, session_b.package_name) is True

        # Close Session B last: SE_DACL_PROTECTED must now be cleanly restored to unprotected.
        pool_b.close()
        assert scan_residual_aces(ws, session_b.package_name) == []
        assert not _is_dos_drive_mapped(session_b.dos_drive)
        assert is_dacl_protected(agents) is False
        assert is_dacl_protected(git_dir) is False
    finally:
        for pool in (pool_a, pool_b):
            try:
                pool.close()
            except Exception:
                pass


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native sandbox test")
def test_inaccessible_runtime_fails_closed(tmp_path, monkeypatch, make_session):
    """Non-AAP runtime roots without WRITE_DAC fail closed with reason=windows_runtime_inaccessible."""
    from virgo.execution import ExecutionUnavailable
    from virgo.sandbox import windows_sandbox

    fake_rt = tmp_path / "locked_runtime"
    fake_rt.mkdir()
    fake_exe = fake_rt / "tool.exe"
    fake_exe.write_bytes(b"MZ")

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(windows_sandbox, "has_aap_read_access", lambda _p, **_kw: False)
    monkeypatch.setattr(windows_sandbox, "can_open_write_dac", lambda _p: False)
    with pytest.raises(ExecutionUnavailable) as exc_info:
        windows_sandbox._check_runtime_accessibility([fake_rt], ws, fake_exe)
    assert exc_info.value.reason == "windows_runtime_inaccessible"
    assert exc_info.value.dimension == "windows_runtime_access"
    assert str(fake_rt) in str(exc_info.value)

    # Verify assembled session fails closed on decline and allows explicit one-shot No-Limit (R6).
    from virgo.execution_policy import is_windows_sandbox_supported
    from virgo.sandbox import windows_acl

    if (
        os.environ.get("VIRGO_REQUIRE_SANDBOX_TESTS") != "1"
        and not is_windows_sandbox_supported()
    ):
        monkeypatch.setattr(windows_acl, "null_device_allows_appcontainer", lambda: True)
    session = make_session(
        settings={"execution": {"mode": "workspace-edit"}},
        refresh_models=False,
        extensions=False,
    )
    provider = session.services["execution"]

    session.ui.answers.append("n")
    declined = run(session, "Write-Output 'must-not-run'")
    assert declined.is_error and declined.details["status"] == "execution_unavailable", declined.text()
    assert "--permission no-limit" in declined.text()
    assert provider.selected_mode_id == "workspace-edit"

    session.ui.answers.append("y")
    approved = run(session, "Write-Output 'fallback-ok'")
    assert not approved.is_error and "fallback-ok" in approved.text(), approved.text()
    assert provider.selected_mode_id == "workspace-edit"


def test_unelevated_modes_and_pwsh_virtual_drive_cwd(make_session, project):
    """Verify no-limit/read-only/workspace-edit modes, protected reads across mode switch, and pwsh CWD."""
    (project / "AGENTS.md").write_text("# agents policy\n", encoding="utf-8")
    (project / ".virgo").mkdir(exist_ok=True)
    (project / ".virgo" / "config.json").write_text('{"protected": true}\n', encoding="utf-8")
    session = native_session(make_session, {"win32"})
    from virgo.sandbox.windows_acl import is_dacl_protected

    provider = session.services["execution"]

    # Workspace-Edit: pwsh Get-Location must NOT fall back to C:\ and relative write must succeed
    pwsh_res = run(
        session,
        "$loc = (Get-Location).Path; Set-Content -Path 'pwsh_cwd_check.txt' -Value 'ok'; "
        "$rd = Get-Content -Path 'pwsh_cwd_check.txt'; Write-Output \"LOC=$loc READ=$rd\"",
    )
    assert not pwsh_res.is_error, pwsh_res.text()
    assert "READ=ok" in pwsh_res.text(), pwsh_res.text()
    assert "LOC=C:\\\n" not in pwsh_res.text() and "LOC=C:\\ " not in pwsh_res.text(), pwsh_res.text()
    assert (project / "pwsh_cwd_check.txt").read_text(encoding="utf-8").strip() == "ok"

    # Read-Only (switched from Workspace-Edit): relative read + protected .virgo read work, write denied
    provider.select_mode("read-only")
    ro_res = run(session, "Get-Content -Path 'pwsh_cwd_check.txt'; Get-Content -Path '.virgo/config.json'")
    assert not ro_res.is_error and "ok" in ro_res.text() and "protected" in ro_res.text(), ro_res.text()
    ro_denied = run(session, "Set-Content -Path 'pwsh_ro_blocked.txt' -Value 'no'")
    assert ro_denied.is_error and not (project / "pwsh_ro_blocked.txt").exists()

    # No-Limit: releases idle AppContainer sessions/drives promptly and runs cleanly
    pool = provider.executor.native._windows_pool
    drives_before_nl = [drive for drive, _ in pool._workspace_drives.values()] if pool is not None else []
    provider.select_mode("no-limit")
    assert is_dacl_protected(project / ".virgo") is False
    for drive in drives_before_nl:
        assert not _is_dos_drive_mapped(drive), f"Drive {drive} remained mapped after switching to no-limit"
    nl_res = run(session, "Write-Output 'no-limit-ok'")
    assert not nl_res.is_error and "no-limit-ok" in nl_res.text(), nl_res.text()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native sandbox test")
def test_null_device_support_probe_and_unsupported_fallback(project, monkeypatch):
    """Verify live \\Device\\Null AAP support probe and fail-closed fallback when absent."""
    from virgo.execution import ExecutionUnavailable
    from virgo.execution_policy import default_execution_mode, is_windows_sandbox_supported
    from virgo.harness.session import AgentSession
    from virgo.sandbox import windows_acl, windows_sandbox

    from tests.support.harness import RecordingUI

    # Live supported (Win11 Feature 60692039 / CI-aligned) host allows AppContainer on \Device\Null.
    required = os.environ.get("VIRGO_REQUIRE_SANDBOX_TESTS") == "1"
    if required or windows_acl.null_device_allows_appcontainer():
        assert windows_acl.null_device_allows_appcontainer() is True
        assert is_windows_sandbox_supported() is True
        assert default_execution_mode() == "workspace-edit"

    # Unsupported Windows (\Device\Null without S-1-15-2-1) defaults to No-Limit with 3 options
    # and fails closed on explicit sandbox precheck.
    monkeypatch.setattr(windows_acl, "null_device_allows_appcontainer", lambda: False)
    assert is_windows_sandbox_supported() is False
    assert default_execution_mode() == "no-limit"

    ui = RecordingUI()
    session = AgentSession(
        project, ui=ui, model="faux/test", session=None, extensions=False, refresh_models=False,
    )
    try:
        assert session.services["execution"].selected_mode_id == "no-limit"
        notice = "\n".join(ui.lines)
        assert "WSL2" in notice and "No-Limit" in notice and "Update Windows" in notice
        with pytest.raises(ExecutionUnavailable) as exc_info:
            windows_sandbox.precheck_windows_sandbox()
        assert exc_info.value.reason == "windows_null_device_unavailable"
        msg = str(exc_info.value)
        assert "WSL2" in msg and "No-Limit" in msg and "Update Windows" in msg
    finally:
        session.shutdown()
