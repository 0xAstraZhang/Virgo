"""Read-only Windows OS / \\Device\\Null capability probe with zero sandbox imports.

Used by ``execution_policy.default_execution_mode()`` and ``AgentSession`` so
Host (No-Limit) sessions can check native Windows AppContainer eligibility
without importing ``virgo.sandbox`` or touching any OS state.
"""
from __future__ import annotations

import ctypes
import struct
import sys
from ctypes import wintypes as w

# Well-known binary SIDs (MS-DTYP 2.4.2.2):
# S-1-15-2-1 (APPLICATION PACKAGE AUTHORITY\ALL APPLICATION PACKAGES)
AAP_SID_BYTES = struct.pack("<BB6sII", 1, 2, b"\x00\x00\x00\x00\x00\x0f", 2, 1)
# S-1-1-0 (Everyone / World)
WORLD_SID_BYTES = struct.pack("<BB6sI", 1, 1, b"\x00\x00\x00\x00\x00\x01", 0)

_ACCESS_ALLOWED_ACE_TYPE = 0
_DENY_ACE_TYPES = frozenset({1, 6, 10, 12})
_STANDARD_ACE_TYPES = frozenset({0, 1, 2, 3, 9, 10, 13, 14})
_OBJECT_ACE_TYPES = frozenset({5, 6, 7, 8, 11, 12, 15, 16})
_INHERIT_ONLY_ACE = 0x08

_READ_CONTROL = 0x00020000
_DACL_SECURITY_INFORMATION = 0x00000004
_GENERIC_ALL = 0x10000000
_GENERIC_WRITE = 0x40000000
_GENERIC_READ = 0x80000000
_FILE_GENERIC_READ = 0x00120089
_FILE_GENERIC_WRITE = 0x00120116
NULL_DEVICE_REQUIRED_ACCESS = _FILE_GENERIC_READ | _FILE_GENERIC_WRITE  # 0x0012019F


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


def dacl_allows_aap_null_access(aces: list[bytes], *, is_null: bool = False) -> bool:
    """Return True if a parsed DACL grants S-1-15-2-1 read/write access (0x0012019F)."""
    if is_null:
        return True
    allowed = 0
    denied = 0
    for ace in aces:
        if len(ace) < 8 or (ace[1] & _INHERIT_ONLY_ACE):
            continue
        sid = _ace_sid_bytes(ace)
        if sid is None:
            continue
        raw_mask = struct.unpack_from("<I", ace, 4)[0]
        mask = raw_mask
        if raw_mask & _GENERIC_ALL:
            mask |= NULL_DEVICE_REQUIRED_ACCESS
        if raw_mask & _GENERIC_READ:
            mask |= _FILE_GENERIC_READ
        if raw_mask & _GENERIC_WRITE:
            mask |= _FILE_GENERIC_WRITE
        if ace[0] in _DENY_ACE_TYPES and sid in (AAP_SID_BYTES, WORLD_SID_BYTES):
            denied |= mask & ~allowed
        elif ace[0] == _ACCESS_ALLOWED_ACE_TYPE and sid == AAP_SID_BYTES:
            allowed |= mask & ~denied
    return (allowed & NULL_DEVICE_REQUIRED_ACCESS) == NULL_DEVICE_REQUIRED_ACCESS


def is_native_x64_win10_plus() -> bool:
    """Return True only for native x64 Python running on x64 Windows 10+."""
    if sys.platform != "win32":
        return False
    if sys.getwindowsversion().major < 10 or ctypes.sizeof(ctypes.c_void_p) != 8:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = ctypes.c_void_p
        is_wow64 = kernel32.IsWow64Process2
        is_wow64.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.WORD), ctypes.POINTER(w.WORD)]
        is_wow64.restype = ctypes.c_int32
        process_machine, native_machine = w.WORD(), w.WORD()
        if not is_wow64(get_current_process(), ctypes.byref(process_machine), ctypes.byref(native_machine)):
            return False
        return process_machine.value == 0 and native_machine.value == 0x8664
    except (AttributeError, OSError):
        return False


def null_device_allows_appcontainer() -> bool:
    """Read-only probe checking whether \\Device\\Null grants S-1-15-2-1 read/write access.

    Windows 11 servicing builds with updated null.sys (10.0.26100.8972+, WIL Feature
    60692039) natively grant ALL APPLICATION PACKAGES (S-1-15-2-1) 0x001201BF on
    \\Device\\Null so unelevated Standard AppContainer children can open NUL and
    subprocess.DEVNULL. Older Windows 10, older Windows 11, and stock Windows Server
    2022/2025 lack this ACE and return False.
    """
    if sys.platform != "win32":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            w.LPCWSTR,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int32

        get_security = advapi32.GetKernelObjectSecurity
        get_security.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_security.restype = ctypes.c_int32

        get_dacl = advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int32),
        ]
        get_dacl.restype = ctypes.c_int32

        invalid = ctypes.c_void_p(-1).value
        handle = create_file(r"\\?\GLOBALROOT\Device\Null", _READ_CONTROL, 0x7, None, 3, 0, None)
        if not handle or handle == invalid:
            return False
        try:
            needed = ctypes.c_uint32()
            get_security(handle, _DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(needed))
            if not needed.value:
                return False
            buf = ctypes.create_string_buffer(needed.value)
            if not get_security(handle, _DACL_SECURITY_INFORMATION, buf, len(buf), ctypes.byref(needed)):
                return False
            present = ctypes.c_int32()
            defaulted = ctypes.c_int32()
            dacl_ptr = ctypes.c_void_p()
            if not get_dacl(buf, ctypes.byref(present), ctypes.byref(dacl_ptr), ctypes.byref(defaulted)):
                return False
            if not present.value or not dacl_ptr.value:
                return dacl_allows_aap_null_access([], is_null=True)
            _, _, acl_size, ace_count, _ = struct.unpack("<BBHHH", ctypes.string_at(dacl_ptr.value, 8))
            raw = ctypes.string_at(dacl_ptr.value, acl_size)
            aces: list[bytes] = []
            pos = 8
            for _ in range(ace_count):
                if pos + 4 > len(raw):
                    return False
                ace_size = struct.unpack_from("<H", raw, pos + 2)[0]
                if ace_size < 4 or pos + ace_size > len(raw):
                    return False
                aces.append(raw[pos : pos + ace_size])
                pos += ace_size
        finally:
            close_handle(handle)
        return dacl_allows_aap_null_access(aces, is_null=False)
    except OSError:
        return False


def is_windows_appcontainer_supported() -> bool:
    """Return True when native x64 Windows 10+ grants S-1-15-2-1 on \\Device\\Null."""
    return is_native_x64_win10_plus() and null_device_allows_appcontainer()
