"""Native Windows Standard AppContainer sandbox with session-level single SID.

Replaces the legacy Administrator-only LPAC backend with Standard AppContainer:
- Retains ALL APPLICATION PACKAGES (S-1-15-2-1); never sets LPAC opt-out.
- Launches via CreateProcessW (no CreateProcessAsUserW, no restricted token,
  no WRITE_RESTRICTED, no device DACL edits, no admin mutex, no WindowStation
  or Desktop DACL mutations).
- Reuses one AppContainer profile and SID per (workspace, mode) session with
  single-call OS-propagated SetNamedSecurityInfoW inheritable ACEs for O(1)
  per-command launch overhead.
- Maps a session-local DefineDosDeviceW virtual drive when workspace ancestors
  (such as C:\\Users) lack S-1-15-2-1 read access so pwsh, powershell.exe,
  cmd dir, and pytest work cleanly without ancestor-ACL failures.
- Persists an atomic write-ahead JSON session journal for deterministic crash
  recovery via recover_stale_sessions().
"""
from __future__ import annotations

import atexit
import ctypes
import json
import ntpath
import os
import shutil
import subprocess
import sys
import threading
import uuid
import weakref
from collections.abc import Sequence
from contextlib import ExitStack
from ctypes import wintypes as w
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import _windows_probe
from ..execution_policy import filter_sandbox_environment
from .platform import SandboxCancelled, SandboxProcessError, SandboxUnavailable
from .windows_acl import (
    acl_state_lock,
    ancestor_lacks_aap,
    can_open_write_dac,
    capture_dacl_snapshot,
    grant_root_access,
    has_aap_read_access,
    is_dacl_protected,
    is_protected_readonly_for_sid,
    protect_readonly_path,
    revoke_sid_from_root,
)
from .windows_files import is_reparse_point, normalize_path, verify_local_ntfs

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("virgo.sandbox.windows_sandbox requires Windows")

BACKEND_NAME = "windows-appcontainer"
READ_ONLY_CAPABILITIES: tuple[str, ...] = ()
WORKSPACE_EDIT_CAPABILITIES: tuple[str, ...] = ("internetClient", "privateNetworkClientServer")

HANDLE = ctypes.c_void_p
PSID = ctypes.c_void_p
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HRESULT = ctypes.c_int32

PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
SE_GROUP_ENABLED = 0x00000004

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87

DDD_REMOVE_DEFINITION = 0x00000002
DDD_EXACT_MATCH_ON_REMOVE = 0x00000004

HRESULT_ALREADY_EXISTS = 0x800700B7
_HRESULT_MISSING_PROFILE = frozenset({
    0x80070002,  # ERROR_FILE_NOT_FOUND
    0x80070003,  # ERROR_PATH_NOT_FOUND
    0x80070490,  # ERROR_NOT_FOUND
})


def _api(library: ctypes.WinDLL, name: str, args: list[Any], result: Any = BOOL):
    fn = getattr(library, name)
    fn.argtypes = args
    fn.restype = result
    return fn


def _check(value: Any) -> Any:
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_GetSystemDirectoryW = _api(_kernel32, "GetSystemDirectoryW", [w.LPWSTR, DWORD], DWORD)
_GetWindowsDirectoryW = _api(_kernel32, "GetWindowsDirectoryW", [w.LPWSTR, DWORD], DWORD)
_CloseHandle = _api(_kernel32, "CloseHandle", [HANDLE])
_LocalFree = _api(_kernel32, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
_GetCurrentProcess = _api(_kernel32, "GetCurrentProcess", [], HANDLE)
_GetCurrentThreadId = _api(_kernel32, "GetCurrentThreadId", [], DWORD)
_IsWow64Process2 = _api(
    _kernel32, "IsWow64Process2", [HANDLE, ctypes.POINTER(w.WORD), ctypes.POINTER(w.WORD)]
)
_OpenProcess = _api(_kernel32, "OpenProcess", [DWORD, BOOL, DWORD], HANDLE)
_GetExitCodeProcess = _api(_kernel32, "GetExitCodeProcess", [HANDLE, ctypes.POINTER(DWORD)])
_GetLogicalDrives = _api(_kernel32, "GetLogicalDrives", [], DWORD)
_DefineDosDeviceW = _api(_kernel32, "DefineDosDeviceW", [DWORD, w.LPCWSTR, w.LPCWSTR])
_QueryDosDeviceW = _api(_kernel32, "QueryDosDeviceW", [w.LPCWSTR, w.LPWSTR, DWORD], DWORD)


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]


_GetProcessTimes = _api(
    _kernel32,
    "GetProcessTimes",
    [
        HANDLE,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ],
)


def _directory(api) -> Path:
    buf = ctypes.create_unicode_buffer(32768)
    count = _check(api(buf, len(buf)))
    if count >= len(buf):
        raise SandboxUnavailable("Windows system directory exceeds the native path limit")
    return Path(normalize_path(buf.value))


SYSTEM_DIRECTORY = _directory(_GetSystemDirectoryW)
WINDOWS_DIRECTORY = _directory(_GetWindowsDirectoryW)

_advapi32 = ctypes.WinDLL(str(SYSTEM_DIRECTORY / "advapi32.dll"), use_last_error=True)
_userenv = ctypes.WinDLL(str(SYSTEM_DIRECTORY / "userenv.dll"), use_last_error=True)
_kernelbase = ctypes.WinDLL(str(SYSTEM_DIRECTORY / "kernelbase.dll"), use_last_error=True)
_ole32 = ctypes.WinDLL(str(SYSTEM_DIRECTORY / "ole32.dll"), use_last_error=True)
_user32 = ctypes.WinDLL(str(SYSTEM_DIRECTORY / "user32.dll"), use_last_error=True)


class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", PSID), ("Attributes", DWORD)]


class SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", PSID),
        ("Capabilities", ctypes.POINTER(SidAndAttributes)),
        ("CapabilityCount", DWORD),
        ("Reserved", DWORD),
    ]


class ServiceStatus(ctypes.Structure):
    _fields_ = [
        (name, DWORD)
        for name in (
            "ServiceType",
            "CurrentState",
            "ControlsAccepted",
            "Win32ExitCode",
            "ServiceSpecificExitCode",
            "CheckPoint",
            "WaitHint",
            "ProcessId",
            "ServiceFlags",
        )
    ]


_ConvertSidToStringSidW = _api(
    _advapi32, "ConvertSidToStringSidW", [PSID, ctypes.POINTER(w.LPWSTR)]
)
_GetLengthSid = _api(_advapi32, "GetLengthSid", [PSID], DWORD)
_FreeSid = _api(_advapi32, "FreeSid", [PSID], ctypes.c_void_p)
_OpenSCManagerW = _api(_advapi32, "OpenSCManagerW", [w.LPCWSTR, w.LPCWSTR, DWORD], HANDLE)
_OpenServiceW = _api(_advapi32, "OpenServiceW", [HANDLE, w.LPCWSTR, DWORD], HANDLE)
_QueryServiceStatusEx = _api(
    _advapi32,
    "QueryServiceStatusEx",
    [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)],
)
_CloseServiceHandle = _api(_advapi32, "CloseServiceHandle", [HANDLE])

_CreateAppContainerProfile = _api(
    _userenv,
    "CreateAppContainerProfile",
    [
        w.LPCWSTR,
        w.LPCWSTR,
        w.LPCWSTR,
        ctypes.POINTER(SidAndAttributes),
        DWORD,
        ctypes.POINTER(PSID),
    ],
    HRESULT,
)
_DeriveAppContainerSidFromAppContainerName = _api(
    _userenv, "DeriveAppContainerSidFromAppContainerName", [w.LPCWSTR, ctypes.POINTER(PSID)], HRESULT
)
_DeleteAppContainerProfile = _api(_userenv, "DeleteAppContainerProfile", [w.LPCWSTR], HRESULT)
_GetAppContainerFolderPath = _api(
    _userenv, "GetAppContainerFolderPath", [w.LPCWSTR, ctypes.POINTER(w.LPWSTR)], HRESULT
)
_CoTaskMemFree = _api(_ole32, "CoTaskMemFree", [ctypes.c_void_p], None)
_DeriveCapabilitySidsFromName = _api(
    _kernelbase,
    "DeriveCapabilitySidsFromName",
    [
        w.LPCWSTR,
        ctypes.POINTER(ctypes.POINTER(PSID)),
        ctypes.POINTER(DWORD),
        ctypes.POINTER(ctypes.POINTER(PSID)),
        ctypes.POINTER(DWORD),
    ],
)

_GetProcessWindowStation = _api(_user32, "GetProcessWindowStation", [], HANDLE)
_GetThreadDesktop = _api(_user32, "GetThreadDesktop", [DWORD], HANDLE)
_GetUserObjectInformationW = _api(
    _user32,
    "GetUserObjectInformationW",
    [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)],
)
_OpenDesktopW = _api(_user32, "OpenDesktopW", [w.LPCWSTR, DWORD, BOOL, DWORD], HANDLE)
_CloseDesktop = _api(_user32, "CloseDesktop", [HANDLE])


def _sid_bytes(sid_ptr: Any) -> bytes:
    size = _check(_GetLengthSid(sid_ptr))
    return ctypes.string_at(sid_ptr, size)


def _sid_string(sid_ptr: Any) -> str:
    output = w.LPWSTR()
    _check(_ConvertSidToStringSidW(sid_ptr, ctypes.byref(output)))
    try:
        return output.value
    finally:
        _LocalFree(output)


def _hresult(value: int, operation: str) -> None:
    if value < 0:
        raise SandboxUnavailable(f"Windows {operation} failed (HRESULT 0x{value & 0xFFFFFFFF:08X})")


def _filetime_to_int(ft: _FileTime) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


def _current_process_create_time() -> int | None:
    ct, et, kt, ut = _FileTime(), _FileTime(), _FileTime(), _FileTime()
    if _GetProcessTimes(
        _GetCurrentProcess(), ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)
    ):
        return _filetime_to_int(ct)
    return None


def is_process_alive(pid: int, expected_create_time: int | None = None) -> bool:
    """Return True if ``pid`` is still running (and matches ``expected_create_time`` if given)."""
    if pid <= 0:
        return False
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = DWORD()
        if not _GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return False
        if expected_create_time is not None:
            ct, et, kt, ut = _FileTime(), _FileTime(), _FileTime(), _FileTime()
            if _GetProcessTimes(
                handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)
            ):
                if _filetime_to_int(ct) != expected_create_time:
                    return False
        return True
    finally:
        _CloseHandle(handle)


def state_dir() -> Path:
    """Return the per-user directory storing Windows AppContainer session journals."""
    override = os.environ.get("VIRGO_WINDOWS_SANDBOX_STATE_DIR")
    if override:
        return Path(os.path.abspath(override))
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else (Path.home() / "AppData" / "Local")
    return base / "Virgo" / "sandbox-sessions"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    data = json.dumps(payload, indent=2, sort_keys=True)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _derive_capability(name: str) -> bytes:
    groups = ctypes.POINTER(PSID)()
    caps = ctypes.POINTER(PSID)()
    group_count, cap_count = DWORD(), DWORD()
    _check(
        _DeriveCapabilitySidsFromName(
            name,
            ctypes.byref(groups),
            ctypes.byref(group_count),
            ctypes.byref(caps),
            ctypes.byref(cap_count),
        )
    )
    try:
        if cap_count.value != 1:
            raise SandboxUnavailable(f"Windows did not resolve the required {name} capability")
        return _sid_bytes(caps[0])
    finally:
        for pointers, count in ((groups, group_count.value), (caps, cap_count.value)):
            if pointers:
                for idx in range(count):
                    if pointers[idx]:
                        _LocalFree(pointers[idx])
                _LocalFree(pointers)


def _create_profile(name: str, cap_bytes_list: Sequence[bytes]) -> tuple[str, bytes, Path]:
    cap_bufs = [ctypes.create_string_buffer(raw) for raw in cap_bytes_list]
    if cap_bufs:
        cap_array = (SidAndAttributes * len(cap_bufs))(
            *(SidAndAttributes(ctypes.cast(buf, PSID), SE_GROUP_ENABLED) for buf in cap_bufs)
        )
        cap_ptr = ctypes.cast(cap_array, ctypes.POINTER(SidAndAttributes))
    else:
        cap_array = None
        cap_ptr = None

    sid_ptr = PSID()
    hr = _CreateAppContainerProfile(
        name,
        name,
        "Virgo session Windows AppContainer sandbox",
        cap_ptr,
        len(cap_bufs),
        ctypes.byref(sid_ptr),
    )
    if hr >= 0:
        try:
            sid_str = _sid_string(sid_ptr)
            sid_raw = _sid_bytes(sid_ptr)
        finally:
            if sid_ptr:
                _FreeSid(sid_ptr)
    elif (hr & 0xFFFFFFFF) == HRESULT_ALREADY_EXISTS:
        derived = PSID()
        _hresult(
            _DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(derived)),
            "DeriveAppContainerSidFromAppContainerName",
        )
        try:
            sid_str = _sid_string(derived)
            sid_raw = _sid_bytes(derived)
        finally:
            if derived:
                _FreeSid(derived)
    else:
        _hresult(hr, "CreateAppContainerProfile")
        raise AssertionError("unreachable")  # pragma: no cover

    folder_ptr = w.LPWSTR()
    _hresult(
        _GetAppContainerFolderPath(sid_str, ctypes.byref(folder_ptr)),
        "GetAppContainerFolderPath",
    )
    try:
        folder = Path(normalize_path(folder_ptr.value))
    finally:
        if folder_ptr:
            _CoTaskMemFree(folder_ptr)
    return sid_str, sid_raw, folder


def _delete_profile(name: str, *, ignore_missing: bool = False) -> bool:
    hr = _DeleteAppContainerProfile(name)
    if hr >= 0:
        return True
    if ignore_missing and (hr & 0xFFFFFFFF) in _HRESULT_MISSING_PROFILE:
        return False
    raise SandboxProcessError(
        f"Could not remove Windows AppContainer profile {name} (HRESULT 0x{hr & 0xFFFFFFFF:08X})"
    )


def _resolve_desktop() -> str | None:
    """Return 'WinSta0\\Default' under non-Default custom desktops; None otherwise.

    Never mutates any WindowStation or Desktop DACL.
    """
    def _obj_name(handle: HANDLE) -> str | None:
        if not handle:
            return None
        buf = ctypes.create_unicode_buffer(256)
        needed = DWORD()
        if not _GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return None
        return buf.value

    station = (_obj_name(_GetProcessWindowStation()) or "").lower()
    desktop = (_obj_name(_GetThreadDesktop(_GetCurrentThreadId())) or "").lower()
    if station == "winsta0" and desktop != "default":
        hd = _OpenDesktopW("Default", 0, False, 0x20000)  # READ_CONTROL only
        if hd:
            _CloseDesktop(hd)
            return "WinSta0\\Default"
    return None


_DOS_DRIVE_LOCK = threading.Lock()


def _allocate_dos_drive(workspace: Path) -> tuple[str, str] | None:
    """Map an unused drive letter (Z:..M:) to ``workspace`` via DefineDosDeviceW."""
    target = str(workspace)
    probe = ctypes.create_unicode_buffer(4096)
    with _DOS_DRIVE_LOCK, acl_state_lock():
        mask = _GetLogicalDrives()
        if not mask:
            return None
        for code in range(ord("Z"), ord("M") - 1, -1):
            if mask & (1 << (code - ord("A"))):
                continue
            drive = f"{chr(code)}:"
            # DefineDosDeviceW pushes onto an existing definition; confirm unmapped first.
            if _QueryDosDeviceW(drive, probe, len(probe)):
                continue
            if _DefineDosDeviceW(0, drive, target):
                return drive, target
    return None


def _remove_dos_drive(drive: str | None, target: str | None) -> None:
    if not drive:
        return
    buf = ctypes.create_unicode_buffer(4096)
    with _DOS_DRIVE_LOCK, acl_state_lock():
        if not _QueryDosDeviceW(drive, buf, len(buf)):
            return
        if target and _DefineDosDeviceW(
            DDD_REMOVE_DEFINITION | DDD_EXACT_MATCH_ON_REMOVE, drive, target
        ):
            return
        err = ctypes.get_last_error()
        if err == _ERROR_FILE_NOT_FOUND:
            return
        # Verify whether the mapping still points at our target before fallback removal.
        if target and ntpath.normcase(buf.value) == ntpath.normcase("\\??\\" + target):
            _DefineDosDeviceW(DDD_REMOVE_DEFINITION, drive, None)


def _map_cwd_to_drive(cwd: Path, workspace: Path, dos_drive: str | None) -> Path:
    if not dos_drive:
        return cwd
    rel = os.path.relpath(str(cwd), str(workspace))
    if rel == ".":
        return Path(dos_drive + "\\")
    if rel == ".." or rel.startswith("..\\"):
        return cwd
    return Path(dos_drive + "\\" + rel)


def backend() -> tuple[str, Path]:
    """Discover native x64 Windows 10+ AppContainer API availability."""
    if sys.getwindowsversion().major < 10:
        raise SandboxUnavailable("The Windows sandbox requires Windows 10-family native APIs")
    process_machine, native_machine = w.WORD(), w.WORD()
    _check(_IsWow64Process2(_GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native_machine)))
    if ctypes.sizeof(HANDLE) != 8 or process_machine.value != 0 or native_machine.value != 0x8664:
        raise SandboxUnavailable("The Windows sandbox currently supports native x64 Python only")
    return BACKEND_NAME, SYSTEM_DIRECTORY / "kernelbase.dll"


def _require_network_isolation() -> None:
    with ExitStack() as resources:
        manager = _check(_OpenSCManagerW(None, None, 0x1))
        resources.callback(_CloseServiceHandle, manager)
        for name in ("BFE", "MpsSvc"):
            service = _check(_OpenServiceW(manager, name, 0x4))
            resources.callback(_CloseServiceHandle, service)
            status, needed = ServiceStatus(), DWORD()
            _check(
                _QueryServiceStatusEx(
                    service, 0, ctypes.byref(status), ctypes.sizeof(status), ctypes.byref(needed)
                )
            )
            if status.CurrentState != 4:  # SERVICE_RUNNING
                raise SandboxUnavailable(
                    f"Windows network isolation requires the running {name} service",
                    reason="windows_network_isolation_unavailable",
                    dimension="windows_network_isolation",
                )


UNSUPPORTED_WINDOWS_GUIDANCE = (
    "Native Windows AppContainer sandboxing requires Windows 11 with updated null.sys "
    "(Feature 60692039, granting ALL APPLICATION PACKAGES access on \\Device\\Null). "
    "Options on this Windows system:\n"
    "  1. WSL2: run Virgo inside WSL2 for full Linux sandbox isolation (recommended).\n"
    "  2. No-Limit: use --permission no-limit or /permissions no-limit.\n"
    "  3. Update Windows: update to Windows 11 with the null.sys AppContainer update (Feature 60692039)."
)


def is_windows_sandbox_supported() -> bool:
    """Return True when x64 Windows 10+ and \\Device\\Null grants S-1-15-2-1 read/write."""
    try:
        backend()
    except (SandboxUnavailable, OSError):
        return False
    return _windows_probe.null_device_allows_appcontainer()


def precheck_windows_sandbox() -> None:
    """Verify Standard AppContainer prerequisites (x64 Win11 null.sys Feature 60692039 and BFE/MpsSvc)."""
    try:
        backend()
    except SandboxUnavailable as error:
        raise SandboxUnavailable(
            f"{error}. {UNSUPPORTED_WINDOWS_GUIDANCE}",
            reason="windows_unsupported_os",
            dimension="windows_appcontainer_sandbox",
        ) from error
    if not _windows_probe.null_device_allows_appcontainer():
        raise SandboxUnavailable(
            UNSUPPORTED_WINDOWS_GUIDANCE,
            reason="windows_null_device_unavailable",
            dimension="windows_null_device",
        )
    try:
        _require_network_isolation()
    except SandboxUnavailable as error:
        raise SandboxUnavailable(
            f"{error}. On Windows, ensure Windows Filtering Platform (BFE/MpsSvc) is running, "
            "or switch to No-Limit (--permission no-limit / /permissions no-limit).",
            reason="windows_network_isolation_unavailable",
            dimension="windows_network_isolation",
        ) from error


def validate_policy(policy, cwd: Path) -> Path:
    """Validate workspace and cwd on a fixed volume with persistent ACLs."""
    workspace = verify_local_ntfs(policy.workspace, allow_reparse=True, expect_directory=True)
    resolved_cwd = verify_local_ntfs(cwd, allow_reparse=True, expect_directory=True)
    allowed_cwd_roots = (workspace, *(Path(normalize_path(path)) for path, _ in policy.grants))
    if not any(resolved_cwd.is_relative_to(root) for root in allowed_cwd_roots):
        raise SandboxUnavailable(
            "Command cwd is outside the originating sandbox workspace",
            reason="invalid_cwd",
        )
    if policy.private_path(resolved_cwd) or policy.private_path(workspace):
        raise SandboxUnavailable(
            "Command cwd is inside private Virgo storage",
            reason="protected_path",
            target=resolved_cwd,
        )
    system_roots = (WINDOWS_DIRECTORY, Path(sys.base_prefix).resolve())
    if any(path.is_relative_to(workspace) for path in system_roots):
        raise SandboxUnavailable(
            "Workspace must not contain Windows or the base interpreter installation",
            reason="unsupported_workspace",
        )
    if Path(sys.prefix).resolve() == workspace:
        raise SandboxUnavailable(
            "Workspace must not be the active interpreter installation",
            reason="unsupported_workspace",
        )
    home = Path(normalize_path(Path.home().resolve()))
    if (
        workspace == home
        or home.is_relative_to(workspace)
        or any(Path(normalize_path(root)).is_relative_to(workspace) for root in policy.private_paths)
    ):
        raise SandboxUnavailable(
            "Native Windows sandbox does not support using the home or private Virgo "
            "directory as the workspace. Switch to a project directory or No-Limit "
            "(--permission no-limit / /permissions no-limit).",
            reason="unsupported_workspace",
            target=workspace,
        )
    return resolved_cwd


_USER_HOME_TOOLCHAINS = frozenset({
    ".cargo", ".rustup", ".local", "scoop", "go", ".volta", ".nvm", ".fnm",
    ".bun", ".deno", ".pyenv", ".mise", ".asdf", "miniconda3", "anaconda3", "miniforge3",
})
_LOCAL_APPDATA_TOOLCHAINS = frozenset({
    "uv", "pnpm", "fnm", "nvm", "volta", "yarn", "go-build", "pypoetry", "pipx", "mise",
})
_ROAMING_APPDATA_TOOLCHAINS = frozenset({
    "npm", "uv", "nvm", "pypoetry", "pipx", "python",
})


def _collapse_user_toolchain_root(
    entry: Path,
    user_home: Path,
    local_appdata: Path | None,
    roaming_appdata: Path | None,
) -> Path | None:
    if local_appdata is not None and entry.is_relative_to(local_appdata) and entry != local_appdata:
        parts = entry.relative_to(local_appdata).parts
        first = parts[0].lower()
        if first == "programs" and len(parts) >= 2:
            return local_appdata / parts[0] / parts[1]
        if first in _LOCAL_APPDATA_TOOLCHAINS:
            return local_appdata / parts[0]
        if first == "microsoft" and len(parts) >= 2 and parts[1].lower() == "winget":
            return local_appdata / parts[0] / parts[1]
        return None
    if roaming_appdata is not None and entry.is_relative_to(roaming_appdata) and entry != roaming_appdata:
        parts = entry.relative_to(roaming_appdata).parts
        if parts[0].lower() in _ROAMING_APPDATA_TOOLCHAINS:
            return roaming_appdata / parts[0]
        return None
    if entry.is_relative_to(user_home) and entry != user_home:
        parts = entry.relative_to(user_home).parts
        if parts[0].lower() in _USER_HOME_TOOLCHAINS:
            return user_home / parts[0]
    return None


def _discover_optional_user_toolchains(
    policy,
    workspace: Path,
    env: dict[str, str],
) -> list[Path]:
    """Discover optional user-owned toolchains (workspace .venv base Python & PATH tools)."""
    raw_candidates: list[Path] = []

    for venv_name in (".venv", "venv"):
        cfg = workspace / venv_name / "pyvenv.cfg"
        try:
            if not cfg.is_file() or is_reparse_point(cfg):
                continue
            for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip().lower() == "home" and v.strip():
                        base_dir = Path(v.strip()).resolve(strict=True)
                        if base_dir.name.lower() in {"scripts", "bin"} and (base_dir.parent / "Lib").is_dir():
                            base_dir = base_dir.parent
                        raw_candidates.append(base_dir)
        except OSError:
            continue

    try:
        user_home = Path(normalize_path(Path.home().resolve()))
    except OSError:
        user_home = None
    local_env = env.get("LOCALAPPDATA") or os.environ.get("LOCALAPPDATA")
    roaming_env = env.get("APPDATA") or os.environ.get("APPDATA")
    local_appdata = Path(normalize_path(local_env)) if local_env else (
        user_home / "AppData" / "Local" if user_home else None
    )
    roaming_appdata = Path(normalize_path(roaming_env)) if roaming_env else (
        user_home / "AppData" / "Roaming" if user_home else None
    )

    if user_home is not None:
        path_value = env.get("PATH") or env.get("Path") or os.environ.get("PATH", "")
        for part in path_value.split(os.pathsep):
            cleaned = part.strip().strip('"')
            if not cleaned:
                continue
            try:
                resolved = Path(cleaned).resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_dir():
                continue
            collapsed_root = _collapse_user_toolchain_root(
                Path(normalize_path(resolved)), user_home, local_appdata, roaming_appdata,
            )
            if collapsed_root is not None:
                raw_candidates.append(collapsed_root)

    private_norm = tuple(Path(normalize_path(p)) for p in policy.private_paths)
    seen: set[str] = set()
    accepted: list[Path] = []
    for candidate in raw_candidates:
        try:
            verified = verify_local_ntfs(candidate, allow_reparse=True, expect_directory=True)
        except OSError:
            continue
        key = ntpath.normcase(str(verified))
        if key in seen:
            continue
        seen.add(key)
        if (
            verified.is_relative_to(WINDOWS_DIRECTORY)
            or verified.is_relative_to(workspace)
            or workspace.is_relative_to(verified)
            or any(verified.is_relative_to(priv) or priv.is_relative_to(verified) for priv in private_norm)
        ):
            continue
        if has_aap_read_access(verified, require_execute=True):
            continue
        if can_open_write_dac(verified):
            accepted.append(verified)

    accepted.sort(key=lambda p: len(p.parts))
    collapsed_final: list[Path] = []
    for root in accepted:
        if not any(root.is_relative_to(parent) for parent in collapsed_final):
            collapsed_final.append(root)
    return collapsed_final


def _discover_runtime_candidates(policy, executable: Path) -> list[Path]:
    """Collect runtime directories required by the broker Python and launched executable."""
    candidates: list[Path] = [Path(root) for root in policy.runtime_paths()]
    exe_resolved = executable.resolve(strict=True)
    exe_dir = exe_resolved.parent
    candidates.append(exe_dir)

    # Git for Windows: ...\Git\cmd\git.exe needs ...\Git (mingw64\bin, etc.)
    if exe_resolved.name.lower() == "git.exe" and exe_dir.name.lower() == "cmd":
        git_root = exe_dir.parent
        if (git_root / "mingw64").is_dir():
            candidates.append(git_root)

    # Python virtualenv launcher: ...\venv\Scripts\python.exe needs venv root + base home
    if exe_dir.name.lower() == "scripts" and (exe_dir.parent / "pyvenv.cfg").is_file():
        venv_root = exe_dir.parent
        candidates.append(venv_root)
        try:
            for line in (venv_root / "pyvenv.cfg").read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip().lower() == "home" and v.strip():
                        candidates.append(Path(v.strip()))
        except OSError:
            pass

    resolved_unique: list[Path] = []
    seen: set[str] = set()
    for root in candidates:
        try:
            resolved = root.resolve(strict=True)
        except OSError:
            continue
        key = ntpath.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            resolved_unique.append(resolved)

    # Collapse overlapping parent/child roots (keep outermost).
    resolved_unique.sort(key=lambda p: len(p.parts))
    collapsed: list[Path] = []
    for root in resolved_unique:
        if not any(root.is_relative_to(parent) for parent in collapsed):
            collapsed.append(root)
    return collapsed


def _check_runtime_accessibility(
    candidates: Sequence[Path],
    workspace: Path,
    executable: Path,
) -> list[Path]:
    """Return runtime roots that need a read-only session ACE, failing closed if inaccessible."""
    to_grant: list[Path] = []
    exe_resolved = executable.resolve(strict=True)
    for root in candidates:
        if root.is_relative_to(WINDOWS_DIRECTORY) or root.is_relative_to(workspace):
            continue
        root_has_aap = has_aap_read_access(root, require_execute=True)
        exe_needs_grant = exe_resolved.is_relative_to(root) and not has_aap_read_access(
            exe_resolved, require_execute=True
        )
        if root_has_aap and not exe_needs_grant:
            continue
        if can_open_write_dac(root):
            to_grant.append(root)
            continue
        raise SandboxUnavailable(
            f"The Windows AppContainer sandbox cannot access required runtime directory '{root}' "
            "(it lacks ALL APPLICATION PACKAGES read permission and the current user lacks WRITE_DAC). "
            "Install Python/tools in a user-owned directory (e.g. `uv python install` or "
            "%LOCALAPPDATA%\\Programs\\Python`) or standard C:\\Program Files, "
            "or run with No-Limit (--permission no-limit / /permissions no-limit).",
            reason="windows_runtime_inaccessible",
            dimension="windows_runtime_access",
            target=root,
        )
    return to_grant


def recover_stale_sessions(
    target_dir: str | Path | None = None,
    *,
    force_dead_only: bool = True,
) -> list[dict[str, Any]]:
    """Recover orphaned Windows AppContainer sessions left by crashed/killed processes.

    Idempotent: a second call after clean recovery returns [].
    """
    journal_dir = Path(os.path.abspath(os.fspath(target_dir))) if target_dir is not None else state_dir()
    if not journal_dir.exists():
        return []

    results: list[dict[str, Any]] = []
    for state_path in sorted(journal_dir.glob("*.json")):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("journal payload is not a JSON object")
            session_id = data.get("session_id", state_path.stem)
            profile_name = data.get("profile_name")
            sid_str = data.get("sid_str")
            raw_pid = data.get("owner_pid", -1)
            owner_pid = int(raw_pid) if raw_pid is not None else -1
            owner_create_time = data.get("owner_create_time")
            dos_drive = data.get("dos_drive")
            dos_target = data.get("dos_target")
            granted_roots = data.get("granted_roots", [])
            denied_paths = data.get("denied_paths", [])
            if not isinstance(granted_roots, list) or not isinstance(denied_paths, list):
                raise ValueError("journal root lists are malformed")
        except Exception as exc:
            results.append({
                "state_file": str(state_path),
                "status": "error",
                "errors": [f"failed to read journal: {exc}"],
            })
            continue

        alive = is_process_alive(owner_pid, owner_create_time)
        if force_dead_only and alive:
            results.append({
                "session_id": session_id,
                "profile_name": profile_name,
                "sid_str": sid_str,
                "owner_pid": owner_pid,
                "owner_alive": True,
                "status": "skipped_alive",
                "errors": [],
            })
            continue

        errors: list[str] = []
        revoked_roots: list[str] = []
        revoked_denies: list[str] = []
        drive_unmapped = False
        profile_deleted = False
        state_removed = False

        if sid_str:
            # Fail-closed order: revoke ALLOW roots first, then restore protected read-only paths.
            for entry in granted_roots:
                root_path = entry["path"] if isinstance(entry, dict) else str(entry)
                try:
                    revoke_sid_from_root(root_path, sid_str, ignore_missing=True)
                    revoked_roots.append(root_path)
                except OSError as exc:
                    errors.append(f"revoke root {root_path}: {exc}")

            for entry in denied_paths:
                if isinstance(entry, dict):
                    target_path = str(entry["path"])
                    restore_unprot = not entry.get("was_protected", False)
                    orig_b64 = entry.get("original_explicit_aces_b64", [])
                else:
                    target_path = str(entry)
                    restore_unprot = False
                    orig_b64 = []
                try:
                    revoke_sid_from_root(
                        target_path,
                        sid_str,
                        ignore_missing=True,
                        restore_unprotected=restore_unprot,
                        original_explicit_aces_b64=orig_b64,
                    )
                    revoked_denies.append(target_path)
                except OSError as exc:
                    errors.append(f"restore protected {target_path}: {exc}")

        if dos_drive:
            try:
                _remove_dos_drive(dos_drive, dos_target)
                drive_unmapped = True
            except OSError as exc:
                errors.append(f"unmap drive {dos_drive}: {exc}")

        if profile_name:
            try:
                profile_deleted = _delete_profile(profile_name, ignore_missing=True)
            except Exception as exc:
                errors.append(f"delete profile {profile_name}: {exc}")

        if not errors:
            try:
                state_path.unlink(missing_ok=True)
                state_removed = True
            except OSError as exc:
                errors.append(f"remove state file {state_path}: {exc}")

        results.append({
            "session_id": session_id,
            "profile_name": profile_name,
            "sid_str": sid_str,
            "owner_pid": owner_pid,
            "owner_alive": alive,
            "status": "recovered" if not errors else "error",
            "revoked_roots": revoked_roots,
            "revoked_denies": revoked_denies,
            "drive_unmapped": drive_unmapped,
            "profile_deleted": profile_deleted,
            "state_file_removed": state_removed,
            "errors": errors,
        })
    return results


_STARTUP_RECOVERY_LOCK = threading.Lock()
_STARTUP_RECOVERY_DONE = False


def _run_startup_recovery_once() -> None:
    global _STARTUP_RECOVERY_DONE
    with _STARTUP_RECOVERY_LOCK:
        if _STARTUP_RECOVERY_DONE:
            return
        _STARTUP_RECOVERY_DONE = True
    try:
        recover_stale_sessions()
    except Exception:
        # Best-effort recovery of previous crashed sessions must never prevent starting a new session.
        pass


class WindowsSandboxSession:
    """Session-scoped Standard AppContainer SID, inheritable root ACLs, and journal."""

    def __init__(
        self,
        workspace: Path,
        *,
        read_only: bool,
        network_allowed: bool,
        dos_drive: str | None = None,
        dos_target: str | None = None,
        journal_dir: Path | None = None,
    ) -> None:
        self.workspace = workspace
        self.read_only = bool(read_only)
        self.network_allowed = bool(network_allowed)
        self.dos_drive = dos_drive
        self.dos_target = dos_target
        self.desktop = _resolve_desktop()
        self.session_id = uuid.uuid4().hex[:16]
        self.profile_name = f"VirgoSession.{self.session_id}"
        self.capabilities = WORKSPACE_EDIT_CAPABILITIES if self.network_allowed else READ_ONLY_CAPABILITIES
        self._state_dir = journal_dir or state_dir()
        self.state_file = self._state_dir / f"{self.session_id}.json"
        self.created_at = datetime.now(UTC).isoformat()

        self._lock = threading.RLock()
        self._active_commands = 0
        self._closed = False
        self._granted_roots: dict[str, dict[str, Any]] = {}
        # Maps normalized path_key -> [read_refs, write_refs]
        self._command_grant_refs: dict[str, list[int]] = {}
        self._command_grant_paths: dict[str, Path] = {}
        self._protected_records: dict[str, dict[str, Any]] = {}

        self._capability_bytes: tuple[bytes, ...] = tuple(
            _derive_capability(name) for name in self.capabilities
        )
        self.package_name, self.package_sid, self.profile_path = _create_profile(
            self.profile_name, self._capability_bytes
        )
        self.home = self.profile_path / "Home"
        self.appdata = self.home / "AppData" / "Roaming"
        self.temp_root = self.profile_path / "Temp"
        ws_key = ntpath.normcase(str(self.workspace))
        try:
            for directory in (self.home, self.home / "AppData", self.appdata, self.temp_root):
                directory.mkdir(parents=True, exist_ok=True)
            # Write-ahead workspace root entry before applying ACLs.
            self._granted_roots[ws_key] = {
                "path": str(self.workspace),
                "writable": not self.read_only,
                "kind": "workspace",
            }
            self._persist_journal()
            grant_root_access(self.workspace, self.package_name, writable=not self.read_only)
        except BaseException:
            try:
                self.close(unmap_drive=False)
            except Exception:
                pass
            raise

    def _persist_journal(self) -> None:
        all_roots = list(self._granted_roots.values())
        for key, (r_refs, w_refs) in self._command_grant_refs.items():
            if (r_refs > 0 or w_refs > 0) and key not in self._granted_roots:
                all_roots.append({
                    "path": str(self._command_grant_paths[key]),
                    "writable": w_refs > 0,
                    "kind": "command_grant",
                })
        payload = {
            "session_id": self.session_id,
            "profile_name": self.profile_name,
            "sid_str": self.package_name,
            "owner_pid": os.getpid(),
            "owner_create_time": _current_process_create_time(),
            "created_at": self.created_at,
            "dos_drive": self.dos_drive,
            "dos_target": self.dos_target,
            "granted_roots": all_roots,
            "denied_paths": list(self._protected_records.values()),
            "capabilities": list(self.capabilities),
        }
        _write_json_atomic(self.state_file, payload)

    def ensure_runtime_roots(self, roots: Sequence[Path]) -> None:
        with self._lock:
            if self._closed:
                raise SandboxUnavailable("Windows AppContainer session is already closed")
            added: list[tuple[str, Path]] = []
            for root in roots:
                key = ntpath.normcase(str(root))
                if key in self._granted_roots:
                    continue
                self._granted_roots[key] = {
                    "path": str(root),
                    "writable": False,
                    "kind": "runtime",
                }
                added.append((key, root))
            if not added:
                return
            self._persist_journal()
            applied: set[str] = set()
            try:
                for key, root in added:
                    grant_root_access(root, self.package_name, writable=False)
                    applied.add(key)
            except BaseException:
                for key, _ in added:
                    if key not in applied:
                        self._granted_roots.pop(key, None)
                try:
                    self._persist_journal()
                except OSError:
                    pass
                raise

    def _lookup_peer_protected_snapshot(self, target_key: str) -> dict[str, Any] | None:
        """Look up pre-Virgo DACL snapshot from a peer session journal protecting ``target_key``."""
        if not self._state_dir.exists():
            return None
        for state_path in self._state_dir.glob("*.json"):
            if state_path == self.state_file:
                continue
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                for rec in data.get("denied_paths", []):
                    if isinstance(rec, dict) and ntpath.normcase(str(rec.get("path", ""))) == target_key:
                        return {
                            "was_protected": bool(rec.get("was_protected", False)),
                            "original_explicit_aces_b64": list(rec.get("original_explicit_aces_b64", [])),
                        }
            except Exception:
                continue
        return None

    def _validate_protected_target(self, target: Path) -> Path:
        """Verify a protected path inside workspace contains zero reparse points."""
        current = target
        while True:
            if is_reparse_point(current):
                raise SandboxUnavailable(
                    f"Protected Windows sandbox path must not be a reparse point: {current}",
                    reason="unsupported_path",
                    target=current,
                )
            if current == self.workspace:
                break
            parent = current.parent
            if parent == current:
                break
            current = parent
        try:
            return verify_local_ntfs(target, allow_reparse=False)
        except OSError as error:
            raise SandboxUnavailable(
                f"Protected Windows sandbox path is invalid: {target} ({error})",
                reason="unsupported_path",
                target=target,
            ) from error

    def ensure_protected_paths(self, paths: Sequence[Path]) -> None:
        """Ensure existing protected targets under workspace are read-only and non-reparse.

        - In writable sessions: enforces SE_DACL_PROTECTED + ALLOW(RX) so writes,
          deletes, and renames are blocked while reads and host access succeed.
          If a previously protected target is later removed from ``paths`` (e.g.
          after explicit user approval on .virgo), restores inherited workspace access.
        - In read-only sessions: rejects reparse points on protected names, and
          if a concurrent busy writable sibling has set SE_DACL_PROTECTED on a
          target, explicitly grants RX for this read-only SID (recorded in
          _granted_roots so teardown/recovery revokes it cleanly).
        """
        candidates: list[Path] = list(paths)

        with self._lock:
            if self._closed:
                raise SandboxUnavailable("Windows AppContainer session is already closed")
            seen: set[str] = set()
            to_protect: list[Path] = []
            to_grant_ro: list[tuple[str, Path]] = []
            to_unprotect: list[dict[str, Any]] = []
            dirty = False

            for raw_target in candidates:
                if not raw_target.is_relative_to(self.workspace) or raw_target == self.workspace:
                    continue
                if not os.path.lexists(raw_target):
                    continue
                target = self._validate_protected_target(raw_target)
                key = ntpath.normcase(str(target))
                if key in seen:
                    continue
                seen.add(key)

                if self.read_only:
                    if is_dacl_protected(target) and key not in self._granted_roots:
                        self._granted_roots[key] = {
                            "path": str(target),
                            "writable": False,
                            "kind": "protected_ro_grant",
                        }
                        to_grant_ro.append((key, target))
                        dirty = True
                    continue

                if key in self._protected_records:
                    # Re-apply if host replaced the file/dir since initial snapshot.
                    if not is_protected_readonly_for_sid(target, self.package_name):
                        to_protect.append(target)
                    continue
                snap = capture_dacl_snapshot(target, exclude_sid_str=self.package_name)
                if snap.get("has_other_appcontainer_sid"):
                    peer = self._lookup_peer_protected_snapshot(key)
                    if peer is not None:
                        was_protected = peer["was_protected"]
                        orig_explicit = peer["original_explicit_aces_b64"]
                    else:
                        # Another AppContainer session protected this path; avoid duplicating
                        # its converted host ACEs as "original explicit ACEs" on final restore.
                        was_protected = False
                        orig_explicit = []
                else:
                    was_protected = snap["was_protected"]
                    orig_explicit = snap["original_explicit_aces_b64"]
                self._protected_records[key] = {
                    "path": str(target),
                    "was_protected": was_protected,
                    "original_explicit_aces_b64": orig_explicit,
                }
                to_protect.append(target)
                dirty = True

            if not self.read_only:
                for key, rec in list(self._protected_records.items()):
                    if key not in seen:
                        to_unprotect.append(rec)
                        self._protected_records.pop(key, None)
                        dirty = True

            if dirty:
                self._persist_journal()
            applied_ro: set[str] = set()
            try:
                for key, target in to_grant_ro:
                    grant_root_access(target, self.package_name, writable=False)
                    applied_ro.add(key)
            except BaseException:
                for key, _ in to_grant_ro:
                    if key not in applied_ro:
                        self._granted_roots.pop(key, None)
                try:
                    self._persist_journal()
                except OSError:
                    pass
                raise
            for rec in to_unprotect:
                revoke_sid_from_root(
                    rec["path"],
                    self.package_name,
                    ignore_missing=True,
                    restore_unprotected=not rec.get("was_protected", False),
                    original_explicit_aces_b64=rec.get("original_explicit_aces_b64", []),
                )
            for target in to_protect:
                protect_readonly_path(target, self.package_name)

    def _reconcile_path_acl(self, key: str, path: Path) -> None:
        """Apply the exact required ACL for ``key`` given current refcounts and base state."""
        r_refs, w_refs = self._command_grant_refs.get(key, [0, 0])
        if w_refs > 0:
            grant_root_access(path, self.package_name, writable=True)
            return
        if r_refs > 0:
            if key in self._protected_records:
                protect_readonly_path(path, self.package_name)
            elif key in self._granted_roots and self._granted_roots[key]["writable"]:
                grant_root_access(path, self.package_name, writable=True)
            else:
                grant_root_access(path, self.package_name, writable=False)
            return
        # Both refcounts reached 0: restore pre-command baseline for this path.
        if key in self._protected_records:
            protect_readonly_path(path, self.package_name)
        elif key in self._granted_roots:
            grant_root_access(
                path,
                self.package_name,
                writable=bool(self._granted_roots[key]["writable"]),
            )
        else:
            revoke_sid_from_root(path, self.package_name, ignore_missing=True)

    def acquire_command(
        self, command_grants: Sequence[tuple[Path, bool]]
    ) -> list[tuple[Path, bool]]:
        """Increment active command count and apply refcounted one-shot path grants."""
        with self._lock:
            if self._closed:
                raise SandboxUnavailable("Windows AppContainer session is already closed")
            acquired: list[tuple[Path, bool]] = []
            touched: list[tuple[str, Path]] = []
            try:
                for raw_path, writable in command_grants:
                    validated = verify_local_ntfs(raw_path, allow_reparse=True)
                    if not can_open_write_dac(validated):
                        raise SandboxUnavailable(
                            f"Cannot grant Windows AppContainer access to '{validated}' "
                            "(current user lacks WRITE_DAC).",
                            reason="unsupported_path",
                            target=validated,
                        )
                    key = ntpath.normcase(str(validated))
                    refs = self._command_grant_refs.setdefault(key, [0, 0])
                    if writable:
                        refs[1] += 1
                    else:
                        refs[0] += 1
                    self._command_grant_paths[key] = validated
                    acquired.append((validated, bool(writable)))
                    touched.append((key, validated))
                if touched:
                    self._persist_journal()
                    seen_keys: set[str] = set()
                    for key, path in touched:
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        self._reconcile_path_acl(key, path)
                self._active_commands += 1
                return acquired
            except BaseException:
                rollback_keys: list[tuple[str, Path]] = []
                for path, writable in reversed(acquired):
                    key = ntpath.normcase(str(path))
                    refs = self._command_grant_refs.get(key)
                    if refs is not None:
                        if writable:
                            refs[1] = max(0, refs[1] - 1)
                        else:
                            refs[0] = max(0, refs[0] - 1)
                        if refs[0] == 0 and refs[1] == 0:
                            self._command_grant_refs.pop(key, None)
                            self._command_grant_paths.pop(key, None)
                    rollback_keys.append((key, path))
                seen_rb: set[str] = set()
                for key, path in rollback_keys:
                    if key in seen_rb:
                        continue
                    seen_rb.add(key)
                    try:
                        self._reconcile_path_acl(key, path)
                    except OSError:
                        pass
                try:
                    self._persist_journal()
                except OSError:
                    pass
                raise

    def release_command(self, command_grants: Sequence[tuple[Path, bool]]) -> None:
        """Reconcile/revoke refcounted one-shot command grants and decrement count."""
        errors: list[str] = []
        with self._lock:
            self._active_commands = max(0, self._active_commands - 1)
            reconcile_targets: list[tuple[str, Path]] = []
            for path, writable in command_grants:
                key = ntpath.normcase(str(path))
                refs = self._command_grant_refs.get(key)
                if refs is not None:
                    if writable:
                        refs[1] = max(0, refs[1] - 1)
                    else:
                        refs[0] = max(0, refs[0] - 1)
                    if refs[0] == 0 and refs[1] == 0:
                        self._command_grant_refs.pop(key, None)
                        self._command_grant_paths.pop(key, None)
                reconcile_targets.append((key, path))
            seen: set[str] = set()
            for key, path in reconcile_targets:
                if key in seen:
                    continue
                seen.add(key)
                try:
                    self._reconcile_path_acl(key, path)
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
            if reconcile_targets and not self._closed:
                try:
                    self._persist_journal()
                except OSError as exc:
                    errors.append(f"journal update: {exc}")
        if errors:
            raise SandboxProcessError(
                "Could not revoke Windows command grants: " + "; ".join(errors)
            )

    def build_environment(
        self,
        source: dict[str, str],
        explicit: dict[str, str],
        command_scratch: Path,
    ) -> dict[str, str]:
        filtered = filter_sandbox_environment(source)
        result = {key.upper(): value for key, value in filtered.items()}

        # Derive base LocalAppData (<base>\Packages\<pkg>\AC -> <base>) so Windows'
        # BasepAppContainerEnvironmentExtension maps child LOCALAPPDATA -> <AC>
        # and TEMP/TMP -> <AC>\Temp cleanly without double-nesting or WinError 203.
        if (
            self.profile_path.name.lower() == "ac"
            and self.profile_path.parent.parent.name.lower() == "packages"
        ):
            base_localappdata = str(self.profile_path.parent.parent.parent)
        else:
            base_localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")

        cache_root = self.home / "Cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        result.update(
            XDG_CACHE_HOME=str(cache_root),
            UV_CACHE_DIR=str(cache_root / "uv"),
            PIP_CACHE_DIR=str(cache_root / "pip"),
            NPM_CONFIG_CACHE=str(cache_root / "npm"),
        )
        result.update({key.upper(): value for key, value in explicit.items()})
        result.update(
            SYSTEMROOT=str(WINDOWS_DIRECTORY),
            WINDIR=str(WINDOWS_DIRECTORY),
            COMSPEC=str(SYSTEM_DIRECTORY / "cmd.exe"),
            PATHEXT=".COM;.EXE;.BAT;.CMD",
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            HOMEDRIVE=self.home.drive,
            HOMEPATH=str(self.home)[len(self.home.drive) :],
            APPDATA=str(self.appdata),
            LOCALAPPDATA=base_localappdata,
            TEMP=str(self.temp_root),
            TMP=str(self.temp_root),
            TMPDIR=str(command_scratch),
            VIRGO="1",
            POWERSHELL_TELEMETRY_OPTOUT="1",
            POWERSHELL_UPDATECHECK="Off",
            DOTNET_CLI_TELEMETRY_OPTOUT="1",
        )
        return result

    def close(self, *, unmap_drive: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            if self._active_commands > 0:
                raise SandboxProcessError(
                    f"Cannot tear down Windows AppContainer session with {self._active_commands} active command(s)"
                )
            errors: list[str] = []

            # Fail-closed order: revoke all ALLOW roots first, then restore protected read-only paths.
            for key, path in list(self._command_grant_paths.items()):
                if key not in self._granted_roots:
                    try:
                        revoke_sid_from_root(path, self.package_name, ignore_missing=True)
                    except OSError as exc:
                        errors.append(f"revoke command grant {path}: {exc}")
            self._command_grant_refs.clear()
            self._command_grant_paths.clear()

            for entry in list(self._granted_roots.values()):
                root_path = entry["path"]
                try:
                    revoke_sid_from_root(root_path, self.package_name, ignore_missing=True)
                except OSError as exc:
                    errors.append(f"revoke root {root_path}: {exc}")

            for rec in list(self._protected_records.values()):
                target_path = rec["path"]
                try:
                    revoke_sid_from_root(
                        target_path,
                        self.package_name,
                        ignore_missing=True,
                        restore_unprotected=not rec.get("was_protected", False),
                        original_explicit_aces_b64=rec.get("original_explicit_aces_b64", []),
                    )
                except OSError as exc:
                    errors.append(f"restore protected {target_path}: {exc}")

            if unmap_drive and self.dos_drive:
                try:
                    _remove_dos_drive(self.dos_drive, self.dos_target)
                except OSError as exc:
                    errors.append(f"unmap drive {self.dos_drive}: {exc}")

            try:
                _delete_profile(self.profile_name, ignore_missing=True)
            except Exception as exc:
                errors.append(str(exc))

            if not errors:
                try:
                    self.state_file.unlink(missing_ok=True)
                except OSError as exc:
                    errors.append(f"remove journal {self.state_file}: {exc}")

            self._closed = True
            if errors:
                raise SandboxProcessError(
                    "Windows AppContainer session teardown failed: " + "; ".join(errors[:5])
                )


class _CommandEnvelope:
    """Lightweight per-command adapter passed to WindowsProcess and NativeProcess."""

    token = None  # Signals WindowsProcess to use CreateProcessW (no CreateProcessAsUserW).

    def __init__(
        self,
        session: WindowsSandboxSession,
        policy,
        argv: Sequence[str],
        cwd: Path,
        env: dict[str, str],
        cancellation,
        *,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self.session = session
        self.policy = policy
        self.cancellation = cancellation
        self.desktop = session.desktop
        self.process = None
        self._closed = False
        self._command_grants: list[tuple[Path, bool]] = []
        self._acquired = False

        # Keep ctypes buffers referenced for the lifetime of CreateProcessW attribute consumption.
        self._sid_buf = ctypes.create_string_buffer(session.package_sid)
        self._cap_bufs = [ctypes.create_string_buffer(raw) for raw in session._capability_bytes]
        if self._cap_bufs:
            self._cap_array = (SidAndAttributes * len(self._cap_bufs))(
                *(
                    SidAndAttributes(ctypes.cast(buf, PSID), SE_GROUP_ENABLED)
                    for buf in self._cap_bufs
                )
            )
            self._cap_ptr = ctypes.cast(self._cap_array, ctypes.POINTER(SidAndAttributes))
        else:
            self._cap_array = None
            self._cap_ptr = None

        self.command_scratch = session.temp_root / f"cmd-{uuid.uuid4().hex[:12]}"
        self.command_scratch.mkdir(parents=True, exist_ok=True)
        try:
            self._check_cancelled()
            self._command_grants = session.acquire_command(policy.grants)
            self._acquired = True
            self.cwd = _map_cwd_to_drive(cwd, session.workspace, session.dos_drive)
            self.environment = session.build_environment(
                env, env_overrides or {}, self.command_scratch
            )
            self._check_cancelled()
        except BaseException:
            self.cleanup()
            raise

    def _check_cancelled(self) -> None:
        if self.cancellation is not None and self.cancellation.is_set():
            raise SandboxCancelled("Windows sandbox launch cancelled before payload release")

    def process_attributes(self) -> list[tuple[int, Any]]:
        capabilities = SecurityCapabilities(
            ctypes.cast(self._sid_buf, PSID),
            self._cap_ptr,
            len(self._cap_bufs),
            0,
        )
        # Standard AppContainer ONLY — keeps ALL APPLICATION PACKAGES (no 0x2000F LPAC opt-out).
        return [(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, capabilities)]

    def cleanup(self) -> None:
        if self._closed:
            return
        if self.process is not None and not self.process.tree_drained:
            raise SandboxProcessError("Windows sandbox resources require verified process-tree drain")
        errors: list[str] = []
        try:
            shutil.rmtree(self.command_scratch, ignore_errors=True)
        except OSError as exc:
            errors.append(f"scratch cleanup: {exc}")
        if self._acquired:
            try:
                self.session.release_command(self._command_grants)
            except Exception as exc:
                errors.append(str(exc))
            self._acquired = False
        self._closed = True
        self.process = None
        if errors:
            raise SandboxProcessError(
                "Could not release Windows command sandbox resources: " + "; ".join(errors)
            )


class WindowsSessionPool:
    """Owns active session-level Standard AppContainer leases for one executor."""

    def __init__(self, *, journal_dir: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, bool, bool], WindowsSandboxSession] = {}
        self._workspace_drives: dict[str, tuple[str, str]] = {}
        self._journal_dir = journal_dir
        self._closed = False
        _LIVE_POOLS.add(self)

    def get_or_create(
        self,
        workspace: Path,
        *,
        read_only: bool,
        network_allowed: bool,
    ) -> WindowsSandboxSession:
        ws_key = ntpath.normcase(str(workspace))
        session_key = (ws_key, bool(read_only), bool(network_allowed))
        with self._lock:
            if self._closed:
                self._closed = False
            existing = self._sessions.get(session_key)
            if existing is not None and not existing._closed:
                return existing

            # Close idle sibling-mode sessions on the same workspace first so
            # SE_DACL_PROTECTED flags from a prior writable session are restored
            # before a new read-only session grants workspace inheritance.
            for sib_key, sibling in list(self._sessions.items()):
                if sib_key[0] == ws_key and sib_key != session_key:
                    with sibling._lock:
                        if sibling._active_commands == 0:
                            sibling.close(unmap_drive=False)
                            if sibling._closed:
                                self._sessions.pop(sib_key, None)

            drive_info = self._workspace_drives.get(ws_key)
            if drive_info is None and ancestor_lacks_aap(workspace):
                drive_info = _allocate_dos_drive(workspace)
                if drive_info is not None:
                    self._workspace_drives[ws_key] = drive_info

            dos_drive, dos_target = drive_info if drive_info is not None else (None, None)
            try:
                session = WindowsSandboxSession(
                    workspace,
                    read_only=read_only,
                    network_allowed=network_allowed,
                    dos_drive=dos_drive,
                    dos_target=dos_target,
                    journal_dir=self._journal_dir,
                )
            except BaseException:
                if drive_info is not None and not any(k[0] == ws_key for k in self._sessions):
                    self._workspace_drives.pop(ws_key, None)
                    _remove_dos_drive(*drive_info)
                raise
            self._sessions[session_key] = session
            return session

    def close_idle(self) -> None:
        """Close idle sessions and unmap workspace drives that have no remaining active sessions."""
        with self._lock:
            errors: list[str] = []
            for key, session in list(self._sessions.items()):
                with session._lock:
                    if session._active_commands > 0:
                        continue
                    try:
                        session.close(unmap_drive=False)
                    except Exception as exc:
                        errors.append(str(exc))
                    if session._closed:
                        self._sessions.pop(key, None)

            active_workspaces = {key[0] for key in self._sessions}
            for ws_key, (drive, target) in list(self._workspace_drives.items()):
                if ws_key in active_workspaces:
                    continue
                try:
                    _remove_dos_drive(drive, target)
                except OSError as exc:
                    errors.append(f"unmap drive {drive}: {exc}")
                else:
                    self._workspace_drives.pop(ws_key, None)
            if errors:
                raise SandboxProcessError(
                    "Windows sandbox idle teardown encountered errors: " + "; ".join(errors[:5])
                )

    def close(self) -> None:
        with self._lock:
            if self._closed and not self._sessions and not self._workspace_drives:
                return
            errors: list[str] = []
            for key, session in list(self._sessions.items()):
                try:
                    session.close(unmap_drive=False)
                except Exception as exc:
                    errors.append(str(exc))
                if session._closed:
                    self._sessions.pop(key, None)

            active_workspaces = {key[0] for key in self._sessions}
            for ws_key, (drive, target) in list(self._workspace_drives.items()):
                if ws_key in active_workspaces:
                    continue
                try:
                    _remove_dos_drive(drive, target)
                except OSError as exc:
                    errors.append(f"unmap drive {drive}: {exc}")
                else:
                    self._workspace_drives.pop(ws_key, None)
            if not self._sessions and not self._workspace_drives:
                self._closed = True
            if errors:
                raise SandboxProcessError(
                    "Windows sandbox pool teardown encountered errors: " + "; ".join(errors[:5])
                )


_LIVE_POOLS: weakref.WeakSet[WindowsSessionPool] = weakref.WeakSet()
_DEFAULT_POOL_LOCK = threading.Lock()
_DEFAULT_POOL: WindowsSessionPool | None = None


def _get_default_pool() -> WindowsSessionPool:
    global _DEFAULT_POOL
    with _DEFAULT_POOL_LOCK:
        if _DEFAULT_POOL is None:
            _DEFAULT_POOL = WindowsSessionPool()
        return _DEFAULT_POOL


def _atexit_cleanup() -> None:
    for pool in list(_LIVE_POOLS):
        try:
            pool.close()
        except Exception:
            pass


atexit.register(_atexit_cleanup)


def spawn(
    policy,
    argv: list[str] | tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
    stdout,
    cancellation,
    *,
    stdin=subprocess.DEVNULL,
    stderr=subprocess.STDOUT,
    env_overrides: dict[str, str] | None = None,
    session_pool: WindowsSessionPool | None = None,
):
    """Launch a command inside a session-scoped Standard AppContainer via WindowsProcess."""
    from ..tools.windows_process import WindowsProcess

    envelope = None
    proc = None
    try:
        if cancellation is not None and cancellation.is_set():
            raise SandboxCancelled("Windows sandbox setup cancelled before payload release")
        precheck_windows_sandbox()
        _run_startup_recovery_once()

        validated_cwd = validate_policy(policy, cwd)
        if not argv:
            raise SandboxUnavailable("Windows sandbox execution requires an explicit native .exe path")
        executable = Path(normalize_path(argv[0]))
        if executable.suffix.lower() != ".exe" or not executable.is_file():
            raise SandboxUnavailable("Windows sandbox execution requires an explicit native .exe path")

        workspace = verify_local_ntfs(policy.workspace, allow_reparse=True, expect_directory=True)
        runtime_candidates = _discover_runtime_candidates(policy, executable)
        runtime_to_grant = _check_runtime_accessibility(runtime_candidates, workspace, executable)
        optional_toolchains = _discover_optional_user_toolchains(policy, workspace, env)

        pool = session_pool if session_pool is not None else _get_default_pool()
        session = pool.get_or_create(
            workspace,
            read_only=policy.read_only,
            network_allowed=policy.network_allowed,
        )
        session.ensure_runtime_roots([*runtime_to_grant, *optional_toolchains])
        session.ensure_protected_paths(policy.all_write_denies())

        envelope = _CommandEnvelope(
            session,
            policy,
            argv,
            validated_cwd,
            env,
            cancellation,
            env_overrides=env_overrides,
        )
        proc = WindowsProcess(
            argv,
            cwd=envelope.cwd,
            env=envelope.environment,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            sandbox=envelope,
        )
        proc._virgo_sandbox_tmp = envelope
        proc._virgo_sandbox = {
            **policy.describe(),
            "backend": BACKEND_NAME,
            "filesystem": "standard-appcontainer-session-acl",
            "appcontainer_sid": session.package_name,
            "dos_drive": session.dos_drive,
        }
        return proc
    except BaseException as error:
        proc = proc or getattr(error, "process", None) or (envelope.process if envelope is not None else None)
        if envelope is not None:
            if proc is not None:
                proc._virgo_sandbox_tmp = envelope
            cleanup_errors: list[str] = []
            if proc is not None:
                try:
                    proc.cleanup_tree()
                except BaseException as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
            try:
                envelope.cleanup()
            except BaseException as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
            if cleanup_errors:
                failure = SandboxProcessError(
                    f"Windows sandbox launch failed ({error}); cleanup failed ({'; '.join(cleanup_errors)})"
                )
                failure.process, failure.envelope = proc, envelope
                raise failure from error
        if proc is not None and getattr(proc, "_released", False):
            if isinstance(error, (SandboxCancelled, SandboxProcessError)):
                raise
            raise SandboxProcessError(
                f"Windows sandbox process started but launch reporting failed: {error}"
            ) from error
        if isinstance(error, (OSError, AttributeError)) and not isinstance(error, SandboxCancelled):
            raise SandboxUnavailable(f"Could not establish the Windows sandbox: {error}") from error
        raise
