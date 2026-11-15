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
from dataclasses import dataclass
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
user = ctypes.WinDLL(str(SYSTEM_DIRECTORY / "user32.dll"), use_last_error=True)


class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", PSID), ("Attributes", DWORD)]


class TokenGroups(ctypes.Structure):
    _fields_ = [("Count", DWORD), ("Groups", SidAndAttributes * 1)]


class SecurityCapabilities(ctypes.Structure):
    _fields_ = [("AppContainerSid", PSID), ("Capabilities", ctypes.POINTER(SidAndAttributes)),
                ("CapabilityCount", DWORD), ("Reserved", DWORD)]


class GenericMapping(ctypes.Structure):
    _fields_ = [(name, DWORD) for name in ("Read", "Write", "Execute", "All")]


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("Length", DWORD), ("Descriptor", ctypes.c_void_p), ("Inherit", BOOL)]


class ServiceStatus(ctypes.Structure):
    _fields_ = [(name, DWORD) for name in (
        "ServiceType", "CurrentState", "ControlsAccepted", "Win32ExitCode",
        "ServiceSpecificExitCode", "CheckPoint", "WaitHint", "ProcessId", "ServiceFlags",
    )]


OpenProcessToken = _api(advapi, "OpenProcessToken", [HANDLE, DWORD, ctypes.POINTER(HANDLE)])
DuplicateToken = _api(advapi, "DuplicateToken", [HANDLE, ctypes.c_int, ctypes.POINTER(HANDLE)])
AccessCheck = _api(advapi, "AccessCheck", [
    ctypes.c_void_p, HANDLE, DWORD, ctypes.POINTER(GenericMapping), ctypes.c_void_p,
    ctypes.POINTER(DWORD), ctypes.POINTER(DWORD), ctypes.POINTER(BOOL),
])
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
GetSecurityDescriptorOwner = _api(advapi, "GetSecurityDescriptorOwner", [
    ctypes.c_void_p, ctypes.POINTER(PSID), ctypes.POINTER(BOOL),
])
CreateMutexEx = _api(kernel, "CreateMutexExW", [
    ctypes.POINTER(SecurityAttributes), w.LPCWSTR, DWORD, DWORD,
], HANDLE)
CreateFile = _api(kernel, "CreateFileW", [
    w.LPCWSTR, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, HANDLE,
], HANDLE)
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
MakeSelfRelativeSD = _api(advapi, "MakeSelfRelativeSD", [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(DWORD),
])
GetProcessWindowStation = _api(user, "GetProcessWindowStation", [], HANDLE)
GetUserObjectInformation = _api(user, "GetUserObjectInformationW", [
    HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD),
])
OpenWindowStation = _api(user, "OpenWindowStationW", [w.LPCWSTR, BOOL, DWORD], HANDLE)
CloseWindowStation = _api(user, "CloseWindowStation", [HANDLE])
GetUserObjectSecurity = _api(user, "GetUserObjectSecurity", [
    HANDLE, ctypes.POINTER(DWORD), ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD),
])
SetUserObjectSecurity = _api(user, "SetUserObjectSecurity", [
    HANDLE, ctypes.POINTER(DWORD), ctypes.c_void_p,
])
CreateDesktop = _api(user, "CreateDesktopW", [
    w.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, DWORD, DWORD,
    ctypes.POINTER(SecurityAttributes),
], HANDLE)
CloseDesktop = _api(user, "CloseDesktop", [HANDLE])
GetCurrentThreadId = _api(kernel, "GetCurrentThreadId", [], DWORD)
GetThreadDesktop = _api(user, "GetThreadDesktop", [DWORD], HANDLE)
SetThreadDesktop = _api(user, "SetThreadDesktop", [HANDLE])


def _get_user_security(handle, information, descriptor, size, needed):
    return GetUserObjectSecurity(handle, ctypes.byref(DWORD(information)), descriptor, size, needed)


def _set_user_security(handle, information, descriptor):
    # Unlike SetKernelObjectSecurity, USER objects require a self-relative SD.
    size = DWORD()
    MakeSelfRelativeSD(descriptor, None, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    relative = ctypes.create_string_buffer(size.value)
    _check(MakeSelfRelativeSD(descriptor, relative, ctypes.byref(size)))
    return SetUserObjectSecurity(handle, ctypes.byref(DWORD(information)), relative)


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
        error = ctypes.WinError(ctypes.get_last_error())
        raise SandboxUnavailable(f"Could not query Windows token information {kind}: {error}")
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


def _verify_lpac(token):
    """Prove the LPAC access distinction using the actual suspended token.

    The class-46 GetTokenInformation query is unavailable on the supported
    Windows runners. LPAC must ignore AAP grants while retaining ARAP access.
    Everyone satisfies only the ordinary user/group check in each descriptor.
    """
    duplicate = HANDLE()
    _check(DuplicateToken(token, 2, ctypes.byref(duplicate)))  # SecurityImpersonation
    try:
        mapping = GenericMapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
        for package, expected in (("S-1-15-2-1", False), ("S-1-15-2-2", True)):
            descriptor = ctypes.c_void_p()
            _check(ConvertSecurityDescriptor(
                f"O:SYG:SYD:(A;;0x1;;;WD)(A;;0x1;;;{package})",
                1, ctypes.byref(descriptor), None,
            ))
            try:
                privileges = ctypes.create_string_buffer(256)
                size, granted, permitted = DWORD(len(privileges)), DWORD(), BOOL()
                # FILE_READ_DATA is already specific, with no generic bits or
                # write rights that could conflate LPAC and write restrictions.
                _check(AccessCheck(descriptor, duplicate, 0x1, ctypes.byref(mapping),
                                   privileges, ctypes.byref(size), ctypes.byref(granted),
                                   ctypes.byref(permitted)))
                if bool(permitted.value) != expected or granted.value != int(expected):
                    raise SandboxUnavailable(
                        f"Windows did not establish LPAC access semantics for {package}: "
                        f"allowed={bool(permitted.value)}, access=0x{granted.value:X}"
                    )
            finally:
                LocalFree(descriptor)
    finally:
        CloseHandle(duplicate)


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


@dataclass
class _DeviceLease:
    name: str
    access: int
    sids: tuple[bytes, ...]
    handle: int | None = None
    mutex: int | None = None


class WindowsSandbox:
    """One token/profile/ACL lease, released only after verified Job drain."""

    def __init__(self, policy, argv, cwd, env, cancellation):
        self.policy, self.cancellation = policy, cancellation
        self.process = None
        self.token = None
        self._profile = None
        self._package_sid = PSID()
        self._grants = None
        self._devices: list[_DeviceLease] = []
        self._station = None
        self._desktop = None
        self.desktop = None
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
            # These capabilities use Windows' existing token-wide grants.
            # lpacInstrumentation permits ETW registrations wherever its SID is
            # authorized; this is not a per-provider whitelist or an ETW ACL edit.
            self._capability_buffers = [_capability(name) for name in ("registryRead", "lpacInstrumentation")]
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
            # Exactly the measured native BCrypt open, plus the CRT O_RDWR
            # open used by subprocess.DEVNULL. Null has no ambient LPAC grant,
            # so it also needs this command's package identity. Neither lease
            # grants ownership, DACL mutation, deletion or other device names.
            self._prepare_device("KsecDD", 0x100003, (self.write_sid,))
            self._prepare_device("Null", 0x12019F, (self.package_sid, self.write_sid))
            runtime_roots = tuple(dict.fromkeys((
                *policy.runtime_paths(), executable.parent,
            )))
            protected = tuple(dict.fromkeys((*policy.protected_paths(), *runtime_roots)))
            self._grants.grant_tree(policy.workspace, policy.mode == "workspace-write", protected,
                                    reject_hardlinks=True, excluded=runtime_roots)
            for root in runtime_roots:
                self._grants.grant_tree(root, False, reject_hardlinks=False, skip_reparse=True)
            self._grants.grant_tree(self.profile_path, True, reject_hardlinks=True)
            self._prepare_desktop()
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
            self._user_name = user_name
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

    def _prepare_device(self, name, access, sids):
        from .windows_sandbox_acl import GetSecurity, _ace, _read

        # These fixed devices each need a machine-wide mutex, separate from
        # the filesystem's per-user mutex. Only authorized administrators or
        # SYSTEM may use this fixed protected mutex; never take over an existing
        # object with an untrusted owner or a different access policy.
        lease = _DeviceLease(name, access, sids)
        self._devices.append(lease)
        descriptor = ctypes.c_void_p()
        _check(ConvertSecurityDescriptor(
            "O:BAG:BAD:P(A;;0x1f0001;;;SY)(A;;0x120001;;;BA)",
            1, ctypes.byref(descriptor), None,
        ))
        try:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            lease.mutex = _check(CreateMutexEx(
                ctypes.byref(attributes), rf"Global\Virgo.NativeSandbox.{name}.v1", 0, 0x120001,
            ))  # SYNCHRONIZE | MUTEX_MODIFY_STATE | READ_CONTROL.
        finally:
            LocalFree(descriptor)
        size = DWORD()
        GetSecurity(lease.mutex, 1 | 4, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        security = ctypes.create_string_buffer(size.value)
        _check(GetSecurity(lease.mutex, 1 | 4, security, len(security), ctypes.byref(size)))
        owner, defaulted = PSID(), BOOL()
        _check(GetSecurityDescriptorOwner(security, ctypes.byref(owner), ctypes.byref(defaulted)))
        policy = _read(lease.mutex)
        expected = [_ace(0, 0, mask, _sid_bytes(_sid_from_string(sid))) for sid, mask in (
            ("S-1-5-18", 0x1F0001), ("S-1-5-32-544", 0x120001),
        )]
        if (not owner or _sid_string(owner) not in {"S-1-5-18", "S-1-5-32-544"}
                or not policy.control & 0x1000 or policy.aces != expected):
            raise SandboxUnavailable(f"Windows {name} coordination mutex has an untrusted security descriptor")
        self._check_cancelled()
        # This exact native object and native authority were measured in the
        # failing runtime operations. No path, capability or broader device set is
        # supplied by the payload. Retain the handle until verified Job drain.
        handle = CreateFile(rf"\\?\GLOBALROOT\Device\{name}", 0x60000, 7, None, 3, 0, None)
        if handle == HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        lease.handle = _check(handle)  # READ_CONTROL | WRITE_DAC, never elevation.
        self._device_access(lease)

    def _device_access(self, lease, *, cleanup=False):
        from .windows_sandbox_acl import _ace, _read, _sid_in_ace, _write

        with self._grants._locked(cleanup=cleanup, mutex=lease.mutex):
            previous = _read(lease.handle, cleanup=cleanup)
            if previous is None:
                return
            retained = [entry for entry in previous.aces if _sid_in_ace(entry) not in lease.sids]
            owned = [] if cleanup else [_ace(0, 0, lease.access, sid) for sid in lease.sids]
            entries = retained + owned
            if entries != previous.aces:
                _write(lease.handle, previous, entries, previous.control)
            current = _read(lease.handle, cleanup=cleanup)
            actual = [] if current is None else [
                entry for entry in current.aces if _sid_in_ace(entry) in lease.sids
            ]
            if actual != owned:
                raise OSError(f"Windows did not retain the required {lease.name} lease state")

    def _release_device(self):
        try:
            for lease in tuple(self._devices):
                if lease.handle is not None:
                    self._device_access(lease, cleanup=True)
                    _check(CloseHandle(lease.handle))
                    lease.handle = None
                if lease.mutex is not None:
                    _check(CloseHandle(lease.mutex))
                    lease.mutex = None
                self._devices.remove(lease)
        except OSError as error:
            raise SandboxProcessError(f"Could not release Windows sandbox device resources: {error}") from error

    def _station_access(self, *, cleanup=False):
        from .windows_sandbox_acl import _ace, _read, _sid_in_ace, _write

        # Limit the station lease to attributes/global atoms for the private
        # desktop. Do not grant clipboard, screen or desktop creation
        # rights on the host station, and never lower its integrity label.
        access = 0x20022  # READ_CONTROL | WINSTA_READATTRIBUTES | ACCESSGLOBALATOMS
        with self._grants._locked(cleanup=cleanup):
            previous = _read(self._station, cleanup=cleanup, getter=_get_user_security)
            if previous is None:
                return
            retained = [ace for ace in previous.aces if _sid_in_ace(ace) not in self._grants.sids]
            if cleanup:
                if len(retained) == len(previous.aces):
                    return
                aces = retained
            else:
                deny = [_ace(1, 0, 0xF037F & ~access, sid) for sid in self._grants.sids]
                allow = [_ace(0, 0, access, sid) for sid in self._grants.sids]
                aces = deny + retained + allow
            _write(self._station, previous, aces, previous.control, setter=_set_user_security)

    def _prepare_desktop(self):
        # GetProcessWindowStation returns a borrowed handle; acquire our own
        # handle before editing its DACL, and retain it through Job drain.
        current = _check(GetProcessWindowStation())
        station_name, needed = ctypes.create_unicode_buffer(32768), DWORD()
        _check(GetUserObjectInformation(current, 2, station_name,
                                        ctypes.sizeof(station_name), ctypes.byref(needed)))
        self._station = _check(OpenWindowStation(station_name.value, False, 0x60002))
        self._station_access()
        name = "VirgoShell." + uuid.uuid4().hex
        descriptor = ctypes.c_void_p()
        # The private desktop permits ordinary window operations, but not hooks,
        # journaling, switching desktops or rewriting its security descriptor.
        participant = 0x200C7  # READ_CONTROL | READ/WRITEOBJECTS | CREATEWINDOW/MENU | ENUMERATE
        text = (f"O:{self._user_name}G:{self._user_name}D:P(A;;GA;;;SY)"
                f"(A;;GA;;;{self._user_name})(A;;0x{participant:X};;;{self.package_name})"
                f"(A;;0x{participant:X};;;{self.write_name})(A;;RC;;;OW)S:(ML;;NW;;;LW)")
        _check(ConvertSecurityDescriptor(text, 1, ctypes.byref(descriptor), None))
        try:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            original = _check(GetThreadDesktop(GetCurrentThreadId()))  # Borrowed.
            ctypes.set_last_error(0)
            self._desktop = _check(CreateDesktop(name, None, None, 0, 0xF01FF, ctypes.byref(attributes)))
            existing = ctypes.get_last_error() == 183
            if _check(GetThreadDesktop(GetCurrentThreadId())) != original:
                _check(SetThreadDesktop(original))
            if existing:
                raise SandboxUnavailable("Windows sandbox desktop identity was already in use")
            self.desktop = station_name.value + "\\" + name
        finally:
            LocalFree(descriptor)

    def _release_desktop(self):
        try:
            if self._desktop is not None:
                _check(CloseDesktop(self._desktop))
                self._desktop = None
            if self._station is not None:
                self._station_access(cleanup=True)
                _check(CloseWindowStation(self._station))
                self._station = None
        except OSError as error:
            raise SandboxProcessError(f"Could not release Windows sandbox desktop resources: {error}") from error

    def process_attributes(self):
        capabilities = SecurityCapabilities(self._package_sid, self._capabilities,
                                             len(self._capabilities), 0)
        # SECURITY_CAPABILITIES; ALL_APPLICATION_PACKAGES_POLICY with OPT_OUT.
        return [(0x20009, capabilities), (0x2000F, DWORD(1))]

    def verify_process(self, process):
        token = HANDLE()
        _check(OpenProcessToken(process, 0xA, ctypes.byref(token)))  # QUERY | DUPLICATE
        try:
            info = _token_info(token, 29)  # TokenIsAppContainer
            if len(info) != ctypes.sizeof(DWORD) or DWORD.from_buffer(info).value != 1:
                raise SandboxUnavailable("Windows did not establish the required AppContainer token")
            _verify_lpac(token)
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
        self._release_desktop()
        self._release_device()
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
