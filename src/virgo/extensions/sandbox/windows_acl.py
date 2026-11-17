"""Preserve destination permissions during checked Windows file replacement.

This module does not grant process tokens or modify directory ACLs for a native
job. Security descriptors are applied to retained staging-file handles only.
"""
from __future__ import annotations

import ctypes
import hashlib
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .windows_files import (
    BOOL,
    DWORD,
    HANDLE,
    WORD,
    CloseHandle,
    _api,
    _check,
    kernel,
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
    """Serialize file permission snapshots and replacement across brokers."""
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


def _parse_descriptor(data) -> _Dacl:
    present, defaulted, acl = BOOL(), BOOL(), ctypes.c_void_p()
    _check(GetDacl(data, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)))
    if not present.value or not acl.value:
        raise OSError("Protected file replacement requires an existing non-null DACL")
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


def _read(handle, *, getter=GetSecurity) -> _Dacl:
    return _parse_descriptor(_descriptor(handle, getter))


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
        raise OSError("File permissions exceed the native DACL size limit")
    acl = ctypes.create_string_buffer(struct.pack("<BBHHH", previous.revision, 0,
                                                  length, len(aces), 0) + b"".join(aces))
    # An absolute SECURITY_DESCRIPTOR occupies 40 bytes on 64-bit Windows.
    descriptor = ctypes.create_string_buffer(64)
    _check(InitializeDescriptor(descriptor, 1))
    _check(SetDacl(descriptor, True, acl, previous.defaulted))
    _check(SetControl(descriptor, 0x1500, control & 0x1500))
    protection = 0x80000000 if control & 0x1000 else 0x20000000
    return _PreparedDacl(acl, descriptor, 4 | protection)
