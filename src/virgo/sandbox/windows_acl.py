"""Session-level Windows NTFS ACL helpers using single-call OS inheritance.

Uses GetNamedSecurityInfoW / SetNamedSecurityInfoW (SE_FILE_OBJECT,
DACL_SECURITY_INFORMATION) with an explicit canonical binary ACL builder.
There is no per-file Python directory walk, no hardlink scanner, and no
retained per-file handle table.

Key Windows kernel invariants enforced here:
- Workspace writable grants omit FILE_DELETE_CHILD (0x40) and apply DELETE
  (0x10000) inherit-only so the workspace root cannot be deleted and protected
  children cannot be deleted/renamed via parent directory rights.
- Protected control metadata (.git, .virgo, AGENTS.md, CLAUDE.md, read-only
  subpaths) uses SE_DACL_PROTECTED with explicit non-session host ACEs
  preserved and session SID restricted to READ_EXECUTE (0x001200A9), because
  Windows' AppContainer access check ignores ACCESS_DENIED_ACE targeting
  AppContainer SIDs (S-1-15-2-*).
"""
from __future__ import annotations

import base64
import ctypes
import errno
import os
import stat
import struct
import sys
import threading
from contextlib import contextmanager
from ctypes import wintypes as w
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .._windows_probe import NULL_DEVICE_REQUIRED_ACCESS, null_device_allows_appcontainer

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("virgo.sandbox.windows_acl requires Windows")

__all__ = [
    "ALL_APPLICATION_PACKAGES_SID",
    "NULL_DEVICE_REQUIRED_ACCESS",
    "acl_state_lock",
    "ancestor_lacks_aap",
    "can_open_write_dac",
    "capture_dacl_snapshot",
    "grant_root_access",
    "has_aap_read_access",
    "inspect_dacl",
    "is_appcontainer_package_sid",
    "is_dacl_protected",
    "is_protected_readonly_for_sid",
    "null_device_allows_appcontainer",
    "protect_readonly_path",
    "revoke_sid_from_root",
    "scan_residual_aces",
]

PSID = ctypes.c_void_p
PACL = ctypes.c_void_p
PSECURITY_DESCRIPTOR = ctypes.c_void_p
HANDLE = ctypes.c_void_p
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
BOOL = ctypes.c_int32

ALL_APPLICATION_PACKAGES_SID = "S-1-15-2-1"

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000

SE_DACL_PROTECTED = 0x1000

# ACE types
ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
_DENY_ACE_TYPES = frozenset({1, 6, 10, 12})
_STANDARD_ACE_TYPES = frozenset({0, 1, 2, 3, 9, 10, 13, 14})
_OBJECT_ACE_TYPES = frozenset({5, 6, 7, 8, 11, 12, 15, 16})

_ACE_TYPE_NAMES = {
    0: "ALLOW",
    1: "DENY",
    2: "AUDIT",
    3: "ALARM",
    5: "ALLOW_OBJECT",
    6: "DENY_OBJECT",
    9: "ALLOW_CALLBACK",
    10: "DENY_CALLBACK",
    11: "ALLOW_CALLBACK_OBJECT",
    12: "DENY_CALLBACK_OBJECT",
}

# ACE flags
OBJECT_INHERIT_ACE = 0x01
CONTAINER_INHERIT_ACE = 0x02
NO_PROPAGATE_INHERIT_ACE = 0x04
INHERIT_ONLY_ACE = 0x08
INHERITED_ACE = 0x10

_ACE_FLAG_NAMES = (
    (OBJECT_INHERIT_ACE, "OI"),
    (CONTAINER_INHERIT_ACE, "CI"),
    (NO_PROPAGATE_INHERIT_ACE, "NP"),
    (INHERIT_ONLY_ACE, "IO"),
    (INHERITED_ACE, "I"),
)

# NTFS file access masks
FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_READ_EA = 0x00000008
FILE_WRITE_EA = 0x00000010
FILE_EXECUTE = 0x00000020
FILE_DELETE_CHILD = 0x00000040
FILE_READ_ATTRIBUTES = 0x00000080
FILE_WRITE_ATTRIBUTES = 0x00000100
DELETE = 0x00010000
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
SYNCHRONIZE = 0x00100000
GENERIC_ALL = 0x10000000
GENERIC_EXECUTE = 0x20000000
GENERIC_READ = 0x80000000

FILE_GENERIC_READ = (
    READ_CONTROL | FILE_READ_DATA | FILE_READ_ATTRIBUTES | FILE_READ_EA | SYNCHRONIZE
)  # 0x00120089
FILE_GENERIC_EXECUTE = (
    READ_CONTROL | FILE_READ_ATTRIBUTES | FILE_EXECUTE | SYNCHRONIZE
)  # 0x001200A0
READ_EXECUTE_MASK = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE  # 0x001200A9

_WRITE_SPECIFIC_BITS = (
    FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_EA | FILE_WRITE_ATTRIBUTES
)  # 0x00000116

# Writable directory root mask: no DELETE on root itself, NEVER FILE_DELETE_CHILD (0x40).
WRITE_NO_DELETE_MASK = READ_EXECUTE_MASK | _WRITE_SPECIFIC_BITS  # 0x001201BF
# Standard Modify without FILE_DELETE_CHILD (0x40), WRITE_DAC, WRITE_OWNER.
MODIFY_NO_DELETE_CHILD_MASK = WRITE_NO_DELETE_MASK | DELETE  # 0x001301BF

_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_ACL_STATE_MUTEX_NAME = "Local\\VirgoAppContainerStateLock"


def _api(library: ctypes.WinDLL, name: str, args: list[Any], result: Any = BOOL):
    fn = getattr(library, name)
    fn.argtypes = args
    fn.restype = result
    return fn


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GetSystemDirectoryW = _api(_kernel32, "GetSystemDirectoryW", [w.LPWSTR, DWORD], DWORD)
_LocalFree = _api(_kernel32, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
_CloseHandle = _api(_kernel32, "CloseHandle", [HANDLE])
_CreateFileW = _api(
    _kernel32,
    "CreateFileW",
    [w.LPCWSTR, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, HANDLE],
    HANDLE,
)
_CreateMutexW = _api(_kernel32, "CreateMutexW", [ctypes.c_void_p, BOOL, w.LPCWSTR], HANDLE)
_WaitForSingleObject = _api(_kernel32, "WaitForSingleObject", [HANDLE, DWORD], DWORD)
_ReleaseMutex = _api(_kernel32, "ReleaseMutex", [HANDLE])

_MUTEX_INIT_LOCK = threading.Lock()
_ACL_STATE_MUTEX_HANDLE: HANDLE | None = None


def _get_state_mutex() -> HANDLE:
    global _ACL_STATE_MUTEX_HANDLE
    if _ACL_STATE_MUTEX_HANDLE:
        return _ACL_STATE_MUTEX_HANDLE
    with _MUTEX_INIT_LOCK:
        if not _ACL_STATE_MUTEX_HANDLE:
            # Default user DACL (lpMutexAttributes=None) in session-local Local\ namespace;
            # requires zero Administrator privileges and synchronizes concurrent Virgo processes.
            handle = _CreateMutexW(None, False, _ACL_STATE_MUTEX_NAME)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            _ACL_STATE_MUTEX_HANDLE = handle
        return _ACL_STATE_MUTEX_HANDLE


@contextmanager
def acl_state_lock(timeout_ms: int = 30000):
    """Cross-process per-user mutex serializing shared DACL RMW and DOS drive allocation."""
    handle = _get_state_mutex()
    status = _WaitForSingleObject(handle, timeout_ms)
    if status not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
        if status == _WAIT_TIMEOUT:
            raise TimeoutError("Timed out waiting for Windows sandbox state mutex")
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        _ReleaseMutex(handle)


def _system_dir() -> Path:
    buf = ctypes.create_unicode_buffer(32768)
    count = _GetSystemDirectoryW(buf, len(buf))
    if not count or count >= len(buf):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buf.value)


_advapi32 = ctypes.WinDLL(str(_system_dir() / "advapi32.dll"), use_last_error=True)

_GetNamedSecurityInfoW = _api(
    _advapi32,
    "GetNamedSecurityInfoW",
    [
        w.LPCWSTR,
        DWORD,
        DWORD,
        ctypes.POINTER(PSID),
        ctypes.POINTER(PSID),
        ctypes.POINTER(PACL),
        ctypes.POINTER(PACL),
        ctypes.POINTER(PSECURITY_DESCRIPTOR),
    ],
    DWORD,
)
_SetNamedSecurityInfoW = _api(
    _advapi32,
    "SetNamedSecurityInfoW",
    [w.LPWSTR, DWORD, DWORD, PSID, PSID, PACL, PACL],
    DWORD,
)
_GetSecurityDescriptorControl = _api(
    _advapi32,
    "GetSecurityDescriptorControl",
    [PSECURITY_DESCRIPTOR, ctypes.POINTER(WORD), ctypes.POINTER(DWORD)],
    BOOL,
)
_ConvertStringSidToSidW = _api(
    _advapi32, "ConvertStringSidToSidW", [w.LPCWSTR, ctypes.POINTER(PSID)], BOOL
)
_ConvertSidToStringSidW = _api(
    _advapi32, "ConvertSidToStringSidW", [PSID, ctypes.POINTER(w.LPWSTR)], BOOL
)
_GetLengthSid = _api(_advapi32, "GetLengthSid", [PSID], DWORD)
_IsValidSid = _api(_advapi32, "IsValidSid", [PSID], BOOL)


def _normalize_win_path(path: str | Path) -> str:
    raw = os.path.abspath(os.fspath(path))
    if len(raw) >= 248 and not raw.startswith("\\\\?\\"):
        if raw.startswith("\\\\"):
            return "\\\\?\\UNC\\" + raw[2:]
        return "\\\\?\\" + raw
    return raw


def _sid_str_to_bytes(sid_str: str) -> bytes:
    sid_ptr = PSID()
    if not _ConvertStringSidToSidW(sid_str, ctypes.byref(sid_ptr)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not _IsValidSid(sid_ptr):
            raise ValueError(f"Invalid SID: {sid_str!r}")
        length = _GetLengthSid(sid_ptr)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(sid_ptr, length)
    finally:
        _LocalFree(sid_ptr)


def _sid_bytes_to_str(sid_bytes: bytes) -> str:
    buf = ctypes.create_string_buffer(sid_bytes)
    out = w.LPWSTR()
    if not _ConvertSidToStringSidW(buf, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return out.value
    finally:
        _LocalFree(out)


_AAP_SID_BYTES = _sid_str_to_bytes(ALL_APPLICATION_PACKAGES_SID)


def _build_ace(ace_type: int, ace_flags: int, access_mask: int, sid_bytes: bytes) -> bytes:
    return struct.pack("<BBHI", ace_type, ace_flags, 8 + len(sid_bytes), access_mask) + sid_bytes


def _ace_sid_bytes(ace: bytes) -> bytes | None:
    if len(ace) < 8:
        return None
    ace_type = ace[0]
    if ace_type in _STANDARD_ACE_TYPES:
        offset = 8
    elif ace_type in _OBJECT_ACE_TYPES and len(ace) >= 12:
        obj_flags = struct.unpack_from("<I", ace, 8)[0]
        offset = 12 + (16 if obj_flags & 1 else 0) + (16 if obj_flags & 2 else 0)
    else:
        return None
    if offset + 8 > len(ace) or ace[offset] != 1:
        return None
    end = offset + 8 + 4 * ace[offset + 1]
    return ace[offset:end] if end <= len(ace) else None


def _ace_mask(ace: bytes) -> int:
    return struct.unpack_from("<I", ace, 4)[0] if len(ace) >= 8 else 0


def _canonical_order(aces: list[bytes]) -> list[bytes]:
    exp_deny: list[bytes] = []
    exp_allow: list[bytes] = []
    inh_deny: list[bytes] = []
    inh_allow: list[bytes] = []
    for ace in aces:
        inherited = bool(ace[1] & INHERITED_ACE)
        is_deny = ace[0] in _DENY_ACE_TYPES
        if not inherited:
            (exp_deny if is_deny else exp_allow).append(ace)
        else:
            (inh_deny if is_deny else inh_allow).append(ace)
    return exp_deny + exp_allow + inh_deny + inh_allow


@dataclass
class _ParsedDacl:
    revision: int
    control: int
    protected: bool
    is_null: bool
    aces: list[bytes]


def _parse_acl_ptr(dacl_ptr: int, label: str) -> tuple[int, list[bytes]]:
    header = ctypes.string_at(dacl_ptr, 8)
    acl_rev, _, acl_size, ace_count, _ = struct.unpack("<BBHHH", header)
    raw = ctypes.string_at(dacl_ptr, acl_size)
    aces: list[bytes] = []
    pos = 8
    for _ in range(ace_count):
        if pos + 4 > len(raw):
            raise OSError(f"Malformed DACL on {label}")
        ace_size = struct.unpack_from("<H", raw, pos + 2)[0]
        if ace_size < 4 or pos + ace_size > len(raw):
            raise OSError(f"Malformed ACE in DACL on {label}")
        aces.append(raw[pos : pos + ace_size])
        pos += ace_size
    return acl_rev, aces


def _read_dacl(win_path: str) -> _ParsedDacl:
    dacl_ptr = PACL()
    sd_ptr = PSECURITY_DESCRIPTOR()
    err = _GetNamedSecurityInfoW(
        win_path,
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl_ptr),
        None,
        ctypes.byref(sd_ptr),
    )
    if err != 0:
        raise ctypes.WinError(err)
    try:
        control = WORD()
        sd_rev = DWORD()
        if not _GetSecurityDescriptorControl(sd_ptr, ctypes.byref(control), ctypes.byref(sd_rev)):
            raise ctypes.WinError(ctypes.get_last_error())
        protected = bool(control.value & SE_DACL_PROTECTED)
        if not dacl_ptr.value:
            return _ParsedDacl(2, int(control.value), protected, True, [])
        acl_rev, aces = _parse_acl_ptr(dacl_ptr.value, win_path)
        return _ParsedDacl(acl_rev, int(control.value), protected, False, aces)
    finally:
        if sd_ptr.value:
            _LocalFree(sd_ptr)


def _pack_acl(revision: int, aces: list[bytes]) -> ctypes.Array[ctypes.c_char]:
    has_object = any(ace[0] in _OBJECT_ACE_TYPES for ace in aces)
    acl_rev = max(revision, 4 if has_object else 2)
    acl_size = 8 + sum(len(ace) for ace in aces)
    if acl_size > 65535 or len(aces) > 65535:
        raise OSError(f"Constructed DACL exceeds 64KB Windows ACL limit ({acl_size} bytes)")
    payload = struct.pack("<BBHHH", acl_rev, 0, acl_size, len(aces), 0) + b"".join(aces)
    return ctypes.create_string_buffer(payload, acl_size)


def _apply_dacl(win_path: str, revision: int, aces: list[bytes], sec_info: int) -> None:
    acl_buf = _pack_acl(revision, _canonical_order(aces))
    path_buf = ctypes.create_unicode_buffer(win_path)
    err = _SetNamedSecurityInfoW(
        path_buf,
        SE_FILE_OBJECT,
        sec_info,
        None,
        None,
        ctypes.cast(acl_buf, PACL),
        None,
    )
    if err != 0:
        raise ctypes.WinError(err)


def _write_dacl_preserving_protection(win_path: str, previous: _ParsedDacl, new_aces: list[bytes]) -> None:
    if previous.protected:
        sec_info = DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
        entries = new_aces
    else:
        sec_info = DACL_SECURITY_INFORMATION
        entries = [ace for ace in new_aces if not (ace[1] & INHERITED_ACE)]
    _apply_dacl(win_path, previous.revision, entries, sec_info)


def _is_directory_no_follow(path: str | Path) -> bool:
    return stat.S_ISDIR(os.lstat(path).st_mode)


def is_appcontainer_package_sid(sid_bytes: bytes) -> bool:
    """Return True if ``sid_bytes`` is a per-package AppContainer SID (S-1-15-2-<r1>-...).

    Excludes well-known S-1-15-2-1 (ALL APPLICATION PACKAGES) and S-1-15-2-2 (ARAP),
    which have only two sub-authorities.
    """
    return (
        len(sid_bytes) >= 16
        and sid_bytes[0] == 1
        and sid_bytes[1] >= 3
        and sid_bytes[2:8] == b"\x00\x00\x00\x00\x00\x0f"
        and struct.unpack_from("<I", sid_bytes, 8)[0] == 2
    )


def _has_other_appcontainer_sid(aces: list[bytes], exclude_sid_bytes: bytes | None) -> bool:
    for ace in aces:
        raw = _ace_sid_bytes(ace)
        if raw is not None and is_appcontainer_package_sid(raw) and raw != exclude_sid_bytes:
            return True
    return False


def grant_root_access(path: str | Path, sid_str: str, *, writable: bool) -> None:
    """Grant ``sid_str`` read-only or writable access on ``path`` in one OS call.

    Directories receive OI|CI inheritable ACEs propagated by SetNamedSecurityInfoW.
    Writable directories deliberately omit FILE_DELETE_CHILD (0x40) and apply
    DELETE (0x10000) inherit-only (OI|CI|IO) so protected children cannot be
    deleted or renamed via parent directory rights.
    """
    win_path = _normalize_win_path(path)
    sid_bytes = _sid_str_to_bytes(sid_str)
    is_dir = _is_directory_no_follow(win_path)
    with acl_state_lock():
        previous = _read_dacl(win_path)
        if previous.is_null:
            raise OSError(errno.EPERM, "Refusing to overwrite a NULL DACL on Windows", win_path)

        base_aces = [
            ace
            for ace in previous.aces
            if not (
                not (ace[1] & INHERITED_ACE)
                and ace[0] not in _DENY_ACE_TYPES
                and _ace_sid_bytes(ace) == sid_bytes
            )
        ]

        if is_dir:
            oi_ci = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
            if writable:
                new_entries = [
                    _build_ace(ACCESS_ALLOWED_ACE_TYPE, oi_ci, WRITE_NO_DELETE_MASK, sid_bytes),
                    _build_ace(ACCESS_ALLOWED_ACE_TYPE, oi_ci | INHERIT_ONLY_ACE, DELETE, sid_bytes),
                ]
            else:
                new_entries = [
                    _build_ace(ACCESS_ALLOWED_ACE_TYPE, oi_ci, READ_EXECUTE_MASK, sid_bytes)
                ]
        else:
            mask = MODIFY_NO_DELETE_CHILD_MASK if writable else READ_EXECUTE_MASK
            new_entries = [_build_ace(ACCESS_ALLOWED_ACE_TYPE, 0, mask, sid_bytes)]

        _write_dacl_preserving_protection(win_path, previous, base_aces + new_entries)


def capture_dacl_snapshot(path: str | Path, exclude_sid_str: str | None = None) -> dict[str, Any]:
    """Snapshot protection state and non-session explicit ACEs before protecting ``path``."""
    win_path = _normalize_win_path(path)
    previous = _read_dacl(win_path)
    exclude_bytes = _sid_str_to_bytes(exclude_sid_str) if exclude_sid_str else None
    has_other_ac = _has_other_appcontainer_sid(previous.aces, exclude_bytes)
    explicit_b64 = [
        base64.b64encode(ace).decode("ascii")
        for ace in previous.aces
        if not (ace[1] & INHERITED_ACE)
        and (exclude_bytes is None or _ace_sid_bytes(ace) != exclude_bytes)
        and not (
            (raw := _ace_sid_bytes(ace)) is not None and is_appcontainer_package_sid(raw)
        )
    ]
    return {
        "was_protected": previous.protected and not has_other_ac,
        "has_other_appcontainer_sid": has_other_ac,
        "original_explicit_aces_b64": explicit_b64,
    }


def is_dacl_protected(path: str | Path) -> bool:
    """Return True if ``path`` currently has SE_DACL_PROTECTED set."""
    return _read_dacl(_normalize_win_path(path)).protected


def is_protected_readonly_for_sid(path: str | Path, sid_str: str) -> bool:
    """Return True if ``path`` already has protected read-only ACEs for ``sid_str``."""
    win_path = _normalize_win_path(path)
    sid_bytes = _sid_str_to_bytes(sid_str)
    parsed = _read_dacl(win_path)
    if not parsed.protected:
        return False
    sid_aces = [ace for ace in parsed.aces if _ace_sid_bytes(ace) == sid_bytes]
    return (
        len(sid_aces) == 1
        and sid_aces[0][0] == ACCESS_ALLOWED_ACE_TYPE
        and _ace_mask(sid_aces[0]) == READ_EXECUTE_MASK
    )


def protect_readonly_path(path: str | Path, sid_str: str) -> None:
    """Restrict ``sid_str`` on ``path`` to read-only via SE_DACL_PROTECTED + ALLOW(RX).

    Preserves all non-session ACEs (keeping full host/user/IDE/git access) as
    explicit entries, drops inherited writable ACEs for ``sid_str``, and adds
    explicit READ_EXECUTE (0x001200A9) for ``sid_str`` in one SetNamedSecurityInfoW call.
    """
    win_path = _normalize_win_path(path)
    sid_bytes = _sid_str_to_bytes(sid_str)
    is_dir = _is_directory_no_follow(win_path)
    flags = (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE) if is_dir else 0
    with acl_state_lock():
        previous = _read_dacl(win_path)
        if previous.is_null:
            raise OSError(errno.EPERM, "Refusing to overwrite a NULL DACL on Windows", win_path)

        preserved = [
            bytes([ace[0], ace[1] & ~INHERITED_ACE]) + ace[2:]
            for ace in previous.aces
            if _ace_sid_bytes(ace) != sid_bytes
        ]
        readonly_ace = _build_ace(ACCESS_ALLOWED_ACE_TYPE, flags, READ_EXECUTE_MASK, sid_bytes)
        _apply_dacl(
            win_path,
            previous.revision,
            [*preserved, readonly_ace],
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
        )


def revoke_sid_from_root(
    path: str | Path,
    sid_str: str,
    *,
    ignore_missing: bool = False,
    restore_unprotected: bool = False,
    original_explicit_aces_b64: list[str] | None = None,
) -> None:
    """Remove all explicit ACEs for ``sid_str`` from ``path`` in one OS call.

    When ``restore_unprotected=True``, restores UNPROTECTED_DACL_SECURITY_INFORMATION
    with ``original_explicit_aces_b64`` so Windows re-inherits parent ACEs and
    propagates ``sid_str`` removal down the subtree—unless another AppContainer
    session SID is still protecting ``path``, in which case only ``sid_str`` is
    removed while preserving SE_DACL_PROTECTED for the surviving session(s).
    """
    win_path = _normalize_win_path(path)
    sid_bytes = _sid_str_to_bytes(sid_str)
    with acl_state_lock():
        try:
            previous = _read_dacl(win_path)
        except OSError as exc:
            if ignore_missing and exc.winerror in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
                return
            raise

        if restore_unprotected and not _has_other_appcontainer_sid(previous.aces, sid_bytes):
            decoded = [
                base64.b64decode(item)
                for item in (original_explicit_aces_b64 or [])
            ]
            keep = [
                ace
                for ace in decoded
                if _ace_sid_bytes(ace) != sid_bytes
                and not (
                    (raw := _ace_sid_bytes(ace)) is not None and is_appcontainer_package_sid(raw)
                )
            ]
            _apply_dacl(
                win_path,
                previous.revision,
                keep,
                DACL_SECURITY_INFORMATION | UNPROTECTED_DACL_SECURITY_INFORMATION,
            )
            return

        if previous.protected:
            retained = [ace for ace in previous.aces if _ace_sid_bytes(ace) != sid_bytes]
            changed = len(retained) != len(previous.aces)
        else:
            explicit_before = [ace for ace in previous.aces if not (ace[1] & INHERITED_ACE)]
            retained = [ace for ace in explicit_before if _ace_sid_bytes(ace) != sid_bytes]
            changed = len(retained) != len(explicit_before)

        if changed:
            _write_dacl_preserving_protection(win_path, previous, retained)


def has_aap_read_access(path: str | Path, *, require_execute: bool = True) -> bool:
    """Return True if ALL APPLICATION PACKAGES (S-1-15-2-1) has read/execute access on ``path``."""
    win_path = _normalize_win_path(path)
    try:
        parsed = _read_dacl(win_path)
    except OSError:
        return False
    if parsed.is_null:
        return True
    needed = (FILE_READ_DATA | FILE_EXECUTE) if require_execute else FILE_READ_DATA
    granted = 0
    for ace in parsed.aces:
        if ace[0] != ACCESS_ALLOWED_ACE_TYPE:
            continue
        if ace[1] & INHERIT_ONLY_ACE:
            continue
        if _ace_sid_bytes(ace) != _AAP_SID_BYTES:
            continue
        mask = _ace_mask(ace)
        if mask & GENERIC_ALL:
            return True
        if mask & GENERIC_READ:
            granted |= FILE_GENERIC_READ
        if mask & GENERIC_EXECUTE:
            granted |= FILE_GENERIC_EXECUTE
        granted |= mask
    return (granted & needed) == needed


def can_open_write_dac(path: str | Path) -> bool:
    """Read-only probe checking whether the current user can open ``path`` with WRITE_DAC."""
    win_path = _normalize_win_path(path)
    handle = _CreateFileW(
        win_path,
        WRITE_DAC,
        0x7,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return False
    _CloseHandle(handle)
    return True


def ancestor_lacks_aap(workspace: str | Path) -> bool:
    """Return True if any ancestor directory of ``workspace`` lacks S-1-15-2-1 read access."""
    current = Path(os.path.abspath(os.fspath(workspace))).parent
    while True:
        if not has_aap_read_access(current, require_execute=False):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def inspect_dacl(path: str | Path) -> list[dict[str, Any]]:
    """Inspect all ACEs (explicit and inherited) on ``path``."""
    win_path = _normalize_win_path(path)
    parsed = _read_dacl(win_path)
    entries: list[dict[str, Any]] = []
    for idx, ace in enumerate(parsed.aces):
        ace_type = ace[0]
        ace_flags = ace[1]
        mask = _ace_mask(ace)
        sid_raw = _ace_sid_bytes(ace)
        sid_text = _sid_bytes_to_str(sid_raw) if sid_raw is not None else "<unknown>"
        entries.append({
            "index": idx,
            "type": _ACE_TYPE_NAMES.get(ace_type, f"TYPE_{ace_type}"),
            "type_code": ace_type,
            "flags": ace_flags,
            "flags_names": [label for bit, label in _ACE_FLAG_NAMES if ace_flags & bit],
            "inherited": bool(ace_flags & INHERITED_ACE),
            "mask": mask,
            "mask_hex": f"0x{mask:08X}",
            "sid": sid_text,
            "is_aap": sid_text == ALL_APPLICATION_PACKAGES_SID,
        })
    return entries


def _is_reparse_point(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def scan_residual_aces(root: str | Path, sid_str: str) -> list[str]:
    """Read-only audit walking ``root`` (pruning reparse points) for remaining ``sid_str`` ACEs."""
    sid_bytes = _sid_str_to_bytes(sid_str)
    root_path = os.path.abspath(os.fspath(root))
    residuals: list[str] = []

    def _check(target: str) -> None:
        try:
            parsed = _read_dacl(_normalize_win_path(target))
        except OSError as exc:
            if exc.winerror in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
                return
            raise
        if any(_ace_sid_bytes(ace) == sid_bytes for ace in parsed.aces):
            residuals.append(target)

    if not os.path.exists(root_path):
        return residuals

    _check(root_path)
    if _is_directory_no_follow(root_path) and not _is_reparse_point(root_path):
        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            safe_dirs: list[str] = []
            for dname in dirnames:
                dpath = os.path.join(dirpath, dname)
                _check(dpath)
                if not _is_reparse_point(dpath):
                    safe_dirs.append(dname)
            dirnames[:] = safe_dirs
            for fname in filenames:
                _check(os.path.join(dirpath, fname))

    residuals.sort()
    return residuals
