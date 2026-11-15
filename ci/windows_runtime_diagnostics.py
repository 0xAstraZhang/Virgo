"""Observe Windows startup denials; these launches are not security acceptance.

The payload is always print('native-ready'). All production token, desktop and
Job checks remain in force. Security descriptors are read or copied in memory;
this script never changes a machine device or ETW provider ACL.
"""
from __future__ import annotations

import base64
import ctypes
import json
import shutil
import sys
import tempfile
import threading
import uuid
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
EventAccessQuery = native._api(native.advapi, "EventAccessQuery", [
    POINTER, POINTER, ctypes.POINTER(native.DWORD),
], native.DWORD)
CreateFile = native._api(native.kernel, "CreateFileW", [
    ctypes.c_wchar_p, native.DWORD, native.DWORD, POINTER, native.DWORD, native.DWORD, native.HANDLE,
], native.HANDLE)


def emit(event, **fields):
    print(json.dumps({"diagnostic_only": True, "event": event, **fields}), flush=True)


def sddl(descriptor):
    text = ctypes.c_wchar_p()
    native._check(ToSddl(descriptor, 1, 7, ctypes.byref(text), None))
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

        for desired in (0x20000, 0x60000):  # READ_CONTROL; additionally WRITE_DAC authority.
            handle = CreateFile(r"\\?\GLOBALROOT\Device\KsecDD", desired, 7, None, 3, 0, None)
            if handle == POINTER(-1).value:
                emit("device_authority", access=f"0x{desired:x}", winerror=ctypes.get_last_error())
                continue
            try:
                emit("device_authority", access=f"0x{desired:x}", winerror=0)
                size = native.DWORD()
                GetSecurity(handle, 7, None, 0, ctypes.byref(size))
                if not size.value:
                    raise ctypes.WinError(ctypes.get_last_error())
                buffer = ctypes.create_string_buffer(size.value)
                native._check(GetSecurity(handle, 7, buffer, len(buffer), ctypes.byref(size)))
                text = sddl(buffer)
                emit("device_descriptor", object=r"\Device\KsecDD", sddl=text)
                for name, sids in (("current", ()), ("write_sid", (envelope.write_name,)),
                                   ("package_sid", (envelope.package_name,)),
                                   ("both_sids", (envelope.write_name, envelope.package_name))):
                    candidate = text + "".join(f"(A;;0x100003;;;{sid})" for sid in sids)
                    emit("device_access_copy", name=name, **access(impersonation, candidate, 0x100003))
            except Exception as error:
                emit("device_observation_error", error=str(error))
            finally:
                native.CloseHandle(handle)

        for name in ("c7e089ac-ba2a-11e0-9af7-68384824019b", "f90714a8-5509-434a-bf6d-b1624c8a19a2"):
            guid = ctypes.create_string_buffer(uuid.UUID(name).bytes_le)
            size = native.DWORD()
            result = EventAccessQuery(guid, None, ctypes.byref(size))
            if not size.value:
                emit("provider_descriptor", guid=name, status=result)
                continue
            buffer = ctypes.create_string_buffer(size.value)
            result = EventAccessQuery(guid, buffer, ctypes.byref(size))
            emit("provider_descriptor", guid=name, status=result, sddl=sddl(buffer) if result == 0 else None)
    finally:
        if impersonation:
            native.CloseHandle(impersonation)
        native.CloseHandle(token)


def main():
    original = native.WindowsSandbox
    interpreter = probe_shell(shutil.which("pwsh") or "pwsh", execute=False)
    source = base64.b64encode(b"print('native-ready')").decode()
    arguments = [sys.executable, "-I", "-S", "-c", f"import base64;exec(base64.b64decode('{source}'))"]
    command = "& " + " ".join("'" + value.replace("'", "''") + "'" for value in arguments)
    for capabilities in (("registryRead",), ("registryRead", "lpacCryptoServices", "lpacInstrumentation")):
        class ObservedSandbox(original):
            capability_names = capabilities

            def process_attributes(self):
                self._capability_buffers = [native._capability(name) for name in self.capability_names]
                self._capabilities = (native.SidAndAttributes * len(self._capability_buffers))(
                    *(native.SidAndAttributes(ctypes.cast(value, native.PSID), 0x4)
                      for value in self._capability_buffers)
                )
                return super().process_attributes()

            def verify_process(self, process):
                super().verify_process(process)
                try:
                    observe(self, process, self.capability_names)
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
