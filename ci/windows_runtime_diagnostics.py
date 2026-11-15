"""Observe Windows runtime denials; this launch is not security acceptance.

The fixed payload queries executable paths and opens the null device without
transferring data. All production token, capability, desktop and Job checks
remain in force. Observations read descriptors or copy them in memory; only the
normal production sandbox manages its existing temporary resource leases.
"""
from __future__ import annotations

import base64
import ctypes
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from virgo.tools import windows_sandbox as native
from virgo.tools.bash import run_command
from virgo.tools.sandbox import SandboxPolicy
from virgo.tools.shell import probe_shell

POINTER = ctypes.c_void_p
GetSecurity = native._api(native.advapi, "GetKernelObjectSecurity", [
    native.HANDLE, native.DWORD, POINTER, native.DWORD, ctypes.POINTER(native.DWORD),
])
ToSddl = native._api(native.advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW", [
    POINTER, native.DWORD, native.DWORD, ctypes.POINTER(ctypes.c_wchar_p), POINTER,
])
CreateFile = native._api(native.kernel, "CreateFileW", [
    ctypes.c_wchar_p, native.DWORD, native.DWORD, POINTER, native.DWORD, native.DWORD, native.HANDLE,
], native.HANDLE)


def emit(event, **fields):
    print(json.dumps({"diagnostic_only": True, "event": event, **fields}), flush=True)


def sddl(descriptor, information=7):
    text = ctypes.c_wchar_p()
    native._check(ToSddl(descriptor, 1, information, ctypes.byref(text), None))
    try:
        return text.value
    finally:
        native.LocalFree(text)


def access(token, text, mask):
    descriptor = POINTER()
    native._check(native.ConvertSecurityDescriptor(text, 1, ctypes.byref(descriptor), None))
    try:
        mapping = native.GenericMapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
        privileges = ctypes.create_string_buffer(4096)
        size, granted, permitted = native.DWORD(len(privileges)), native.DWORD(), native.BOOL()
        native._check(native.AccessCheck(descriptor, token, mask, ctypes.byref(mapping), privileges,
                                         ctypes.byref(size), ctypes.byref(granted), ctypes.byref(permitted)))
        return {"allowed": bool(permitted.value), "granted": f"0x{granted.value:08x}"}
    finally:
        native.LocalFree(descriptor)


def observe(envelope, process, capabilities):
    emit("verified_token", capabilities=capabilities, restricting_sid=envelope.write_name)
    token, impersonation = native.HANDLE(), native.HANDLE()
    native._check(native.OpenProcessToken(process, 0xA, ctypes.byref(token)))
    try:
        native._check(native.DuplicateToken(token, 2, ctypes.byref(impersonation)))
        descriptor = "O:SYG:SYD:(A;;0x3;;;WD)(A;;0x3;;;S-1-15-2-2)"
        for name, value, mask in (
            ("ambient_read", descriptor, 1),
            ("ambient_write", descriptor, 2),
            ("explicit_write", descriptor + f"(A;;0x3;;;{envelope.write_name})", 2),
        ):
            emit("file_access_semantics", name=name, **access(impersonation, value, mask))

        for device, required in (("KsecDD", 0x100003), ("Null", 0x12019F)):
            for desired in (0x20000, 0x60000):  # READ_CONTROL; additionally WRITE_DAC authority.
                handle = CreateFile(rf"\\?\GLOBALROOT\Device\{device}", desired, 7, None, 3, 0, None)
                if handle == POINTER(-1).value:
                    emit("device_authority", device=device, access=f"0x{desired:x}", winerror=ctypes.get_last_error())
                    continue
                try:
                    emit("device_authority", device=device, access=f"0x{desired:x}", winerror=0)
                    size = native.DWORD()
                    GetSecurity(handle, 0x17, None, 0, ctypes.byref(size))
                    if not size.value:
                        raise ctypes.WinError(ctypes.get_last_error())
                    buffer = ctypes.create_string_buffer(size.value)
                    native._check(GetSecurity(handle, 0x17, buffer, len(buffer), ctypes.byref(size)))
                    text = sddl(buffer, 0x17)
                    emit("device_descriptor", device=device, sddl=text)
                    dacl, separator, label = text.partition("S:")
                    for name, sids, grant in (
                        ("current", (), required),
                        ("write_sid", (envelope.write_name,), required),
                        ("package_sid", (envelope.package_name,), required),
                        ("both_sids", (envelope.write_name, envelope.package_name), required),
                        ("data_only_write_sid", (envelope.write_name,), 0x100003),
                    ):
                        candidate = dacl + "".join(f"(A;;0x{grant:x};;;{sid})" for sid in sids) + separator + label
                        emit("device_access_copy", device=device, name=name, desired=f"0x{required:x}",
                             **access(impersonation, candidate, required))
                except Exception as error:
                    emit("device_observation_error", device=device, error=str(error))
                finally:
                    native.CloseHandle(handle)
    finally:
        if impersonation:
            native.CloseHandle(impersonation)
        native.CloseHandle(token)


PAYLOAD = r'''
import ctypes, json, os, sys
from ctypes import wintypes as w
k = ctypes.WinDLL('kernel32', use_last_error=True)
n = ctypes.WinDLL('ntdll')
k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
k.CreateFileW.restype = w.HANDLE
k.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
k.GetFinalPathNameByHandleW.restype = w.DWORD
k.CloseHandle.argtypes = [w.HANDLE]
def emit(event, **fields):
    print(json.dumps({'diagnostic_only': True, 'event': event, **fields}), flush=True)
for executable in dict.fromkeys((sys.executable, sys._base_executable)):
    ctypes.set_last_error(0)
    handle = k.CreateFileW(executable, 0, 0, None, 3, 0x02000000, None)
    error = ctypes.get_last_error()
    opened = handle != ctypes.c_void_p(-1).value
    emit('cpython_realpath_open', executable=executable, desired=0, share=0,
         flags='0x02000000', opened=opened, winerror=error)
    if opened:
        try:
            for flags in (0, 2, 8):
                path = ctypes.create_unicode_buffer(32768)
                ctypes.set_last_error(0)
                count = k.GetFinalPathNameByHandleW(handle, path, len(path), flags)
                error = ctypes.get_last_error()
                emit('cpython_finalpath', flags=flags, count=count, winerror=error,
                     path=path.value if 0 < count < len(path) else None)
        finally:
            k.CloseHandle(handle)
try:
    descriptor = os.open(os.devnull, os.O_RDWR)
except OSError as error:
    emit('python_devnull', errno=error.errno, winerror=error.winerror, error=str(error))
else:
    os.close(descriptor)
    emit('python_devnull', errno=0, winerror=0)
ctypes.set_last_error(0)
handle = k.CreateFileW('NUL', 0xc0000000, 3, None, 3, 0x80, None)
error = ctypes.get_last_error()
opened = handle != ctypes.c_void_p(-1).value
emit('win32_devnull', desired='GENERIC_READ|GENERIC_WRITE', opened=opened, winerror=error)
if opened:
    k.CloseHandle(handle)
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
path = r'\Device\Null'
name = Name(len(path.encode('utf-16-le')), len(path.encode('utf-16-le')) + 2, path)
attributes = Attributes(ctypes.sizeof(Attributes), None, ctypes.pointer(name), 0x40, None, None)
for access in (0x120089, 0x12019f, 0x100003):
    handle, io = w.HANDLE(), IoStatus()
    status = n.NtOpenFile(ctypes.byref(handle), access, ctypes.byref(attributes), ctypes.byref(io), 7, 0)
    emit('native_devnull', desired=hex(access), status=hex(status & 0xffffffff))
    if status == 0:
        k.CloseHandle(handle)
'''


def main():
    original = native.WindowsSandbox
    interpreter = probe_shell(shutil.which("pwsh") or "pwsh", execute=False)
    source = base64.b64encode(PAYLOAD.encode()).decode()
    arguments = [sys.executable, "-I", "-S", "-c", f"import base64;exec(base64.b64decode('{source}'))"]
    command = "& " + " ".join("'" + value.replace("'", "''") + "'" for value in arguments)
    capabilities = ("registryRead", "lpacInstrumentation")

    class ObservedSandbox(original):
        def verify_process(self, process):
            super().verify_process(process)
            try:
                observe(self, process, capabilities)
            except Exception as error:
                emit("observation_error", error=str(error))

    native.WindowsSandbox = ObservedSandbox
    try:
        with tempfile.TemporaryDirectory(prefix="virgo-native-diagnostic-") as directory:
            root = Path(directory)
            output, code, status = run_command(command, root, 30, threading.Event(), interpreter=interpreter,
                                              sandbox_policy=SandboxPolicy("workspace-write", root))
            emit("fixed_payload_result", capabilities=capabilities, exit_code=code,
                 status=status, output=output.text)
    except Exception as error:
        emit("fixed_payload_error", capabilities=capabilities, error=str(error))
    finally:
        native.WindowsSandbox = original


if __name__ == "__main__":
    main()
