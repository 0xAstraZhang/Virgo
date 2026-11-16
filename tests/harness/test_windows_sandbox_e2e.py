"""Windows security acceptance, defined before the native implementation.

Failure modes: an approval replaces OS enforcement; an ordinary or AAP-readable
host secret leaks; an ambient ARAP grant permits host writes; metadata/readonly
ACLs can be replaced; a junction/hardlink broadens grants; children lose their
token/Job; inherited handles/environment expose credentials; network failure is
mistaken for isolation; root exit/cancel leaves descendants or temporary grants;
one command's cleanup revokes another's authority; cwd/current-session replaces
the originating workspace; Core staging revokes a live command's ordinary file
writes; failed setup replays a payload without isolation.

Only the model is scripted. The selected shell, processes, tokens, DACLs,
reparse points, listeners and cancellation are real Windows operations. These
tests intentionally fail on Windows when the requested backend is unavailable.
"""
from __future__ import annotations

import base64
import ctypes
import gc
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from virgo.agent import ToolContext
from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage, UserMessage
from virgo.harness.extensions import ExtensionAPI
from virgo.harness.store import SessionStore
from virgo.tools.bash import run_command
from virgo.tools.sandbox import SandboxPolicy, SandboxUnavailable
from virgo.tools.shell import Shell

from tests.support.harness import tool_results, wait_until
from tests.support.shell import selected

pytestmark = pytest.mark.skipif(os.name != "nt", reason="real Windows LPAC security acceptance")


def command(source: str) -> str:
    encoded = base64.b64encode(source.encode()).decode()
    bootstrap = f"import base64;exec(base64.b64decode('{encoded}'))"
    return "& " + " ".join("'" + value.replace("'", "''") + "'" for value in
                            [sys.executable, "-I", "-S", "-c", bootstrap])


def run(session, source: str, **arguments):
    faux.script("test", faux.reply(tools=[(selected(session).tool_name, {
        "command": command(source), **arguments,
    })]), faux.reply("done"))
    session.prompt("Run the Windows boundary acceptance operation")
    return tool_results(session)[-1]


def payload_json(output: str):
    # These fixed payloads emit one final JSON record. Shell retains combined
    # stdout/stderr, including CPython's measured LPAC DOS-path warning; that
    # diagnostic is not the payload record and must not be filtered by Shell.
    return json.loads(output.rstrip().rsplit("\n", 1)[-1])


def data(result):
    assert not result.is_error, result.text()
    return payload_json(result.text())


@pytest.fixture
def native(make_session):
    executable = shutil.which("pwsh")
    assert executable, "PowerShell 7 is required by the strict Windows acceptance runner"
    session = make_session(settings={
        "profile": "minimal", "shell": {"executable": executable},
        "sandbox": {"mode": "workspace-write", "network": "restricted"},
        "plugins": {"background": {"enabled": True}, "subagent": {"enabled": True}},
    }, refresh_models=False, extensions=False)
    result = run(session, "print('native-ready')")
    assert not result.is_error and "native-ready" in result.text(), result.text()
    assert result.details["sandbox"]["mode"] == "workspace-write"
    return session


def grant(path: Path, sid: str) -> None:
    """Host fixture setup, using a controlled argv and a well-known SID."""
    result = subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"),
                             str(path), "/grant", f"*{sid}:F"], capture_output=True, check=False)
    assert result.returncode == 0, (result.stdout, result.stderr)


def junction(path: Path, target: Path) -> None:
    result = subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"),
                             "/d", "/c", "mklink", "/J", str(path), str(target)],
                            capture_output=True, check=False)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert path.lstat().st_file_attributes & 0x400, "fixture must be a real NTFS reparse point"


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


DEVICE_ACCESS = {"KsecDD": 0x100003, "Null": 0x12019F}


class DeviceSecurity:
    """Independent native observations and a controlled host-only fixture ACE.

    Measured BCrypt startup and subprocess.DEVNULL denials require fixed leases.
    These checks are defined before that mechanism: unrelated ACE bytes/order,
    owner, group and mandatory label must survive overlapping grants/revocation.
    """

    def __init__(self, name="KsecDD"):
        assert name in DEVICE_ACCESS
        self.name = name

    def __enter__(self):
        from ctypes import wintypes as w

        self.w = w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)

        def bind(library, name, arguments, result=w.BOOL):
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
            return function

        pointer = ctypes.c_void_p
        self.close = bind(self.kernel, "CloseHandle", [w.HANDLE])
        self.free = bind(self.kernel, "LocalFree", [pointer], pointer)
        create = bind(self.kernel, "CreateFileW", [
            w.LPCWSTR, w.DWORD, w.DWORD, pointer, w.DWORD, w.DWORD, w.HANDLE,
        ], w.HANDLE)
        self.get = bind(self.advapi, "GetKernelObjectSecurity", [
            w.HANDLE, w.DWORD, pointer, w.DWORD, ctypes.POINTER(w.DWORD),
        ])
        self.get_dacl = bind(self.advapi, "GetSecurityDescriptorDacl", [
            pointer, ctypes.POINTER(w.BOOL), ctypes.POINTER(pointer), ctypes.POINTER(w.BOOL),
        ])
        self.get_control = bind(self.advapi, "GetSecurityDescriptorControl", [
            pointer, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD),
        ])
        self.get_ace = bind(self.advapi, "GetAce", [pointer, w.DWORD, ctypes.POINTER(pointer)])
        self.to_sddl = bind(self.advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW", [
            pointer, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), pointer,
        ])
        self.to_sid = bind(self.advapi, "ConvertStringSidToSidW", [w.LPCWSTR, ctypes.POINTER(pointer)])
        self.sid_length = bind(self.advapi, "GetLengthSid", [pointer], w.DWORD)
        self.init_acl = bind(self.advapi, "InitializeAcl", [pointer, w.DWORD, w.DWORD])
        self.add_ace = bind(self.advapi, "AddAce", [pointer, w.DWORD, w.DWORD, pointer, w.DWORD])
        self.init_sd = bind(self.advapi, "InitializeSecurityDescriptor", [pointer, w.DWORD])
        self.set_dacl = bind(self.advapi, "SetSecurityDescriptorDacl", [pointer, w.BOOL, pointer, w.BOOL])
        self.set_control = bind(self.advapi, "SetSecurityDescriptorControl", [pointer, w.WORD, w.WORD])
        self.set = bind(self.advapi, "SetKernelObjectSecurity", [w.HANDLE, w.DWORD, pointer])
        self.handle = create(rf"\\?\GLOBALROOT\Device\{self.name}", 0x60000, 7, None, 3, 0, None)
        assert self.handle != pointer(-1).value, ctypes.get_last_error()
        return self

    def __exit__(self, *_):
        assert self.close(self.handle), ctypes.get_last_error()

    def _read(self):
        w = self.w
        size = w.DWORD()
        self.get(self.handle, 0x17, None, 0, ctypes.byref(size))  # Owner/group/DACL/label.
        assert size.value, ctypes.get_last_error()
        descriptor = ctypes.create_string_buffer(size.value)
        assert self.get(self.handle, 0x17, descriptor, len(descriptor), ctypes.byref(size)), ctypes.get_last_error()
        present, defaulted, acl = w.BOOL(), w.BOOL(), ctypes.c_void_p()
        assert self.get_dacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted))
        assert present.value and acl.value, f"{self.name} must have a non-null DACL"
        control, revision = w.WORD(), w.DWORD()
        assert self.get_control(descriptor, ctypes.byref(control), ctypes.byref(revision))
        header = ctypes.string_at(acl, 8)
        entries = []
        for index in range(struct.unpack_from("<H", header, 4)[0]):
            entry = ctypes.c_void_p()
            assert self.get_ace(acl, index, ctypes.byref(entry)), ctypes.get_last_error()
            size = struct.unpack_from("<H", ctypes.string_at(entry, 4), 2)[0]
            entries.append(ctypes.string_at(entry, size))
        return descriptor, header[0], control.value, defaulted.value, tuple(entries)

    def state(self):
        descriptor, _, control, _, entries = self._read()
        text = self.w.LPWSTR()
        assert self.to_sddl(descriptor, 1, 0x13, ctypes.byref(text), None), ctypes.get_last_error()
        try:
            return text.value, control, entries
        finally:
            self.free(text)

    def sid(self, name):
        value = ctypes.c_void_p()
        assert self.to_sid(name, ctypes.byref(value)), ctypes.get_last_error()
        try:
            return ctypes.string_at(value, self.sid_length(value))
        finally:
            self.free(value)

    def leases(self, *names):
        sids = {self.sid(name) for name in names}
        return [entry for entry in self.state()[2] if entry[0] == 0 and entry[8:] in sids]

    def assert_lease(self, name, *, present=True):
        sid = self.sid(name)
        expected = struct.pack("<BBHI", 0, 0, 8 + len(sid), DEVICE_ACCESS[self.name]) + sid
        assert self.leases(name) == ([expected] if present else []), name

    def _marker(self, sid, *, remove=False):
        _, revision, control, defaulted, previous = self._read()
        entries = [entry for entry in previous if not (entry[0] == 0 and entry[8:] == sid)]
        marker = struct.pack("<BBHI", 0, 0, 8 + len(sid), 0x20000) + sid
        if not remove:
            entries.append(marker)
        acl = ctypes.create_string_buffer(8 + sum(map(len, entries)))
        assert self.init_acl(acl, len(acl), revision), ctypes.get_last_error()
        for entry in entries:
            raw = ctypes.create_string_buffer(entry)
            assert self.add_ace(acl, revision, 0xFFFFFFFF, raw, len(entry)), ctypes.get_last_error()
        descriptor = ctypes.create_string_buffer(64)
        assert self.init_sd(descriptor, 1)
        assert self.set_dacl(descriptor, True, acl, defaulted)
        assert self.set_control(descriptor, 0x1500, control & 0x1500)
        assert self.set(self.handle, 4 | (0x80000000 if control & 0x1000 else 0x20000000), descriptor), ctypes.get_last_error()
        return marker

    @contextmanager
    def marker(self):
        # No payload can change this ACL. The host changes it only while both
        # jobs are ready and no broker is in setup/cleanup; preserve all others.
        name = "S-1-5-21-" + "-".join(str(value) for value in struct.unpack("<4I", uuid.uuid4().bytes))
        sid = self.sid(name)
        entry = self._marker(sid)
        try:
            yield name, entry
        finally:
            self._marker(sid, remove=True)


def device_state():
    states = {}
    for name in DEVICE_ACCESS:
        with DeviceSecurity(name) as device:
            states[name] = device.state()
    return states


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


@contextmanager
def untrusted_device_mutex(name):
    """A precreated global object must cause refusal, never policy takeover."""
    from ctypes import wintypes as w

    assert name in DEVICE_ACCESS
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    class Attributes(ctypes.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]

    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    ]
    kernel.CreateMutexExW.argtypes = [ctypes.POINTER(Attributes), w.LPCWSTR, w.DWORD, w.DWORD]
    kernel.CreateMutexExW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.GetKernelObjectSecurity.argtypes = [
        w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD),
    ]

    def state(handle):
        size = w.DWORD()
        advapi.GetKernelObjectSecurity(handle, 7, None, 0, ctypes.byref(size))
        assert size.value, ctypes.get_last_error()
        buffer = ctypes.create_string_buffer(size.value)
        assert advapi.GetKernelObjectSecurity(handle, 7, buffer, len(buffer), ctypes.byref(size))
        return buffer.raw[:size.value]

    owner = process_sids(os.getpid(), 1)[0]
    descriptor = ctypes.c_void_p()
    text = f"O:{owner}G:{owner}D:P(A;;0x1f0001;;;SY)(A;;0x120001;;;BA)(A;;0x120001;;;WD)"
    assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(text, 1, ctypes.byref(descriptor), None)
    try:
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        ctypes.set_last_error(0)
        handle = kernel.CreateMutexExW(ctypes.byref(attributes), rf"Global\Virgo.NativeSandbox.{name}.v1", 0, 0x120001)
        error = ctypes.get_last_error()
        assert handle, error
        try:
            assert error != 183, "previous command leaked the global device mutex"
            before = state(handle)
            try:
                yield
            finally:
                assert state(handle) == before, "broker must not take over a precreated mutex"
        finally:
            assert kernel.CloseHandle(handle)
    finally:
        kernel.LocalFree(descriptor)


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


def test_private_host_files_denied_with_positive_workspace_control(native, project):
    outside = project.parent / "private-secret.txt"
    outside.write_text("private-host-canary")
    (project / "input.txt").write_text("workspace-readable")
    child_source = f"from pathlib import Path; Path({str(outside)!r}).write_text('child-escape')"
    source = f"""import json, subprocess, sys
from pathlib import Path
assert Path('input.txt').read_text() == 'workspace-readable'
Path('ordinary.txt').write_text('workspace-writable')
denied = []
for action in [lambda: Path({str(outside)!r}).read_text(), lambda: Path({str(outside)!r}).write_text('bad')]:
    try:
        action()
    except PermissionError:
        denied.append(True)
    else:
        denied.append(False)
child = subprocess.run([sys.executable, '-I', '-S', '-c', {child_source!r}], capture_output=True)
print(json.dumps({{'denied': denied, 'child_denied': child.returncode != 0}}))
"""
    assert data(run(native, source)) == {"denied": [True, True], "child_denied": True}
    assert (project / "ordinary.txt").read_text() == "workspace-writable"
    assert outside.read_text() == "private-host-canary"


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
    out, code, status = run_command(command(source), project, 30, threading.Event(),
                                    interpreter=selected(native), sandbox_policy=SandboxPolicy(mode, project))
    assert code == 0 and status == "ok", out.text
    assert payload_json(out.text) == {"arap_read": "arap-canary", "denied": [True, True, True], "acl_denied": True}
    assert aap.read_text() == "aap-canary" and arap.read_text() == "arap-canary"


def test_readonly_child_policy_is_enforced_by_windows(native, project):
    target = project / "readonly.txt"
    target.write_text("readable")
    api = ExtensionAPI(native, "windows-acceptance")
    child = api.create_runtime("readonly-child", "subagent", parent=native.main, permission="plan")
    try:
        ctx = ToolContext("readonly-check", project, threading.Event(), agent=child.name,
                          review_source=child.review_source)
        policy = native.services["sandbox"].for_context(ctx)
        assert policy.mode == "read-only" and policy.workspace == project.resolve()
        source = """import json, subprocess, sys
from pathlib import Path
assert Path('readonly.txt').read_text() == 'readable'
denied = []
for action in [lambda: Path('readonly.txt').write_text('bad'), lambda: Path('new.txt').write_text('bad'),
               lambda: Path('readonly.txt').unlink(), lambda: Path('readonly.txt').rename('renamed.txt')]:
    try:
        action()
    except PermissionError:
        denied.append(True)
    else:
        denied.append(False)
p = subprocess.run([sys.executable, '-I', '-S', '-c',
                    "from pathlib import Path; Path('child-write.txt').write_text('bad')"], capture_output=True)
print(json.dumps({'denied': denied, 'child_denied': p.returncode != 0}))
"""
        out, code, status = run_command(command(source), project, 30, threading.Event(),
                                        interpreter=selected(native), sandbox_policy=policy)
        assert code == 0 and status == "ok", out.text
        assert payload_json(out.text) == {"denied": [True] * 4, "child_denied": True}
        assert target.read_text() == "readable"
        assert not any((project / name).exists() for name in ("new.txt", "renamed.txt", "child-write.txt"))
    finally:
        api.release_runtime(child)


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
        assert data(run(native, source)) == {"files": [[True] * 4 for _ in paths],
                                             "directories": [[True] * 3, [True] * 3]}
        assert all(path.read_text() == "protected" for path in paths)
        assert (project / "ordinary-control.txt").read_text() == "allowed"


@pytest.mark.parametrize("kind", ["junction", "metadata-junction", "hardlink"])
def test_reparse_and_shared_file_grants_fail_before_payload(native, project, kind):
    outside = project.parent / "outside-links"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("outside")
    alias = project / (".git" if kind == "metadata-junction" else "alias")
    if kind == "hardlink":
        os.link(canary, alias)
    else:
        junction(alias, outside)
    before = dacl(canary)
    try:
        result = run(native, "from pathlib import Path; Path('payload-started').touch()")
        assert result.is_error and result.details["status"] == "sandbox_unavailable", result.text()
        assert not (project / "payload-started").exists()
        assert canary.read_text() == "outside" and dacl(canary) == before
    finally:
        if kind == "hardlink":
            alias.unlink()
        else:
            os.rmdir(alias)
    if kind != "junction":
        return
    # The payload may create a reparse point only if following it still retains
    # the target's OS access checks; the broker must not grant its target later.
    cmd = str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
    source = f"""import json, os, subprocess
from pathlib import Path
Path('positive-control.txt').write_text('allowed')
p = subprocess.run([{cmd!r}, '/d', '/c', 'mklink', '/J', 'live-junction', {str(outside)!r}], capture_output=True)
denied = []
if p.returncode == 0:
    for action in [lambda: Path('live-junction/canary.txt').read_text(),
                   lambda: Path('live-junction/canary.txt').write_text('bad')]:
        try:
            action()
        except PermissionError:
            denied.append(True)
        else:
            denied.append(False)
try:
    os.link({str(canary)!r}, 'live-hardlink')
except PermissionError:
    linked = False
else:
    linked = True
    try:
        Path('live-hardlink').write_text('bad')
    except PermissionError:
        denied.append(True)
    else:
        denied.append(False)
print(json.dumps({{'junction': p.returncode == 0, 'linked': linked, 'denied': denied}}))
"""
    outcome = data(run(native, source))
    assert len(outcome["denied"]) == 2 * outcome["junction"] + outcome["linked"]
    assert all(outcome["denied"])
    assert canary.read_text() == "outside" and (project / "positive-control.txt").read_text() == "allowed"


def test_credentials_and_unlisted_handles_are_not_inherited(native, project, monkeypatch):
    import msvcrt

    secret = "host-environment-and-handle-canary"
    keys = ["OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
            "HTTP_PROXY", "HTTPS_PROXY", "SSH_AUTH_SOCK", "VIRGO_HOME", "PYTHONPATH", "BASH_ENV", "ENV"]
    for key in keys:
        monkeypatch.setenv(key, secret)
    outside = project.parent / "handle-secret.txt"
    outside.write_text(secret)
    with outside.open("rb") as stream:
        os.set_inheritable(stream.fileno(), True)
        handle = msvcrt.get_osfhandle(stream.fileno())
        source = f"""import ctypes, json, os
from ctypes import wintypes as w
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
buffer = ctypes.create_string_buffer(128)
count = w.DWORD()
try:
    ok = k.ReadFile({handle}, buffer, 128, ctypes.byref(count), None)
except OSError as error:
    # Windows can raise STATUS_INVALID_HANDLE through ctypes instead of
    # returning FALSE when the deliberately unlisted handle is inaccessible.
    if (getattr(error, 'winerror', 0) & 0xffffffff) != 0xC0000008:
        raise
    ok = False
print(json.dumps({{'secrets': [os.environ.get(key) for key in {keys!r}],
                  'handle': buffer.raw[:count.value].decode(errors='replace') if ok else None,
                  'home': os.environ.get('USERPROFILE'), 'temp': os.environ.get('TEMP')}}))
"""
        outcome = data(run(native, source))
    assert outcome["secrets"] == [None] * len(keys)
    assert outcome["handle"] != secret
    assert outcome["home"] and outcome["temp"]
    assert not Path(outcome["temp"]).exists(), "private scratch must be cleaned after drain"


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
    outcome = data(run(native, source))
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
    assert data(run(native, source)) == [expected, expected]


def test_live_ipv4_ipv6_and_nonloopback_connections_are_denied(native):
    addresses = {item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET,
                                                          socket.SOCK_STREAM)}
    nonloopback = next((value for value in sorted(addresses) if not value.startswith("127.")), None)
    assert nonloopback, "strict network acceptance requires a real non-loopback IPv4 interface"
    endpoints = []
    with ExitStack() as resources:
        for family, address in [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1"),
                                (socket.AF_INET, nonloopback)]:
            listener = resources.enter_context(socket.socket(family, socket.SOCK_STREAM))
            listener.bind((address, 0))
            listener.listen(5)
            listener.settimeout(3)
            target = listener.getsockname()
            with socket.socket(family, socket.SOCK_STREAM) as control:
                control.settimeout(3)
                control.connect(target)
            accepted, _ = listener.accept()
            accepted.close()
            endpoints.append((int(family), target))
        udp = resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(3)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            control.sendto(b"host-positive-control", udp.getsockname())
        assert udp.recvfrom(128)[0] == b"host-positive-control"
        source = f"""import json, socket
results = []
for family, target in {endpoints!r}:
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect(target)
    except OSError as error:
        results.append(error.winerror)
    else:
        results.append(0)
try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b'sandbox-must-not-send', {udp.getsockname()!r})
except OSError as error:
    results.append(error.winerror)
else:
    results.append(0)
print(json.dumps(results))
"""
        assert data(run(native, source)) == [10013] * (len(endpoints) + 1), "requires WSAEACCES, not refusal/timeout"


def process_exited(pid: int) -> bool:
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        assert ctypes.get_last_error() == 87, ctypes.get_last_error()
        return True
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.parametrize("finish", ["exit", "timeout", "cancel"])
def test_completion_drains_descendants_before_scratch_cleanup(native, project, finish):
    before_device = device_state()
    child_source = """import time
from pathlib import Path
Path('child-ready').touch()
time.sleep(6)
Path('late-write').touch()
"""
    source = f"""import json, os, subprocess, sys, time
from pathlib import Path
p = subprocess.Popen([sys.executable, '-I', '-S', '-c', {child_source!r}], creationflags=0x200,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path('lifetime.json').write_text(json.dumps({{'pid': p.pid, 'scratch': os.environ['TEMP']}}))
deadline = time.monotonic() + 10
while not Path('child-ready').exists():
    assert time.monotonic() < deadline
    time.sleep(.02)
""" + ("time.sleep(25)\n" if finish != "exit" else "print('complete')\n")
    results, errors = [], []

    def execute():
        try:
            results.append(run(native, source, timeout=3 if finish == "timeout" else 30))
        except BaseException as error:
            errors.append(error)

    if finish == "cancel":
        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        try:
            wait_until(lambda: (project / "child-ready").exists() or not worker.is_alive(), timeout=25)
            assert (project / "child-ready").exists(), errors or [r.text() for r in results]
            lifetime = json.loads((project / "lifetime.json").read_text())
            restricting, = process_sids(lifetime["pid"], 11)
            package, = process_sids(lifetime["pid"], 31)
            for name in DEVICE_ACCESS:
                with DeviceSecurity(name) as device:
                    device.assert_lease(restricting)
                    device.assert_lease(package, present=name == "Null")
            native.abort()
            worker.join(15)
            assert not worker.is_alive()
        finally:
            native.abort()
            worker.join(15)
    else:
        execute()
    assert not errors and results, errors
    result = results[0]
    expected = {"exit": "ok", "timeout": "timeout", "cancel": "aborted"}[finish]
    assert result.details["status"] == expected, result.text()
    assert result.is_error == (finish != "exit"), result.text()
    lifetime = json.loads((project / "lifetime.json").read_text())
    assert process_exited(lifetime["pid"]), "descendant must have exited before command completion"
    assert not (project / "late-write").exists()
    assert not Path(lifetime["scratch"]).exists()
    assert device_state() == before_device, "device lease must be revoked after descendant drain"


def test_background_cancel_preserves_other_job_and_origin_after_switch(native, project):
    (project / "shared.txt").write_text("before")
    jobs = native.services["jobs"]
    active = []

    def stop_jobs():
        for job in active:
            if not job.done.is_set():
                jobs.kill(job)
                assert job.done.wait(15), "fixture must drain before removing its unrelated ACE"

    with ExitStack() as resources:
        devices = [resources.enter_context(DeviceSecurity(name)) for name in DEVICE_ACCESS]
        before_devices = [device.state() for device in devices]
        try:
            first = run(native, """import time
from pathlib import Path
Path('first-ready').touch()
time.sleep(25)
Path('first-late').touch()
""", background=True)
            assert not first.is_error, first.text()
            a = list(jobs.jobs.values())[-1]
            active.append(a)
            second = run(native, """import time
from pathlib import Path
Path('second-ready').touch()
deadline = time.monotonic() + 25
while not Path('release-second').exists():
    assert time.monotonic() < deadline
    time.sleep(.02)
Path('shared.txt').write_text('second-still-authorized')
""", background=True)
            assert not second.is_error, second.text()
            b = list(jobs.jobs.values())[-1]
            active.append(b)
            wait_until(lambda: (project / "first-ready").exists() and (project / "second-ready").exists(), timeout=25)
            a_sid, = process_sids(a.proc.pid, 11)
            b_sid, = process_sids(b.proc.pid, 11)
            a_package, = process_sids(a.proc.pid, 31)
            b_package, = process_sids(b.proc.pid, 31)
            assert a_sid != b_sid
            assert a_package != b_package
            for device in devices:
                device.assert_lease(a_sid)
                device.assert_lease(b_sid)
                device.assert_lease(a_package, present=device.name == "Null")
                device.assert_lease(b_package, present=device.name == "Null")
            with ExitStack() as markers:
                marker_entries = [markers.enter_context(device.marker()) for device in devices]
                try:
                    other = project.parent / "selected-workspace"
                    other.mkdir()
                    selected_store = SessionStore.new(native.paths.sessions, other)
                    selected_store.append_message(UserMessage("Selected workspace conversation"))
                    native.open_session(selected_store.path)
                    jobs.kill(a)
                    assert a.done.wait(15) and a.status == "killed"
                    assert b.status == "running" and b.proc.poll() is None
                    for device, before, (marker_sid, entry) in zip(devices, before_devices, marker_entries, strict=True):
                        device.assert_lease(a_sid, present=False)
                        device.assert_lease(a_package, present=False)
                        device.assert_lease(b_sid)
                        device.assert_lease(b_package, present=device.name == "Null")
                        assert device.leases(marker_sid) == [entry]
                        assert device.state() == (*before[:2], before[2] + tuple(device.leases(b_sid, b_package)) + (entry,))
                    (project / "release-second").touch()
                    assert b.done.wait(15) and b.status == "completed", jobs.output(b)
                    assert a.sandbox["backend"] == b.sandbox["backend"] == "windows-lpac"
                    assert a.sandbox["workspace"] == b.sandbox["workspace"] == str(project.resolve())
                    assert a.log.parent == b.log.parent == project / ".virgo" / "outputs"
                    assert (project / "shared.txt").read_text() == "second-still-authorized"
                    assert not (project / "first-late").exists() and not list(other.iterdir())
                    for device, before, (_, entry) in zip(devices, before_devices, marker_entries, strict=True):
                        assert device.state() == (*before[:2], before[2] + (entry,))
                finally:
                    stop_jobs()
            assert [device.state() for device in devices] == before_devices
        finally:
            stop_jobs()


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_core_mutation_preserves_live_background_workspace_writes(native, project, tool):
    target = project / "ordinary.txt"
    target.write_text("original\n")
    baseline_dacl = dacl(target)
    created = project / "created-by-core.txt"
    jobs = native.services["jobs"]
    result = run(native, f"""import time
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
""", background=True)
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
        assert committed_dacl == original_dacl
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


def test_core_create_during_command_preflight_preserves_workspace_writes(
    native, project, monkeypatch, record_testsuite_property,
):
    from virgo.tools import windows_sandbox_acl as acl
    from virgo.tools import windows_sandbox_files as files

    target = project / "created-during-startup.txt"
    staged, retained = threading.Event(), threading.Event()
    release_core, release_grant = threading.Event(), threading.Event()
    attempting_commit, core_done, shell_done = threading.Event(), threading.Event(), threading.Event()
    cancellation = threading.Event()
    observed, results, errors = {}, {}, {}
    replace, update = files.replace_staged, acl.AclGrants._update

    @contextmanager
    def pause_commit(source, destination, **arguments):
        if destination == target:
            with files.open_path(source, files.FILE_READ_ATTRIBUTES, directory=False) as handle:
                observed["identity"] = handle.identity
            observed["staged_path"] = str(source)
            staged.set()
            assert release_core.wait(30), "fixture did not release the staged Core commit"
            attempting_commit.set()
        with replace(source, destination, **arguments):
            yield

    def pause_grant(self, grant, *, cleanup=False):
        if not cleanup and grant.handle.identity == observed.get("identity") and not retained.is_set():
            observed["grant_path"] = str(grant.path)
            observed["grant_writable"] = grant.writable
            retained.set()
            assert release_grant.wait(15), "fixture did not release the retained staging grant"
        return update(self, grant, cleanup=cleanup)

    monkeypatch.setattr(files, "replace_staged", pause_commit)
    monkeypatch.setattr(acl.AclGrants, "_update", pause_grant)
    faux.script("test", faux.reply(tools=[("write", {"path": target.name, "content": "created\n"})]),
                faux.reply("done"))

    def core_write():
        try:
            native.prompt("Create an ordinary file while another command starts")
            results["core"] = tool_results(native)[-1]
        except BaseException as error:
            errors["core"] = repr(error)
        finally:
            core_done.set()

    def start_shell():
        try:
            results["shell"] = run_command(command("""import os, time
from pathlib import Path
Path('startup-pid').write_text(str(os.getpid()))
Path('startup-ready').touch()
deadline = time.monotonic() + 30
while not Path('release-startup-payload').exists():
    assert time.monotonic() < deadline
    time.sleep(.02)
assert Path('created-during-startup.txt').read_text() == 'created\\n'
with Path('created-during-startup.txt').open('a') as stream:
    stream.write('after\\n')
"""), project, 30, cancellation, interpreter=selected(native),
                sandbox_policy=SandboxPolicy("workspace-write", project))
        except BaseException as error:
            errors["shell"] = repr(error)
        finally:
            shell_done.set()

    core_thread = threading.Thread(target=core_write, daemon=True)
    shell_thread = threading.Thread(target=start_shell, daemon=True)
    try:
        core_thread.start()
        wait_until(lambda: staged.is_set() or core_done.is_set(), timeout=15)
        assert staged.is_set(), f"Core did not reach real staging: {errors}, {results}"
        assert not target.exists(), "the fixture must exercise creation of a new ordinary file"
        shell_thread.start()
        wait_until(lambda: retained.is_set() or shell_done.is_set(), timeout=25)
        assert retained.is_set(), f"Command preflight did not retain the staging inode: {errors}, {results}"
        assert Path(observed["grant_path"]) == Path(observed["staged_path"]), observed
        assert not observed["grant_writable"], observed
        release_core.set()
        assert attempting_commit.wait(5), "Core did not attempt its native commit"
        # A fixed implementation may serialize the entire workspace preflight.
        # Release its grant even when Core is waiting for that lock; do not make
        # the vulnerable interleaving a requirement for the corrected behavior.
        observed["core_completed_before_grant"] = core_done.wait(2)
        record_testsuite_property("core_completed_before_staging_grant", observed["core_completed_before_grant"])
        if core_done.is_set() and target.exists():
            observed["dacl_before_grant"] = dacl(target)
        release_grant.set()
        assert core_done.wait(15), f"Core mutation did not finish: {errors}"
        assert "core" not in errors, errors
        assert not results["core"].is_error, results["core"].text()
        assert target.read_text() == "created\n"
        ready = project / "startup-ready"
        wait_until(lambda: ready.exists() or shell_done.is_set(), timeout=25)
        assert ready.exists(), f"Native payload did not start: {errors}, {results}"
        pid = int((project / "startup-pid").read_text())
        restricting, = process_sids(pid, 11)
        package, = process_sids(pid, 31)
        observed["live_dacl"] = dacl(target)
        (project / "release-startup-payload").touch()
        assert shell_done.wait(20), f"Native payload did not drain: {errors}"
        assert "shell" not in errors, errors
        out, code, status = results["shell"]
        assert code == 0 and status == "ok", f"Ordering and DACL evidence:\n{observed}\nPayload output:\n{out.text}"
        assert target.read_text() == "created\nafter\n"
        assert not list((project / ".virgo").glob("mutation-*.tmp"))
        for path in (project, project / ".virgo", target):
            cleaned = dacl(path)
            assert restricting not in cleaned and package not in cleaned, cleaned
    finally:
        release_core.set()
        release_grant.set()
        cancellation.set()
        for thread in (core_thread, shell_thread):
            if thread.ident is not None:
                thread.join(25)
        assert not core_thread.is_alive() and not shell_thread.is_alive(), "fixture must drain both workers"


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
        faux.script("test", faux.reply(tools=[(selected(native).tool_name, {"command": command(source)})]),
                    faux.reply("done"))
        child.agent.prompt("Exercise the original runtime workspace")
        result = next(m for m in reversed(child.messages) if isinstance(m, ToolResultMessage))
        assert not result.is_error and "outside-denied" in result.text(), result.text()
        assert result.details["sandbox"]["workspace"] == str(project.resolve())
        assert (nested / "child-success.txt").read_text() == "origin" and secret.read_text() == "outside"
        faux.script("test", faux.reply(tools=[(selected(native).tool_name, {
            "command": command("from pathlib import Path; Path('escaped.txt').touch()"),
        })]), faux.reply("done"))
        escaped.agent.prompt("Attempt to change the sandbox root using cwd")
        result = next(m for m in reversed(escaped.messages) if isinstance(m, ToolResultMessage))
        assert result.is_error and result.details["status"] == "sandbox_unavailable", result.text()
        assert not (outside / "escaped.txt").exists() and not (outside / ".virgo").exists()
    finally:
        api.release_runtime(child)
        api.release_runtime(escaped)


def test_actual_start_failure_is_fail_closed_and_nonzero_payload_is_not_replayed(native, project):
    policy = SandboxPolicy("workspace-write", project)
    before = set((project / ".virgo" / "outputs").iterdir())
    before_device = device_state()
    missing = Shell("pwsh", str(project / "missing-interpreter.exe"), "unknown")
    with pytest.raises(SandboxUnavailable):
        run_command("Set-Content payload-started bad", project, 10, threading.Event(),
                    interpreter=missing, sandbox_policy=policy)
    assert not (project / "payload-started").exists()
    assert set((project / ".virgo" / "outputs").iterdir()) == before
    # This file passes name/path validation and fails real CreateProcess only
    # after the broker has acquired its leases. No patched API supplies failure.
    invalid = project.parent / "invalid-runtime" / "pwsh.exe"
    invalid.parent.mkdir()
    invalid.write_bytes(b"not a Windows executable image")
    with pytest.raises(SandboxUnavailable) as failed:
        run_command("Set-Content payload-started bad", project, 10, threading.Event(),
                    interpreter=Shell("pwsh", str(invalid), "unknown"), sandbox_policy=policy)
    cause = failed.value
    while cause.__cause__ is not None:
        cause = cause.__cause__
    # Both native image-loader errors describe this real invalid executable;
    # other setup failures cannot stand in for reaching CreateProcess.
    assert getattr(cause, "winerror", None) in {193, 216}, failed.value
    assert device_state() == before_device
    assert not (project / "payload-started").exists()
    assert set((project / ".virgo" / "outputs").iterdir()) == before
    for name in DEVICE_ACCESS:
        with untrusted_device_mutex(name), pytest.raises(SandboxUnavailable):
            run_command("Set-Content payload-started bad", project, 10, threading.Event(),
                        interpreter=selected(native), sandbox_policy=policy)
        assert device_state() == before_device
        assert not (project / "payload-started").exists()
        assert set((project / ".virgo" / "outputs").iterdir()) == before
    # The existing PowerShell wrapper turns an unhandled native failure into
    # exit 1. An explicit shell exit preserves its code, so keep the successful
    # single side effect followed by the precise nonzero lifecycle assertion.
    source = "from pathlib import Path; p=Path('once.txt'); p.open('a').write('once')"
    faux.script("test", faux.reply(tools=[(selected(native).tool_name, {
        "command": command(source) + "\nexit 7",
    })]), faux.reply("done"))
    native.prompt("Run one side effect followed by an explicit nonzero shell exit")
    result = tool_results(native)[-1]
    assert result.is_error and result.details["status"] == "ok" and result.details["exit_code"] == 7, result.text()
    assert (project / "once.txt").read_text() == "once"
    assert device_state() == before_device


def test_repeated_commands_release_owned_acls_scratch_and_handles(native, project):
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.GetProcessHandleCount.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]

    def count():
        value = w.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value))
        return value.value

    stable = project / "stable.txt"
    stable.write_text("original")
    before = {path: dacl(path) for path in (project, stable)}
    before_device = device_state()
    handles = count()
    identities = set()
    for index in range(4):
        source = TOKEN_SOURCE + f"""
from pathlib import Path
Path('new-{index}.txt').write_text('ordinary')
token_result['scratch'] = os.environ['TEMP']
print(json.dumps(token_result))
"""
        result = data(run(native, source))
        assert result["sid"] not in identities, "each command must use an independent identity"
        identities.add(result["sid"])
        assert not Path(result["scratch"]).exists()
        assert result["sid"] not in dacl(project / f"new-{index}.txt")
        for path, original in before.items():
            assert dacl(path) == original, f"temporary ACL grant remains on {path}"
        assert device_state() == before_device, "repeated command left a device lease"
    gc.collect()
    assert count() - handles < 12, "repeated native launches leaked Windows handles"
