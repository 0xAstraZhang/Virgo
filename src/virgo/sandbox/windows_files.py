"""Local persistent-ACL volume path normalization and validation for Windows AppContainer.

Supports local NTFS and Windows 11 ReFS Dev Drives (any fixed local volume with
FILE_PERSISTENT_ACLS). Ancestor directory junctions and OneDrive cloud reparse
tags are resolved cleanly; link reparse points are rejected only when
``allow_reparse=False`` (e.g. protected control targets inside a workspace).
This module is imported only on Windows.
"""
from __future__ import annotations

import ctypes
import errno
import ntpath
import os
import re
import stat
from ctypes import wintypes as w
from pathlib import Path

DWORD = ctypes.c_uint32
UINT = ctypes.c_uint
BOOL = ctypes.c_int32
DRIVE_FIXED = 3
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FILE_PERSISTENT_ACLS = 0x00000008

_LINK_REPARSE_TAGS = frozenset({
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
})

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_GetDriveTypeW = _kernel32.GetDriveTypeW
_GetDriveTypeW.argtypes = [w.LPCWSTR]
_GetDriveTypeW.restype = UINT

_GetVolumeInformationW = _kernel32.GetVolumeInformationW
_GetVolumeInformationW.argtypes = [
    w.LPCWSTR,
    w.LPWSTR,
    DWORD,
    ctypes.POINTER(DWORD),
    ctypes.POINTER(DWORD),
    ctypes.POINTER(DWORD),
    w.LPWSTR,
    DWORD,
]
_GetVolumeInformationW.restype = BOOL

_RESERVED_DEVICE_RE = re.compile(
    r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])",
    re.IGNORECASE,
)


def _invalid(path, reason: str) -> OSError:
    return OSError(errno.EINVAL, f"Unsupported Windows sandbox path: {reason}", os.fspath(path))


def _validate_component(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or name[-1] in " ."
        or any(ord(char) < 32 or char in '\\/:*?"<>|' for char in name)
        or _RESERVED_DEVICE_RE.fullmatch(name.split(".", 1)[0])
    ):
        raise _invalid(name, "invalid or reserved name component")


def normalize_path(path: str | os.PathLike[str]) -> str:
    """Normalize DOS dot components without following filesystem links."""
    value = os.fspath(path)
    if not isinstance(value, str):
        raise _invalid(path, "a string path is required")
    if value.startswith("\\\\?\\") and len(value) > 6 and value[5] == ":":
        value = value[4:]
    if not re.match(r"^[A-Za-z]:[\\/]", value):
        raise _invalid(path, "an absolute local drive path is required")
    value = value.replace("/", "\\")
    for name in value[3:].split("\\"):
        if name and name not in {".", ".."}:
            _validate_component(name)
    normalized = ntpath.normpath(value)
    return normalized[0].upper() + normalized[1:]


def is_reparse_point(path: str | os.PathLike[str]) -> bool:
    """Return True if ``path`` itself is a symlink or directory junction (ignoring OneDrive cloud tags)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not (getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT):
        return False
    tag = getattr(info, "st_reparse_tag", 0)
    return tag in _LINK_REPARSE_TAGS if tag else True


def verify_local_ntfs(
    path: str | os.PathLike[str],
    *,
    allow_reparse: bool = True,
    expect_directory: bool | None = None,
) -> Path:
    """Validate that ``path`` resolves to a fixed local volume supporting persistent ACLs (NTFS or ReFS)."""
    normalized = normalize_path(path)
    target = Path(normalized)
    if not os.path.lexists(target):
        raise FileNotFoundError(errno.ENOENT, "Path does not exist", normalized)

    if not allow_reparse and is_reparse_point(target):
        raise OSError(errno.ELOOP, "Protected Windows sandbox path must not be a link reparse point", str(target))

    resolved = Path(normalize_path(target.resolve(strict=True)))
    if not allow_reparse and ntpath.normcase(str(resolved)) != ntpath.normcase(normalized):
        raise OSError(errno.ELOOP, "Protected Windows sandbox path resolved outside its lexical path", normalized)

    drive_root = resolved.anchor
    if _GetDriveTypeW(drive_root) != DRIVE_FIXED:
        raise _invalid(resolved, "a fixed local volume is required")

    fs_name = ctypes.create_unicode_buffer(64)
    fs_flags = DWORD()
    if not _GetVolumeInformationW(
        drive_root,
        None,
        0,
        None,
        None,
        ctypes.byref(fs_flags),
        fs_name,
        len(fs_name),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not (fs_flags.value & FILE_PERSISTENT_ACLS):
        raise _invalid(resolved, "a volume with persistent ACLs (NTFS or ReFS Dev Drive) is required")

    if expect_directory is True and not resolved.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "Directory path required", str(resolved))
    if expect_directory is False and not resolved.is_file():
        raise IsADirectoryError(errno.EISDIR, "Regular file path required", str(resolved))
    return resolved
