"""Shared Win32 ctypes helpers and basic kernel32 bindings for Windows execution modules."""
from __future__ import annotations

import ctypes
import os
import stat
import sys
from ctypes import wintypes as w
from pathlib import Path
from typing import Any

BOOL = ctypes.c_int32
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
UINT = ctypes.c_uint
HANDLE = ctypes.c_void_p
PSID = ctypes.c_void_p
HRESULT = ctypes.c_int32

FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

_LINK_REPARSE_TAGS = frozenset({
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
})


def bind_api(library: Any, name: str, args: list[Any], result: Any = BOOL):
    fn = getattr(library, name)
    fn.argtypes = args
    fn.restype = result
    return fn


def check_win32(value: Any) -> Any:
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def is_reparse_point(path: str | os.PathLike[str], *, link_only: bool = False) -> bool:
    """Return True if ``path`` is a reparse point (or link reparse point when ``link_only``)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not (getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT):
        return False
    if not link_only:
        return True
    tag = getattr(info, "st_reparse_tag", 0)
    return tag in _LINK_REPARSE_TAGS if tag else True


if sys.platform == "win32":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GetSystemDirectoryW = bind_api(kernel32, "GetSystemDirectoryW", [w.LPWSTR, DWORD], DWORD)
    GetWindowsDirectoryW = bind_api(kernel32, "GetWindowsDirectoryW", [w.LPWSTR, DWORD], DWORD)
    CloseHandle = bind_api(kernel32, "CloseHandle", [HANDLE])
    LocalFree = bind_api(kernel32, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
    GetCurrentProcess = bind_api(kernel32, "GetCurrentProcess", [], HANDLE)
    IsWow64Process2 = bind_api(
        kernel32, "IsWow64Process2", [HANDLE, ctypes.POINTER(WORD), ctypes.POINTER(WORD)]
    )

    def _query_dir(api) -> Path:
        buf = ctypes.create_unicode_buffer(32768)
        count = check_win32(api(buf, len(buf)))
        if count >= len(buf):
            raise OSError("Windows system directory exceeds the native path limit")
        return Path(buf.value)

    def system_directory() -> Path:
        return _query_dir(GetSystemDirectoryW)

    def windows_directory() -> Path:
        return _query_dir(GetWindowsDirectoryW)

    def system_dll(name: str) -> ctypes.WinDLL:
        return ctypes.WinDLL(str(system_directory() / name), use_last_error=True)
