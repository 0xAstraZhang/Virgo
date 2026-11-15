"""Windows LPAC and a per-command write-restricted token.

The lowbox enforces network/process isolation and object-ACL read access. Reads
also include Windows' ambient LPAC grants; this is not a filesystem namespace.
A separate, unique restricting SID is the only authority in the additional
write check. Neither approval nor an environment variable replaces these APIs.
Only the existing WindowsProcess adapter launches and supervises processes.
"""
from __future__ import annotations

import ctypes
import struct
import sys
import uuid
from contextlib import ExitStack
from ctypes import wintypes as w
from pathlib import Path

from .sandbox import SandboxCancelled, SandboxProcessError, SandboxUnavailable
from .windows_sandbox_files import normalize_path, open_path

HANDLE = ctypes.c_void_p
PSID = ctypes.c_void_p
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
kernel = ctypes.WinDLL("kernel32", use_last_error=True)


def _api(library, name, args, result=BOOL):
    function = getattr(library, name)
    function.argtypes, function.restype = args, result
    return function


def _check(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


GetSystemDirectory = _api(kernel, "GetSystemDirectoryW", [w.LPWSTR, DWORD], DWORD)
GetWindowsDirectory = _api(kernel, "GetWindowsDirectoryW", [w.LPWSTR, DWORD], DWORD)
CloseHandle = _api(kernel, "CloseHandle", [HANDLE])
LocalFree = _api(kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
GetCurrentProcess = _api(kernel, "GetCurrentProcess", [], HANDLE)
IsWow64Process = _api(kernel, "IsWow64Process2", [HANDLE, ctypes.POINTER(w.WORD), ctypes.POINTER(w.WORD)])


def _directory(api):
    output = ctypes.create_unicode_buffer(32768)
    count = _check(api(output, len(output)))
    if count >= len(output):
        raise SandboxUnavailable("Windows system directory exceeds the native path limit")
    return Path(normalize_path(output.value))


SYSTEM_DIRECTORY = _directory(GetSystemDirectory)
advapi = ctypes.WinDLL(str(SYSTEM_DIRECTORY / "advapi32.dll"), use_last_error=True)
userenv = ctypes.WinDLL(str(SYSTEM_DIRECTORY / "userenv.dll"), use_last_error=True)
base = ctypes.WinDLL(str(SYSTEM_DIRECTORY / "kernelbase.dll"), use_last_error=True)
ole = ctypes.WinDLL(str(SYSTEM_DIRECTORY / "ole32.dll"), use_last_error=True)


class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", PSID), ("Attributes", DWORD)]


class TokenGroups(ctypes.Structure):
    _fields_ = [("Count", DWORD), ("Groups", SidAndAttributes * 1)]


class SecurityCapabilities(ctypes.Structure):
    _fields_ = [("AppContainerSid", PSID), ("Capabilities", ctypes.POINTER(SidAndAttributes)),
                ("CapabilityCount", DWORD), ("Reserved", DWORD)]


class ServiceStatus(ctypes.Structure):
    _fields_ = [(name, DWORD) for name in (
        "ServiceType", "CurrentState", "ControlsAccepted", "Win32ExitCode",
        "ServiceSpecificExitCode", "CheckPoint", "WaitHint", "ProcessId", "ServiceFlags",
    )]


OpenProcessToken = _api(advapi, "OpenProcessToken", [HANDLE, DWORD, ctypes.POINTER(HANDLE)])
GetTokenInformation = _api(advapi, "GetTokenInformation", [HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                         DWORD, ctypes.POINTER(DWORD)])
SetTokenInformation = _api(advapi, "SetTokenInformation", [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD])
CreateRestrictedToken = _api(advapi, "CreateRestrictedToken", [
    HANDLE, DWORD, DWORD, ctypes.c_void_p, DWORD, ctypes.c_void_p,
    DWORD, ctypes.POINTER(SidAndAttributes), ctypes.POINTER(HANDLE),
])
ConvertStringSid = _api(advapi, "ConvertStringSidToSidW", [w.LPCWSTR, ctypes.POINTER(PSID)])
ConvertSidToString = _api(advapi, "ConvertSidToStringSidW", [PSID, ctypes.POINTER(w.LPWSTR)])
GetLengthSid = _api(advapi, "GetLengthSid", [PSID], DWORD)
FreeSid = _api(advapi, "FreeSid", [PSID], ctypes.c_void_p)
ConvertSecurityDescriptor = _api(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [
    w.LPCWSTR, DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
])
GetSecurityDescriptorDacl = _api(advapi, "GetSecurityDescriptorDacl", [
    ctypes.c_void_p, ctypes.POINTER(BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(BOOL),
])
CreateProfile = _api(userenv, "CreateAppContainerProfile", [
    w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(SidAndAttributes), DWORD, ctypes.POINTER(PSID),
], ctypes.c_int32)
DeleteProfile = _api(userenv, "DeleteAppContainerProfile", [w.LPCWSTR], ctypes.c_int32)
GetProfilePath = _api(userenv, "GetAppContainerFolderPath", [w.LPCWSTR, ctypes.POINTER(w.LPWSTR)], ctypes.c_int32)
CoTaskMemFree = _api(ole, "CoTaskMemFree", [ctypes.c_void_p], None)
DeriveCapability = _api(base, "DeriveCapabilitySidsFromName", [
    w.LPCWSTR, ctypes.POINTER(ctypes.POINTER(PSID)), ctypes.POINTER(DWORD),
    ctypes.POINTER(ctypes.POINTER(PSID)), ctypes.POINTER(DWORD),
])
OpenSCManager = _api(advapi, "OpenSCManagerW", [w.LPCWSTR, w.LPCWSTR, DWORD], HANDLE)
OpenService = _api(advapi, "OpenServiceW", [HANDLE, w.LPCWSTR, DWORD], HANDLE)
QueryServiceStatus = _api(advapi, "QueryServiceStatusEx", [HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                       DWORD, ctypes.POINTER(DWORD)])
CloseServiceHandle = _api(advapi, "CloseServiceHandle", [HANDLE])


def _hresult(value, operation):
    if value < 0:
        raise SandboxUnavailable(f"Windows {operation} failed (HRESULT 0x{value & 0xffffffff:08X})")


def _sid_bytes(sid):
    size = _check(GetLengthSid(sid))
    return ctypes.string_at(sid, size)


def _sid_string(sid):
    output = w.LPWSTR()
    _check(ConvertSidToString(sid, ctypes.byref(output)))
    try:
        return output.value
    finally:
        LocalFree(output)


def _sid_from_string(text):
    output = PSID()
    _check(ConvertStringSid(text, ctypes.byref(output)))
    try:
        return ctypes.create_string_buffer(_sid_bytes(output))
    finally:
        LocalFree(output)


def _token_info(token, kind):
    size = DWORD()
    GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
    if not size.value or size.value > 1 << 20:
        raise SandboxUnavailable(f"Could not query Windows token information {kind}")
    output = ctypes.create_string_buffer(size.value)
    _check(GetTokenInformation(token, kind, output, len(output), ctypes.byref(size)))
    if size.value > len(output):
        raise SandboxUnavailable("Windows returned inconsistent token information")
    return output


def _token_sids(token, kind):
    output = _token_info(token, kind)
    count = DWORD.from_buffer(output).value
    offset = TokenGroups.Groups.offset
    if offset + count * ctypes.sizeof(SidAndAttributes) > len(output):
        raise SandboxUnavailable("Windows returned an invalid token SID list")
    values = (SidAndAttributes * count).from_buffer(output, offset)
    return {_sid_bytes(value.Sid) for value in values}


def _capability(name):
    groups, capabilities = ctypes.POINTER(PSID)(), ctypes.POINTER(PSID)()
    group_count, capability_count = DWORD(), DWORD()
    _check(DeriveCapability(name, ctypes.byref(groups), ctypes.byref(group_count),
                            ctypes.byref(capabilities), ctypes.byref(capability_count)))
    try:
        if capability_count.value != 1:
            raise SandboxUnavailable(f"Windows did not resolve the required {name} capability")
        return ctypes.create_string_buffer(_sid_bytes(capabilities[0]))
    finally:
        for pointers, count in ((groups, group_count.value), (capabilities, capability_count.value)):
            if pointers:
                for index in range(count):
                    LocalFree(pointers[index])
                LocalFree(pointers)


def backend():
    """Discovery only; each actual process must pass suspended-token checks."""
    if sys.getwindowsversion().major < 10:
        raise SandboxUnavailable("The Windows sandbox requires Windows 10-family native APIs")
    process_machine, native_machine = w.WORD(), w.WORD()
    _check(IsWow64Process(GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native_machine)))
    if ctypes.sizeof(HANDLE) != 8 or process_machine.value != 0 or native_machine.value != 0x8664:
        raise SandboxUnavailable("The Windows sandbox currently supports native x64 Python only")
    return "windows-lpac", SYSTEM_DIRECTORY / "kernelbase.dll"


def _require_network_isolation():
    # AppContainer network isolation depends on Windows filtering services, not
    # proxy settings. Never start a payload on a host without these services.
    with ExitStack() as resources:
        manager = _check(OpenSCManager(None, None, 0x1))
        resources.callback(CloseServiceHandle, manager)
        for name in ("BFE", "MpsSvc"):
            service = _check(OpenService(manager, name, 0x4))
            resources.callback(CloseServiceHandle, service)
            status, needed = ServiceStatus(), DWORD()
            _check(QueryServiceStatus(service, 0, ctypes.byref(status), ctypes.sizeof(status),
                                      ctypes.byref(needed)))
            if status.CurrentState != 4:  # SERVICE_RUNNING
                raise SandboxUnavailable(f"Windows network isolation requires the running {name} service")


def validate_policy(policy, cwd):
    """Retain the runtime's lexical root; a later junction cannot redefine it."""
    workspace = Path(normalize_path(policy.workspace))
    cwd = Path(normalize_path(cwd))
    if not cwd.is_relative_to(workspace):
        raise SandboxUnavailable("Command cwd is outside the originating sandbox workspace")
    for path in (workspace, cwd):
        with open_path(path, 0x1, directory=True):
            pass
    roots = (SYSTEM_DIRECTORY.parent, Path(sys.base_prefix).absolute())
    if any(path.is_relative_to(workspace) for path in roots):
        raise SandboxUnavailable("Workspace must not contain Windows or the base interpreter installation")
    if Path(sys.prefix).absolute() == workspace:
        raise SandboxUnavailable("Workspace must not be the active interpreter installation")
    return cwd


class WindowsSandbox:
    """One token/profile/ACL lease, released only after verified Job drain."""

    def __init__(self, policy, argv, cwd, env, cancellation):
        self.policy, self.cancellation = policy, cancellation
        self.process = None
        self.token = None
        self._profile = None
        self._package_sid = PSID()
        self._grants = None
        self._pins = ExitStack()
        self._closed = False
        self._check_cancelled()
        try:
            backend()
            _require_network_isolation()
            self.cwd = validate_policy(policy, cwd)
            executable = Path(normalize_path(argv[0]))
            if executable.name.lower() != "pwsh.exe":
                raise SandboxUnavailable("Windows sandbox execution requires PowerShell 7 (select /shell pwsh)")
            with open_path(executable, 0x1, directory=False):
                pass
            # Pins preserve the originating root, cwd and protected runtime names.
            self._pins.enter_context(open_path(policy.workspace, 0x1, directory=True,
                                                share_delete=False, share_write=False))
            self._pins.enter_context(open_path(self.cwd, 0x1, directory=True,
                                                share_delete=False, share_write=False))
            self._capability_buffers = [_capability("registryRead")]
            self._capabilities = (SidAndAttributes * len(self._capability_buffers))(
                *(SidAndAttributes(ctypes.cast(value, PSID), 0x4) for value in self._capability_buffers)
            )
            profile = "VirgoShell." + uuid.uuid4().hex
            _hresult(CreateProfile(profile, profile, "Temporary Virgo shell sandbox",
                                   self._capabilities, len(self._capabilities),
                                   ctypes.byref(self._package_sid)), "CreateAppContainerProfile")
            self._profile = profile
            self.package_sid = _sid_bytes(self._package_sid)
            self.package_name = _sid_string(self._package_sid)
            # This ordinary SID is unrelated to AppContainer capability SIDs.
            # A fresh 128-bit value is never shared by concurrent commands.
            self.write_name = "S-1-5-21-" + "-".join(str(value) for value in struct.unpack("<4I", uuid.uuid4().bytes))
            self._write_sid = _sid_from_string(self.write_name)
            self.write_sid = _sid_bytes(self._write_sid)
            self._make_token()
            folder = w.LPWSTR()
            _hresult(GetProfilePath(self.package_name, ctypes.byref(folder)), "GetAppContainerFolderPath")
            try:
                self.profile_path = Path(normalize_path(folder.value))
            finally:
                CoTaskMemFree(folder)
            self.home = self.profile_path / "home"
            self.scratch = self.profile_path / "tmp"
            for path in (self.home, self.home / "AppData", self.home / "AppData" / "Roaming",
                         self.home / "AppData" / "Local", self.scratch):
                path.mkdir(exist_ok=True)
            from .windows_sandbox_acl import AclGrants

            self._grants = AclGrants(self.package_sid, self.write_sid, cancellation)
            runtime_roots = tuple(dict.fromkeys((
                *policy.runtime_paths(), executable.parent,
            )))
            protected = tuple(dict.fromkeys((*policy.protected_paths(), *runtime_roots)))
            self._grants.grant_tree(policy.workspace, policy.mode == "workspace-write", protected,
                                    reject_hardlinks=True, excluded=runtime_roots)
            for root in runtime_roots:
                self._grants.grant_tree(root, False, reject_hardlinks=False)
            self._grants.grant_tree(self.profile_path, True, reject_hardlinks=True)
            self.environment = self._environment(env)
            self._check_cancelled()
        except BaseException as setup_error:
            try:
                self.cleanup()
            except BaseException as cleanup_error:
                failure = SandboxProcessError(
                    f"Windows sandbox setup failed ({setup_error}); cleanup failed ({cleanup_error}); "
                    f"profile: {self._profile}"
                )
                failure.envelope = self
                raise failure from setup_error
            raise

    def _check_cancelled(self):
        if self.cancellation is not None and self.cancellation.is_set():
            raise SandboxCancelled("Windows sandbox setup cancelled before payload release")

    def _make_token(self):
        source = HANDLE()
        _check(OpenProcessToken(GetCurrentProcess(), 0x008B, ctypes.byref(source)))  # QUERY | DUPLICATE | ASSIGN_PRIMARY | ADJUST_DEFAULT
        try:
            user = _token_info(source, 1)
            user_name = _sid_string(ctypes.c_void_p.from_buffer(user).value)
            restricting = (SidAndAttributes * 1)(SidAndAttributes(ctypes.cast(self._write_sid, PSID), 0))
            token = HANDLE()
            # DISABLE_MAX_PRIVILEGE | WRITE_RESTRICTED. No Everyone/logon SID
            # alternatives are added to this additional write-access check.
            _check(CreateRestrictedToken(source, 0x1 | 0x8, 0, None, 0, None,
                                          1, restricting, ctypes.byref(token)))
            self.token = token.value
            descriptor = ctypes.c_void_p()
            text = (f"D:(A;;GA;;;{user_name})(A;;GA;;;{self.package_name})"
                    f"(A;;GA;;;{self.write_name})(A;;RC;;;OW)")
            _check(ConvertSecurityDescriptor(text, 1, ctypes.byref(descriptor), None))
            try:
                present, defaulted, acl = BOOL(), BOOL(), ctypes.c_void_p()
                _check(GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                                                 ctypes.byref(acl), ctypes.byref(defaulted)))
                if not present.value or not acl.value:
                    raise SandboxUnavailable("Could not construct the Windows token default DACL")
                _check(SetTokenInformation(self.token, 6, ctypes.byref(acl), ctypes.sizeof(acl)))
            finally:
                LocalFree(descriptor)
        finally:
            CloseHandle(source)

    def process_attributes(self):
        capabilities = SecurityCapabilities(self._package_sid, self._capabilities,
                                             len(self._capabilities), 0)
        # SECURITY_CAPABILITIES; ALL_APPLICATION_PACKAGES_POLICY with OPT_OUT.
        return [(0x20009, capabilities), (0x2000F, DWORD(1))]

    def verify_process(self, process):
        token = HANDLE()
        _check(OpenProcessToken(process, 0x8, ctypes.byref(token)))
        try:
            for kind, description in ((29, "AppContainer"), (46, "LPAC")):
                info = _token_info(token, kind)
                if len(info) != ctypes.sizeof(DWORD) or DWORD.from_buffer(info).value != 1:
                    raise SandboxUnavailable(f"Windows did not establish the required {description} token")
            info = _token_info(token, 31)  # TOKEN_APPCONTAINER_INFORMATION
            if _sid_bytes(ctypes.c_void_p.from_buffer(info).value) != self.package_sid:
                raise SandboxUnavailable("Windows returned the wrong AppContainer identity")
            if _token_sids(token, 11) != {self.write_sid}:  # TokenRestrictedSids
                raise SandboxUnavailable("Windows did not retain the exclusive write-restricting SID")
            expected = {_sid_bytes(value) for value in self._capability_buffers}
            if _token_sids(token, 30) != expected:  # TokenCapabilities
                raise SandboxUnavailable("Windows returned unexpected sandbox capabilities")
        finally:
            CloseHandle(token)

    def _environment(self, source):
        allowed = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"}
        result = {key.upper(): value for key, value in source.items() if key.upper() in allowed}
        windows = _directory(GetWindowsDirectory)
        result.update(
            SYSTEMROOT=str(windows), WINDIR=str(windows), COMSPEC=str(SYSTEM_DIRECTORY / "cmd.exe"),
            PATHEXT=".COM;.EXE;.BAT;.CMD", HOME=str(self.home), USERPROFILE=str(self.home),
            HOMEDRIVE=self.home.drive, HOMEPATH=str(self.home)[len(self.home.drive):],
            APPDATA=str(self.home / "AppData" / "Roaming"),
            LOCALAPPDATA=str(self.home / "AppData" / "Local"),
            TEMP=str(self.scratch), TMP=str(self.scratch), TMPDIR=str(self.scratch), VIRGO="1",
            POWERSHELL_TELEMETRY_OPTOUT="1", POWERSHELL_UPDATECHECK="Off",
            DOTNET_CLI_TELEMETRY_OPTOUT="1",
        )
        return result

    def cleanup(self):
        if self._closed:
            return
        if self.process is not None and not self.process.tree_drained:
            raise SandboxProcessError("Windows sandbox resources require verified process-tree drain")
        # The caller must first prove Job drain. If revocation fails, retain
        # the profile/lease for diagnosis rather than claiming complete cleanup.
        if self._grants is not None:
            self._grants.cleanup()
            self._grants = None
        self._pins.close()
        if self.token is not None:
            _check(CloseHandle(self.token))
            self.token = None
        if self._profile is not None:
            result = DeleteProfile(self._profile)
            if result < 0:
                raise SandboxProcessError(f"Could not remove Windows sandbox profile {self._profile} (0x{result & 0xffffffff:08X})")
            self._profile = None
        if self._package_sid:
            FreeSid(self._package_sid)
            self._package_sid = PSID()
        self._closed = True
        self.process = None


def spawn(policy, argv, cwd, env, stdout, cancellation):
    from .windows_process import WindowsProcess

    envelope, proc = None, None
    try:
        envelope = WindowsSandbox(policy, argv, cwd, env, cancellation)
        proc = WindowsProcess(argv, cwd=envelope.cwd, env=envelope.environment,
                              stdout=stdout, sandbox=envelope)
        proc._virgo_sandbox_tmp = envelope
        proc._virgo_sandbox = {**policy.describe(), "backend": "windows-lpac",
                               "filesystem": "lpac-acl-with-write-restriction"}
        return proc
    except BaseException as error:
        proc = proc or getattr(error, "process", None) or (envelope.process if envelope is not None else None)
        if envelope is not None:
            if proc is not None:
                proc._virgo_sandbox_tmp = envelope
            try:
                if proc is not None:
                    proc.cleanup_tree()
                envelope.cleanup()
            except BaseException as cleanup_error:
                failure = SandboxProcessError(f"Windows sandbox launch failed ({error}); cleanup failed: {cleanup_error}")
                failure.process, failure.envelope = proc, envelope
                raise failure from error
        if proc is not None and getattr(proc, "_released", False):
            if isinstance(error, (SandboxCancelled, SandboxProcessError)):
                raise
            raise SandboxProcessError(f"Windows sandbox process started but launch reporting failed: {error}") from error
        if isinstance(error, (OSError, AttributeError)) and not isinstance(error, SandboxCancelled):
            raise SandboxUnavailable(f"Could not establish the Windows sandbox: {error}") from error
        raise
