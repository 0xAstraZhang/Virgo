"""Per-execution Windows file grants, removed only after the Job is drained.

ACL edits target retained handles, never link targets. The low-level setter does
not request a recursive filesystem ACL update; existing objects are visited
individually and new objects inherit the temporary entries from their parents.
"""
from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
import struct
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .platform import SandboxCancelled, SandboxProcessError, SandboxUnavailable
from .windows_files import (
    BOOL,
    DWORD,
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    HANDLE,
    WORD,
    CloseHandle,
    FileHandle,
    _api,
    _check,
    kernel,
    normalize_path,
    open_path,
)

advapi = ctypes.WinDLL("advapi32", use_last_error=True)
GetCurrentProcess = _api(kernel, "GetCurrentProcess", [], HANDLE)
CreateMutex = _api(kernel, "CreateMutexW", [ctypes.c_void_p, BOOL, ctypes.c_wchar_p], HANDLE)
WaitForSingleObject = _api(kernel, "WaitForSingleObject", [HANDLE, DWORD], DWORD)
ReleaseMutex = _api(kernel, "ReleaseMutex", [HANDLE])
OpenProcessToken = _api(advapi, "OpenProcessToken", [HANDLE, DWORD, ctypes.POINTER(HANDLE)])
GetTokenInformation = _api(advapi, "GetTokenInformation", [
    HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD),
])
IsValidSid = _api(advapi, "IsValidSid", [ctypes.c_void_p])
GetLengthSid = _api(advapi, "GetLengthSid", [ctypes.c_void_p], DWORD)
GetSecurity = _api(advapi, "GetKernelObjectSecurity", [
    HANDLE, DWORD, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD),
])
GetDacl = _api(advapi, "GetSecurityDescriptorDacl", [
    ctypes.c_void_p, ctypes.POINTER(BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(BOOL),
])
GetControl = _api(advapi, "GetSecurityDescriptorControl", [
    ctypes.c_void_p, ctypes.POINTER(WORD), ctypes.POINTER(DWORD),
])
InitializeDescriptor = _api(advapi, "InitializeSecurityDescriptor", [ctypes.c_void_p, DWORD])
SetDacl = _api(advapi, "SetSecurityDescriptorDacl", [ctypes.c_void_p, BOOL, ctypes.c_void_p, BOOL])
SetControl = _api(advapi, "SetSecurityDescriptorControl", [ctypes.c_void_p, WORD, WORD])
SetSecurity = _api(advapi, "SetKernelObjectSecurity", [HANDLE, DWORD, ctypes.c_void_p])


class _GenericMapping(ctypes.Structure):
    _fields_ = [("read", DWORD), ("write", DWORD), ("execute", DWORD), ("all", DWORD)]


CreatePrivateObjectSecurityEx = _api(advapi, "CreatePrivateObjectSecurityEx", [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    BOOL, DWORD, HANDLE, ctypes.POINTER(_GenericMapping),
])
DestroyPrivateObjectSecurity = _api(advapi, "DestroyPrivateObjectSecurity", [
    ctypes.POINTER(ctypes.c_void_p),
])

_THREAD_LOCK = threading.RLock()
_ACL_ACCESS = 0x20000 | 0x40000  # READ_CONTROL | WRITE_DAC, no content access
_READ = 0x1200A9  # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
_WRITE = _READ | 0x116  # DATA/APPEND/EA/ATTRIBUTES, never DELETE_CHILD
_DELETE = 0x10000
_ADMIN = 0xC0040  # WRITE_DAC | WRITE_OWNER | FILE_DELETE_CHILD
_MUTATE = _ADMIN | _DELETE | 0x116


def _user_mutex_name():
    token = HANDLE()
    _check(OpenProcessToken(GetCurrentProcess(), 8, ctypes.byref(token)))
    try:
        size = DWORD()
        GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        data = ctypes.create_string_buffer(size.value)
        _check(GetTokenInformation(token, 1, data, len(data), ctypes.byref(size)))
        sid = ctypes.c_void_p.from_buffer(data).value
        _check(IsValidSid(sid))
        digest = hashlib.sha256(ctypes.string_at(sid, GetLengthSid(sid))).hexdigest()
        return "Global\\Virgo.NativeSandboxAcl." + digest
    finally:
        CloseHandle(token)


@contextmanager
def acl_lock(*, mutex=None, cancellation=None):
    """Serialize native ACL snapshots, commits and cleanup across brokers."""
    owned = mutex is None
    if owned:
        mutex = _check(CreateMutex(None, False, _user_mutex_name()))
    try:
        deadline = time.monotonic() + 10
        acquired = False
        while not _THREAD_LOCK.acquire(timeout=0.05):
            if cancellation is not None:
                cancellation()
            if time.monotonic() >= deadline:
                raise OSError("Timed out waiting for the Windows ACL broker lock")
        try:
            while not acquired:
                if cancellation is not None:
                    cancellation()
                status = WaitForSingleObject(mutex, 50)
                if status in {0, 0x80}:  # ownership also transfers after an abandoned mutex
                    acquired = True
                elif status != 258:
                    raise ctypes.WinError(ctypes.get_last_error())
                elif time.monotonic() >= deadline:
                    raise OSError("Timed out waiting for the Windows ACL broker mutex")
            yield
        finally:
            try:
                if acquired:
                    _check(ReleaseMutex(mutex))
            finally:
                _THREAD_LOCK.release()
    finally:
        if owned:
            _check(CloseHandle(mutex))


@dataclass
class _Dacl:
    revision: int
    control: int
    defaulted: bool
    aces: list[bytes]


def _descriptor(handle, getter=GetSecurity):
    size = DWORD()
    getter(handle, 4, None, 0, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    data = ctypes.create_string_buffer(size.value)
    _check(getter(handle, 4, data, len(data), ctypes.byref(size)))
    return data


def _parse_descriptor(data, *, cleanup=False) -> _Dacl | None:
    present, defaulted, acl = BOOL(), BOOL(), ctypes.c_void_p()
    _check(GetDacl(data, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)))
    if not present.value or not acl.value:
        if cleanup:
            # A payload may supply a protected null DACL when creating an
            # ordinary file. It contains no execution SID entries to revoke.
            return None
        raise OSError("Windows sandbox grants require an existing non-null DACL")
    control, revision = WORD(), DWORD()
    _check(GetControl(data, ctypes.byref(control), ctypes.byref(revision)))
    header = ctypes.string_at(acl, 8)
    acl_revision, _, length, count, _ = struct.unpack("<BBHHH", header)
    raw = ctypes.string_at(acl, length)
    aces, position = [], 8
    for _ in range(count):
        if position + 4 > len(raw):
            raise OSError("Malformed Windows DACL")
        length = struct.unpack_from("<H", raw, position + 2)[0]
        if length < 4 or position + length > len(raw):
            raise OSError("Malformed Windows ACE")
        aces.append(raw[position:position + length])
        position += length
    return _Dacl(acl_revision, control.value, bool(defaulted.value), aces)


def _read(handle, *, cleanup=False, getter=GetSecurity) -> _Dacl | None:
    return _parse_descriptor(_descriptor(handle, getter), cleanup=cleanup)


def inherited_file_dacl(parent):
    """Ask Windows for a new file's normal destination-parent inheritance."""
    token, descriptor = HANDLE(), ctypes.c_void_p()
    _check(OpenProcessToken(GetCurrentProcess(), 8, ctypes.byref(token)))  # TOKEN_QUERY
    try:
        # FILE_GENERIC_READ/WRITE/EXECUTE and FILE_ALL_ACCESS. Native inheritance
        # maps generic masks and Creator Owner/Group; do not hand-roll ACE rules.
        mapping = _GenericMapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
        _check(CreatePrivateObjectSecurityEx(_descriptor(parent), None, ctypes.byref(descriptor),
                                             None, False, 1, token, ctypes.byref(mapping)))
        try:
            return _parse_descriptor(descriptor)
        finally:
            _check(DestroyPrivateObjectSecurity(ctypes.byref(descriptor)))
    finally:
        _check(CloseHandle(token))


@dataclass
class _PreparedDacl:
    acl: object  # Keep the buffer referenced by the absolute descriptor alive.
    descriptor: object
    information: int

    def apply(self, handle, *, setter=SetSecurity):
        _check(setter(handle, self.information, self.descriptor))


def _prepare_dacl(previous: _Dacl, aces: list[bytes], control: int):
    length = 8 + sum(map(len, aces))
    if length > 65535 or len(aces) > 65535:
        raise OSError("Windows sandbox grants exceed the native DACL size limit")
    acl = ctypes.create_string_buffer(struct.pack("<BBHHH", previous.revision, 0,
                                                  length, len(aces), 0) + b"".join(aces))
    # An absolute SECURITY_DESCRIPTOR occupies 40 bytes on 64-bit Windows.
    descriptor = ctypes.create_string_buffer(64)
    _check(InitializeDescriptor(descriptor, 1))
    _check(SetDacl(descriptor, True, acl, previous.defaulted))
    _check(SetControl(descriptor, 0x1500, control & 0x1500))
    protection = 0x80000000 if control & 0x1000 else 0x20000000
    return _PreparedDacl(acl, descriptor, 4 | protection)


def _write(handle, previous: _Dacl, aces: list[bytes], control: int, *, setter=SetSecurity):
    _prepare_dacl(previous, aces, control).apply(handle, setter=setter)


def _sid_in_ace(ace: bytes):
    if ace[0] in {0, 1, 2, 3, 9, 10, 13, 14}:
        offset = 8
    elif ace[0] in {5, 6, 7, 8, 11, 12, 15, 16} and len(ace) >= 12:
        flags = struct.unpack_from("<I", ace, 8)[0]
        offset = 12 + (16 if flags & 1 else 0) + (16 if flags & 2 else 0)
    else:
        return None
    if offset + 8 > len(ace) or ace[offset] != 1:
        return None
    end = offset + 8 + 4 * ace[offset + 1]
    return ace[offset:end] if end <= len(ace) else None


def _ace(kind: int, flags: int, mask: int, sid: bytes):
    return struct.pack("<BBHI", kind, flags, 8 + len(sid), mask) + sid


def _key(path):
    return ntpath.normcase(normalize_path(path))


def _inside(path: str, root: str):
    return path == root or path.startswith(root.rstrip("\\") + "\\")


@dataclass
class _Grant:
    path: Path
    handle: FileHandle
    writable: bool
    delete_allowed: bool
    original_control: int | None = None
    written_control: int | None = None


class AclGrants:
    """Temporary package/read and restricting-SID/write grants for one launch."""

    def __init__(self, package_sid: bytes, write_sid: bytes, cancellation=None):
        self.sids = (bytes(package_sid), bytes(write_sid))
        for sid in self.sids:
            data = ctypes.create_string_buffer(sid)
            if (len(sid) < 8 or len(sid) != 8 + sid[1] * 4
                    or not IsValidSid(data) or GetLengthSid(data) != len(sid)):
                raise SandboxUnavailable("Invalid Windows sandbox grant SID")
        if self.sids[0] == self.sids[1]:
            raise SandboxUnavailable("Package and restricting identities must be distinct")
        self.cancellation = cancellation
        self._grants = {}
        self._owned = set()
        self._roots = []
        try:
            self._mutex = _check(CreateMutex(None, False, _user_mutex_name()))
        except OSError as error:
            raise SandboxUnavailable(f"Could not initialize Windows ACL ownership: {error}") from error
        self._finalizer = weakref.finalize(self, CloseHandle, self._mutex)

    def _cancel(self):
        if self.cancellation is not None and self.cancellation.is_set():
            raise SandboxCancelled("Windows sandbox setup was cancelled")

    def _locked(self, *, cleanup=False, mutex=None):
        mutex = self._mutex if mutex is None else mutex
        if mutex is None:
            raise OSError("Windows ACL ownership has already been released")
        return acl_lock(mutex=mutex,
                        cancellation=None if cleanup else self._cancel)

    def _walk(self, root, excluded, *, cleanup=False, skip_reparse=False):
        root = Path(root)
        pending = [root]
        while pending:
            if not cleanup:
                self._cancel()
            path = pending.pop()
            if any(_inside(_key(path), item) for item in excluded):
                continue
            try:
                handle = open_path(path, _ACL_ACCESS,
                                   allow_reparse=cleanup or (skip_reparse and path != root))
            except FileNotFoundError:
                if cleanup:
                    continue
                raise
            directory = bool(handle.info.attributes & FILE_ATTRIBUTE_DIRECTORY)
            reparse = bool(handle.info.attributes & FILE_ATTRIBUTE_REPARSE_POINT)
            if reparse and skip_reparse:
                # Read-only runtimes can contain aliases such as python3.exe.
                # Do not mutate the alias, follow it or grant its target.
                handle.close()
                continue
            yield path, handle
            if directory and not reparse:
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)

    def _update(self, grant: _Grant, *, cleanup=False):
        with self._locked(cleanup=cleanup):
            previous = _read(grant.handle.value, cleanup=cleanup)
            if previous is None:
                return
            retained = [ace for ace in previous.aces if _sid_in_ace(ace) not in self.sids]
            if cleanup:
                if len(retained) == len(previous.aces):
                    return
                control = previous.control
                if control == grant.written_control and grant.original_control is not None:
                    control = grant.original_control
                _write(grant.handle.value, previous, retained, control)
                return
            if grant.original_control is None:
                grant.original_control = previous.control
            flags = 3 if grant.handle.info.attributes & FILE_ATTRIBUTE_DIRECTORY else 0
            deny = [_ace(1, flags, _ADMIN if grant.writable else _MUTATE, self.sids[1])]
            if grant.writable and not grant.delete_allowed:
                deny.append(_ace(1, 0, _DELETE, self.sids[1]))
            mask = _WRITE if grant.writable else _READ
            if grant.delete_allowed:
                mask |= _DELETE
            allow = [_ace(0, flags, mask, sid) for sid in self.sids]
            if flags and grant.writable and not grant.delete_allowed:
                allow.extend(_ace(0, flags | 8, _DELETE, sid) for sid in self.sids)
            _write(grant.handle.value, previous, deny + retained + allow, previous.control)
            grant.written_control = _read(grant.handle.value).control

    def grant_tree(self, root: Path, writable: bool, protected: tuple[Path, ...] = (),
                   reject_hardlinks: bool = True, excluded: tuple[Path, ...] = (),
                   skip_reparse: bool = False):
        pending = []
        try:
            if not self._mutex:
                raise SandboxUnavailable("Windows file grants have already been released")
            if writable and skip_reparse:
                raise SandboxUnavailable("Writable sandbox grants must reject every reparse point")
            root = Path(normalize_path(root))
            root_key = _key(root)
            protected_keys, excluded_keys = tuple(map(_key, protected)), tuple(map(_key, excluded))
            # Preflight the complete tree before its first ACL mutation. Retain
            # every opened object; path replacements cannot redirect later edits.
            for path, handle in self._walk(root, excluded_keys, skip_reparse=skip_reparse):
                pending.append((path, handle))
                if reject_hardlinks and not handle.info.attributes & FILE_ATTRIBUTE_DIRECTORY:
                    if handle.info.nlinks != 1:
                        raise SandboxUnavailable(f"Sandbox file grants must not be hard-linked: {path}")
            self._roots.append((root, excluded_keys))
            for path, handle in pending:
                self._cancel()
                info = handle.refresh_info()
                if reject_hardlinks and not info.attributes & FILE_ATTRIBUTE_DIRECTORY and info.nlinks != 1:
                    raise SandboxUnavailable(f"Sandbox file changed into a hard link: {path}")
                key = _key(path)
                can_write = writable and not any(_inside(key, item) for item in protected_keys)
                can_delete = can_write and key != root_key and not any(
                    _inside(item, key) for item in protected_keys
                )
                grant = self._grants.get(handle.identity)
                if grant is None:
                    grant = _Grant(path, handle, can_write, can_delete)
                    self._grants[handle.identity] = grant
                    self._owned.add(handle)
                else:
                    grant.writable = grant.writable and can_write
                    grant.delete_allowed = grant.delete_allowed and can_delete
                self._update(grant)
        except (SandboxCancelled, SandboxUnavailable):
            raise
        except OSError as error:
            raise SandboxUnavailable(f"Could not establish Windows file grants: {error}") from error
        finally:
            for _, handle in pending:
                if handle not in self._owned:
                    handle.close()

    def cleanup(self):
        """Remove only this launch's SIDs, preserving all other current ACEs."""
        if not self._mutex:
            return
        errors = []
        # Clear existing parent grants first, stopping new inheritance before
        # discovering files created by this or concurrent sandbox commands.
        for grant in self._grants.values():
            try:
                self._update(grant, cleanup=True)
            except OSError as error:
                errors.append(f"{grant.path}: {error}")
        seen = set()
        for root, excluded in self._roots:
            try:
                walk = self._walk(root, excluded, cleanup=True)
                while True:
                    # Open and revoke under the same lock as Core replacement.
                    # Otherwise Core could copy a live ACE to a new inode after
                    # this walk opened the old inode, leaving the new ACE behind.
                    with self._locked(cleanup=True):
                        item = next(walk, None)
                        if item is None:
                            break
                        path, handle = item
                        with handle:
                            if handle.identity not in seen:
                                self._update(_Grant(path, handle, False, False), cleanup=True)
                                seen.add(handle.identity)
            except OSError as error:
                errors.append(f"{root}: {error}")
        if errors:
            raise SandboxProcessError("Windows ACL cleanup could not be verified: " + "; ".join(errors[:5]))
        try:
            for handle in tuple(self._owned):
                handle.close()
                self._owned.remove(handle)
                self._grants.pop(handle.identity, None)
            self._roots.clear()
            _check(CloseHandle(self._mutex))
            self._finalizer.detach()
            self._mutex = None
        except OSError as error:
            raise SandboxProcessError(f"Windows ACL handles could not be released: {error}") from error
