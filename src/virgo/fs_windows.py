"""No-reparse Windows file primitives copied into the trusted filesystem worker.

Only standard-library imports are allowed: this file is loaded beside the fixed
worker snapshot, never from a workspace package. Native sandbox ACLs continue to
enforce the authority of every handle operation.
"""
from __future__ import annotations

import ctypes
import errno
import msvcrt
import ntpath
import os
import re
import weakref
from dataclasses import dataclass

DWORD, WORD, HANDLE = ctypes.c_uint32, ctypes.c_uint16, ctypes.c_void_p
NTSTATUS, BOOL = ctypes.c_int32, ctypes.c_int32
FILE_OPEN, FILE_CREATE, FILE_OPEN_IF = 1, 2, 3
FILE_READ_ATTRIBUTES, SYNCHRONIZE = 0x80, 0x100000
FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT = 0x10, 0x400
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", WORD), ("MaximumLength", WORD), ("Buffer", ctypes.c_void_p)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [("Length", DWORD), ("RootDirectory", HANDLE),
                ("ObjectName", ctypes.POINTER(_UnicodeString)), ("Attributes", DWORD),
                ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]


class _IOStatus(ctypes.Union):
    _fields_ = [("Status", NTSTATUS), ("Pointer", ctypes.c_void_p)]


class _IOStatusBlock(ctypes.Structure):
    _fields_ = [("Result", _IOStatus), ("Information", ctypes.c_size_t)]


class _FileTime(ctypes.Structure):
    _fields_ = [("Low", DWORD), ("High", DWORD)]


class _ByHandleInfo(ctypes.Structure):
    _fields_ = [("Attributes", DWORD), ("CreationTime", _FileTime),
                ("LastAccessTime", _FileTime), ("LastWriteTime", _FileTime),
                ("VolumeSerial", DWORD), ("SizeHigh", DWORD), ("SizeLow", DWORD),
                ("Links", DWORD), ("IndexHigh", DWORD), ("IndexLow", DWORD)]


class _RenameInfo(ctypes.Structure):
    _fields_ = [("Flags", DWORD), ("RootDirectory", HANDLE),
                ("FileNameLength", DWORD), ("FileName", ctypes.c_ubyte * 1)]


def _api(library, name, args, result=BOOL):
    function = getattr(library, name)
    function.argtypes, function.restype = args, result
    return function


CloseHandle = _api(kernel, "CloseHandle", [HANDLE])
CreateFile = _api(kernel, "CreateFileW", [
    ctypes.c_wchar_p, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, HANDLE,
], HANDLE)
GetFileInformation = _api(kernel, "GetFileInformationByHandle", [HANDLE, ctypes.POINTER(_ByHandleInfo)])
GetVolumeInformation = _api(kernel, "GetVolumeInformationByHandleW", [
    HANDLE, ctypes.c_wchar_p, DWORD, ctypes.POINTER(DWORD), ctypes.POINTER(DWORD),
    ctypes.POINTER(DWORD), ctypes.c_wchar_p, DWORD,
])
NtCreateFile = _api(ntdll, "NtCreateFile", [
    ctypes.POINTER(HANDLE), DWORD, ctypes.POINTER(_ObjectAttributes), ctypes.POINTER(_IOStatusBlock),
    ctypes.c_void_p, DWORD, DWORD, DWORD, DWORD, ctypes.c_void_p, DWORD,
], NTSTATUS)
NtSetInformationFile = _api(ntdll, "NtSetInformationFile", [
    HANDLE, ctypes.POINTER(_IOStatusBlock), ctypes.c_void_p, DWORD, ctypes.c_int,
], NTSTATUS)
RtlNtStatusToDosError = _api(ntdll, "RtlNtStatusToDosError", [NTSTATUS], DWORD)


def _check(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def _invalid(path, reason):
    return OSError(errno.EINVAL, f"Unsupported Windows sandbox path: {reason}", os.fspath(path))


def _component(name):
    if (not name or name in {".", ".."} or name[-1] in " ."
            or any(ord(char) < 32 or char in '\\/:*?"<>|' for char in name)
            or re.fullmatch(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])",
                            name.split(".", 1)[0], re.IGNORECASE)):
        raise _invalid(name, "invalid or reserved name component")


def normalize_path(path) -> str:
    """Normalize DOS dot components without following filesystem links."""
    value = os.fspath(path)
    if not isinstance(value, str) or not re.match(r"^[A-Za-z]:[\\/]", value):
        raise _invalid(path, "an absolute local drive path is required")
    value = value.replace("/", "\\")
    for name in value[3:].split("\\"):
        if name and name not in {".", ".."}:
            _component(name)
    normalized = ntpath.normpath(value)
    return normalized[0].upper() + normalized[1:]


@dataclass(frozen=True)
class FileInfo:
    attributes: int
    nlinks: int
    volume_serial: int
    file_index: int
    size: int


def descriptor_identity(descriptor: int) -> tuple[int, int]:
    """Read the same native identity from an already checked CRT descriptor."""
    info = _ByHandleInfo()
    _check(GetFileInformation(msvcrt.get_osfhandle(descriptor), ctypes.byref(info)))
    return info.VolumeSerial, (info.IndexHigh << 32) | info.IndexLow


class FileHandle:
    """An owned, non-inheritable handle with stable volume/file identity."""

    def __init__(self, value: int, *, allow_reparse=False):
        self.value = value
        self._allow_reparse = allow_reparse
        self.refresh_info()
        self._finalizer = weakref.finalize(self, CloseHandle, value)

    def refresh_info(self) -> FileInfo:
        native = _ByHandleInfo()
        _check(GetFileInformation(self.value, ctypes.byref(native)))
        if native.Attributes & FILE_ATTRIBUTE_REPARSE_POINT and not self._allow_reparse:
            raise OSError(errno.ELOOP, "Windows sandbox paths must not contain reparse points")
        self.info = FileInfo(native.Attributes, native.Links, native.VolumeSerial,
                             (native.IndexHigh << 32) | native.IndexLow,
                             (native.SizeHigh << 32) | native.SizeLow)
        return self.info

    @property
    def identity(self) -> tuple[int, int]:
        return self.info.volume_serial, self.info.file_index

    def detach(self) -> int:
        if not self.value:
            raise ValueError("File handle is closed")
        value, self.value = self.value, None
        self._finalizer.detach()
        return value

    def close(self):
        if self.value:
            _check(CloseHandle(self.value))
            self.value = None
            self._finalizer.detach()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _check_filesystem(handle, name):
    filesystem, flags = ctypes.create_unicode_buffer(32), DWORD()
    _check(GetVolumeInformation(handle, None, 0, None, None, ctypes.byref(flags),
                                 filesystem, len(filesystem)))
    if filesystem.value.upper() != "NTFS" or not flags.value & 0x8:  # PERSISTENT_ACLS
        raise _invalid(name, "a local NTFS volume with persistent ACLs is required")


def _open(name, access, *, parent=None, share_delete=True, share_write=True,
          directory=None, disposition=FILE_OPEN, allow_reparse=False):
    if disposition not in {FILE_OPEN, FILE_CREATE, FILE_OPEN_IF}:
        raise ValueError("Unsupported native file disposition")
    # UNICODE_STRING counts UTF-16 bytes, including surrogate pairs correctly.
    encoded = name.encode("utf-16-le")
    if len(encoded) > 65532:
        raise _invalid(name, "path exceeds the native Unicode length limit")
    buffer = ctypes.create_string_buffer(encoded + b"\0\0")
    text = _UnicodeString(len(encoded), len(encoded) + 2, ctypes.cast(buffer, ctypes.c_void_p))
    if allow_reparse:
        if parent is None:
            raise ValueError("Reparse-object opens require a retained parent handle")
        _component(name)
    attributes = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), parent,
                                   ctypes.pointer(text), 0x40 | (0 if allow_reparse else 0x1000),
                                   None, None)
    # OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE rejects reparses in every component.
    options = 0x20 | 0x200000  # FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_REPARSE_POINT
    if directory is not None:
        options |= 0x1 if directory else 0x40  # DIRECTORY_FILE / NON_DIRECTORY_FILE
    sharing = 0x1 | (0x2 if share_write else 0) | (0x4 if share_delete else 0)
    handle, status = HANDLE(), _IOStatusBlock()
    result = NtCreateFile(ctypes.byref(handle), access | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                          ctypes.byref(attributes), ctypes.byref(status), None, 0x80,
                          sharing, disposition, options, None, 0)
    if result < 0:
        error = ctypes.WinError(RtlNtStatusToDosError(result))
        error.filename = name
        raise error
    try:
        _check_filesystem(handle, name)
        return FileHandle(handle.value, allow_reparse=allow_reparse)
    except BaseException as error:
        CloseHandle(handle)
        if isinstance(error, OSError) and error.filename is None:
            error.filename = name
        raise


def open_path(path, access, *, share_delete=True, share_write=True, directory=None,
              disposition=FILE_OPEN, allow_reparse=False) -> FileHandle:
    """Open below a retained drive root, refusing filesystem reparses throughout."""
    normalized = normalize_path(path)
    if allow_reparse and len(normalized) > 3:
        # Cleanup may remove our ACE from a junction created by a payload. Open
        # only its final object relative to a pinned, non-reparse parent. This
        # never turns a link target into an ACL mutation target.
        parent_path, name = ntpath.split(normalized)
        with open_path(parent_path, FILE_READ_ATTRIBUTES, directory=True,
                       share_delete=False, share_write=False) as parent:
            return open_child(parent, name, access, share_delete=share_delete,
                              share_write=share_write, directory=directory,
                              disposition=disposition, allow_reparse=True)
    # Resolving a DOS-device symbolic link with QueryDosDevice is a host-only
    # namespace query under LPAC. A zero-access directory handle needs metadata
    # only; it neither grants directory listings nor bypasses child ACL checks.
    value = CreateFile(normalized[:3], 0, 0x7, None, 3, 0x02000000 | 0x00200000, None)
    if value == ctypes.c_void_p(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        error.filename = normalized[:3]
        raise error
    try:
        root = FileHandle(value)
    except BaseException:
        CloseHandle(value)
        raise
    if len(normalized) == 3:
        if directory is False or disposition != FILE_OPEN:
            root.close()
            raise _invalid(path, "the drive root requires an existing directory open")
        return root
    with root:
        if disposition != FILE_OPEN:
            _check_filesystem(root.value, normalized[:3])
        return _open(normalized[3:], access, parent=root.value, share_delete=share_delete,
                     share_write=share_write, directory=directory, disposition=disposition)


def open_child(parent: FileHandle, name: str, access, *, share_delete=True, share_write=True,
               directory=None, disposition=FILE_OPEN, allow_reparse=False) -> FileHandle:
    """Open one name relative to the retained parent object, never its old path."""
    _component(name)
    if not parent.value:
        raise ValueError("An open directory handle is required")
    attributes = parent.refresh_info().attributes
    if not attributes & FILE_ATTRIBUTE_DIRECTORY or attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("An open non-reparse directory handle is required")
    if disposition != FILE_OPEN:
        _check_filesystem(parent.value, name)
    return _open(name, access, parent=parent.value, share_delete=share_delete,
                 share_write=share_write, directory=directory, disposition=disposition,
                 allow_reparse=allow_reparse)



def rename_child(parent: FileHandle, source: FileHandle, name: str, *, replace: bool) -> None:
    """Atomically rename the retained staged file without changing any DACL.

    Staging in the target directory gives new files its inherited ACL, including
    active native execution grants. This worker never requests WRITE_DAC or a
    broker ACL mutex. Existing explicit per-file ACLs are not copied.
    """
    _component(name)
    encoded = name.encode("utf-16-le")
    data = ctypes.create_string_buffer(ctypes.sizeof(_RenameInfo) + len(encoded))
    info = _RenameInfo.from_buffer(data)
    # POSIX replacement remains possible while native ACL bookkeeping retains
    # the old inode. A previously missing target must still be missing.
    info.Flags = (0x1 | 0x2) if replace else 0
    info.RootDirectory = parent.value
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(data) + _RenameInfo.FileName.offset, encoded, len(encoded))
    status = _IOStatusBlock()
    result = NtSetInformationFile(source.value, ctypes.byref(status), data, len(data), 65)
    if result < 0:
        error = ctypes.WinError(RtlNtStatusToDosError(result))
        error.filename = name
        raise error


def remove_child(parent: FileHandle, name: str) -> None:
    """Remove the helper's staging entry relative to the retained parent."""
    with open_child(parent, name, 0x10000, directory=False, share_delete=False) as handle:
        disposition, status = ctypes.c_ubyte(1), _IOStatusBlock()
        result = NtSetInformationFile(handle.value, ctypes.byref(status), ctypes.byref(disposition),
                                      ctypes.sizeof(disposition), 13)
        if result < 0:
            error = ctypes.WinError(RtlNtStatusToDosError(result))
            error.filename = name
            raise error
