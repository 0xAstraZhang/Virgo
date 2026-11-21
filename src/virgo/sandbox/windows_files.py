"""Local NTFS path normalization and validation for the Windows AppContainer sandbox.

Only direct local NTFS drive paths with persistent ACLs are accepted as
canonical Broker paths. Child commands may receive a session-local virtual
drive letter mapped from an already-validated canonical workspace root.
This module is imported only on Windows.
"""
from __future__ import annotations

import ctypes
import errno
import ntpath
import os
import re
from ctypes import wintypes as w
from pathlib import Path

DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FILE_PERSISTENT_ACLS = 0x00000008

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_QueryDosDeviceW = _kernel32.QueryDosDeviceW
_QueryDosDeviceW.argtypes = [w.LPCWSTR, w.LPWSTR, DWORD]
_QueryDosDeviceW.restype = DWORD

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
_HARDDISK_VOLUME_RE = re.compile(r"\\Device\\HarddiskVolume[0-9]+", re.IGNORECASE)


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
    if not isinstance(value, str) or not re.match(r"^[A-Za-z]:[\\/]", value):
        raise _invalid(path, "an absolute local drive path is required")
    value = value.replace("/", "\\")
    for name in value[3:].split("\\"):
        if name and name not in {".", ".."}:
            _validate_component(name)
    normalized = ntpath.normpath(value)
    return normalized[0].upper() + normalized[1:]


def is_reparse_point(path: str | os.PathLike[str]) -> bool:
    """Return True if ``path`` itself is a Windows reparse point (symlink/junction)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def verify_local_ntfs(
    path: str | os.PathLike[str],
    *,
    allow_reparse: bool = False,
    expect_directory: bool | None = None,
) -> Path:
    """Validate that ``path`` exists on a direct local NTFS volume with persistent ACLs."""
    normalized = normalize_path(path)
    drive = normalized[:2]
    device = ctypes.create_unicode_buffer(32768)
    if not _QueryDosDeviceW(drive, device, len(device)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not _HARDDISK_VOLUME_RE.fullmatch(device.value):
        raise _invalid(path, "mapped, substituted and non-disk drives are not supported")

    fs_name = ctypes.create_unicode_buffer(64)
    fs_flags = DWORD()
    if not _GetVolumeInformationW(
        drive + "\\",
        None,
        0,
        None,
        None,
        ctypes.byref(fs_flags),
        fs_name,
        len(fs_name),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if fs_name.value.upper() != "NTFS" or not (fs_flags.value & FILE_PERSISTENT_ACLS):
        raise _invalid(path, "a local NTFS volume with persistent ACLs is required")

    target = Path(normalized)
    if not os.path.lexists(target):
        raise FileNotFoundError(errno.ENOENT, "Path does not exist", normalized)

    if not allow_reparse:
        # Reject reparse points on the target itself or any parent component.
        current = target
        while True:
            if is_reparse_point(current):
                raise OSError(errno.ELOOP, "Windows sandbox paths must not contain reparse points", str(current))
            parent = current.parent
            if parent == current:
                break
            current = parent
        # Canonicalize casing on disk while verifying no link indirection occurred.
        resolved = target.resolve(strict=True)
        if ntpath.normcase(str(resolved)) != ntpath.normcase(normalized):
            raise OSError(errno.ELOOP, "Windows sandbox path resolved outside its lexical path", normalized)
        target = resolved

    if expect_directory is True and not target.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "Directory path required", str(target))
    if expect_directory is False and not target.is_file():
        raise IsADirectoryError(errno.EISDIR, "Regular file path required", str(target))
    return target
