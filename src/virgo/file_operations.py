"""Bounded pipe protocol for the trusted, isolated filesystem worker.

The Broker opens only its trusted snapshot. Model-supplied paths remain request
data until the worker is inside the selected operating-system boundary.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from platformdirs import user_cache_dir

MAX_MESSAGE = 16 * 1024 * 1024
OPERATIONS = frozenset({"read", "write", "edit", "glob", "grep", "read_bytes", "walk_files", "read_context"})
# Capture before the first executor can release model work. Later executors
# must not reload source files that an editable-checkout task has changed.
_WORKER_SOURCES = tuple((name, Path(__file__).with_name(name).read_bytes())
                        for name in ("fs_worker.py", "fs_windows.py"))


def _windows_devices():
    """Capture launch facts independently of any model-provided filesystem path."""
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    drives = kernel.GetLogicalDrives
    drives.argtypes, drives.restype = [], ctypes.c_uint32
    query = kernel.QueryDosDeviceW
    query.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    query.restype = ctypes.c_uint32
    mask = drives()
    if not mask:
        raise ctypes.WinError(ctypes.get_last_error())
    devices = {}
    for index, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if not mask & (1 << index):
            continue
        name, target = letter + ":", ctypes.create_unicode_buffer(32768)
        if not query(name, target, len(target)):
            raise ctypes.WinError(ctypes.get_last_error())
        if re.fullmatch(r"\\Device\\HarddiskVolume[0-9]+", target.value, re.IGNORECASE):
            devices[name] = target.value
    return devices


def _windows_private_directory(path):
    """Create or verify a user-owned root without resetting active native ACEs."""
    import ctypes

    from .sandbox import windows_acl as win

    convert_sid = win._api(win.advapi, "ConvertSidToStringSidW", [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p),
    ])
    local_free = win._api(win.kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)

    def sid_text(sid):
        win._check(win.IsValidSid(sid))
        text = ctypes.c_wchar_p()
        win._check(convert_sid(sid, ctypes.byref(text)))
        try:
            return text.value
        finally:
            local_free(text)

    token = win.HANDLE()
    win._check(win.OpenProcessToken(win.GetCurrentProcess(), 8, ctypes.byref(token)))
    try:
        size = win.DWORD()
        win.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.create_string_buffer(size.value)
        win._check(win.GetTokenInformation(token, 1, user, len(user), ctypes.byref(size)))
        owner = sid_text(ctypes.c_void_p.from_buffer(user).value)
    finally:
        win.CloseHandle(token)

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", win.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", win.BOOL)]

    convert = win._api(win.advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [
        ctypes.c_wchar_p, win.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(win.DWORD),
    ])
    create = win._api(win.kernel, "CreateDirectoryW", [ctypes.c_wchar_p, ctypes.POINTER(SecurityAttributes)])
    descriptor = ctypes.c_void_p()
    win._check(convert(f"O:{owner}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{owner})",
                       1, ctypes.byref(descriptor), None))
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if not create(str(path), ctypes.byref(attributes)):
            error = ctypes.get_last_error()
            if error != 183:  # ERROR_ALREADY_EXISTS: validate, never replace it.
                raise ctypes.WinError(error)
    finally:
        local_free(descriptor)

    get_owner = win._api(win.advapi, "GetSecurityDescriptorOwner", [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(win.BOOL),
    ])
    # A single native open rejects reparse points in every component. The
    # descriptor is inspected on that retained object, not by reopening its path.
    with win.open_path(path, 0x20000, directory=True) as handle:  # READ_CONTROL
        size = win.DWORD()
        win.GetSecurity(handle.value, 1 | 4, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        security = ctypes.create_string_buffer(size.value)
        win._check(win.GetSecurity(handle.value, 1 | 4, security, len(security), ctypes.byref(size)))
        actual, defaulted = ctypes.c_void_p(), win.BOOL()
        win._check(get_owner(security, ctypes.byref(actual), ctypes.byref(defaulted)))
        policy = win._parse_descriptor(security)
        if sid_text(actual) != owner or not policy.control & 0x1000:
            raise PermissionError(f"Untrusted owner or inherited security on Virgo runtime directory: {path}")
        for ace in policy.aces:
            # Native commands may add temporary read grants and deny ACEs.
            # Preserve them, but reject mutation grants to unrelated identities.
            if ace[0] in {1, 6, 10, 12} or not struct.unpack_from("<I", ace, 4)[0] & 0x500D0156:
                continue
            sid = win._sid_in_ace(ace)
            if not sid or sid_text(ctypes.create_string_buffer(sid)) not in {owner, "S-1-5-18", "S-1-5-32-544"}:
                raise PermissionError(f"Non-private Virgo runtime directory: {path}")


def _private_directory(path):
    if os.name == "nt":
        _windows_private_directory(path)
        return
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError(f"Virgo runtime directory must be owned and private to this user: {path}")
    finally:
        os.close(descriptor)


def _runtime_root():
    """One durable, per-user namespace for current and future helper snapshots."""
    cache = Path(user_cache_dir())
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = cache.resolve(strict=True) / "virgo-runtime"
    _private_directory(root)
    return root


def _receive(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise ValueError("Incomplete filesystem worker response")
        data.extend(chunk)
    return bytes(data)


class FileOperations:
    def __init__(self):
        self._condition = threading.Condition()
        self._active = 0
        self._closed = False
        self._retained = False
        self._mutation = threading.Lock()
        self.home = str(Path.home())
        self.interpreter = str(Path(sys.executable).resolve(strict=True))
        # Reuse the Broker's original source bytes, including when a previous
        # executor has since allowed edits to the installation checkout.
        self.root = _runtime_root()
        self.directory = Path(tempfile.mkdtemp(prefix="snapshot-", dir=self.root)).resolve(strict=True)
        try:
            for name, content in _WORKER_SOURCES:
                target = self.directory / name
                target.write_bytes(content)
                target.chmod(0o400)
            if os.name == "nt":
                target = self.directory / "windows_devices.json"
                target.write_text(json.dumps(_windows_devices()), encoding="utf-8")
                target.chmod(0o400)
            self.directory.chmod(0o500)
        except BaseException:
            self.close()
            raise

    def close(self):
        from .execution import ExecutionFailed

        with self._condition:
            self._closed = True
            deadline = time.monotonic() + 10
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ExecutionFailed("Filesystem operations are still active; retained their trusted runtime")
                self._condition.wait(remaining)
            if self._retained:
                raise ExecutionFailed("Filesystem worker cleanup was incomplete; retained its trusted runtime")
        if self.directory.exists():
            self.directory.chmod(0o700)
            for file in self.directory.iterdir():
                file.chmod(0o600)
            shutil.rmtree(self.directory)

    def run(self, executor, operation, arguments, cwd, policy, cancellation):
        from .execution import ExecutionCancelled, ExecutionUnavailable

        if operation not in OPERATIONS or not isinstance(arguments, dict):
            raise ValueError("Unsupported filesystem operation")
        request = json.dumps({"version": 1, "operation": operation, "arguments": arguments,
                              "cwd": os.fspath(cwd), "home": self.home}, ensure_ascii=False).encode("utf-8")
        if len(request) > MAX_MESSAGE:
            raise ValueError("Filesystem operation exceeds the 16 MiB request limit")
        with self._condition:
            if self._closed:
                raise ExecutionUnavailable("Filesystem executor is closed")
            self._active += 1
        lock = self._mutation if operation in {"write", "edit"} else threading.Lock()
        acquired = False
        try:
            while not acquired:
                if cancellation is not None and cancellation.is_set():
                    raise ExecutionCancelled("Filesystem operation cancelled before launch")
                acquired = lock.acquire(timeout=0.05)
            return self._run(executor, operation, request, cwd, policy, cancellation)
        finally:
            if acquired:
                lock.release()
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def _run(self, executor, operation, request, cwd, policy, cancellation):
        from .execution import ExecutionCancelled, ExecutionFailed, ProcessRequest

        process_request = ProcessRequest(
            (self.interpreter, "-I", "-S", str(self.directory / "fs_worker.py"), "--stdio"),
            Path(cwd), dict(os.environ), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            runtime_paths=(self.directory,),
        )
        process = executor.run_process(process_request, policy, cancellation)
        errors, response, diagnostics, readers = [], [], bytearray(), []

        def send():
            try:
                for data in (struct.pack("!I", len(request)), request):
                    pending = memoryview(data)
                    while pending:
                        written = process.stdin.write(pending)
                        if not written:
                            raise OSError("Filesystem worker input closed")
                        pending = pending[written:]
                process.stdin.flush()
            except (OSError, ValueError) as error:
                errors.append(error)
            finally:
                try:
                    process.stdin.close()
                except (OSError, ValueError) as error:
                    errors.append(error)

        def receive():
            try:
                size = struct.unpack("!I", _receive(process.stdout, 4))[0]
                if size > MAX_MESSAGE:
                    raise ValueError("Filesystem worker exceeded its response limit")
                response.append(_receive(process.stdout, size))
                if process.stdout.read(1):
                    raise ValueError("Filesystem worker response contains trailing bytes")
            except (OSError, ValueError) as error:
                errors.append(error)

        def diagnose():
            try:
                while chunk := process.stderr.read(4096):
                    diagnostics.extend(chunk)
                    del diagnostics[:-8192]
            except (OSError, ValueError) as error:
                errors.append(error)

        try:
            for function in (send, receive, diagnose):
                reader = threading.Thread(target=function, name="virgo-files-io", daemon=True)
                reader.start()
                readers.append(reader)
            deadline = time.monotonic() + 120
            while process.poll() is None:
                if cancellation is not None and cancellation.is_set():
                    failure = ExecutionCancelled("Filesystem operation cancelled; changes may already exist")
                    failure.boundary = {"payload_started": True,
                                        "side_effects": "possible" if operation in {"write", "edit"} else "none"}
                    raise failure
                if errors or time.monotonic() >= deadline:
                    raise ExecutionFailed("Filesystem worker protocol failed or timed out; do not replay it")
                time.sleep(0.01)
            process.wait(timeout=5)
            process.cleanup_tree()
            for reader in readers:
                reader.join(timeout=5)
            if any(reader.is_alive() for reader in readers):
                raise ExecutionFailed("Filesystem worker streams did not drain after cleanup")
            if process.returncode or errors or not response:
                detail = bytes(diagnostics).decode("utf-8", errors="replace").strip()
                raise ExecutionFailed(f"Filesystem worker failed (exit {process.returncode}): {detail or errors}; do not replay it")
            try:
                result = json.loads(response[0])
            except (ValueError, UnicodeError) as error:
                raise ExecutionFailed("Filesystem worker returned invalid JSON; do not replay it") from error
            if (not isinstance(result, dict) or result.get("version") != 1
                    or type(result.get("ok")) is not bool or not isinstance(result.get("details", {}), dict)):
                raise ExecutionFailed("Filesystem worker returned an invalid protocol envelope")
            result["boundary"] = getattr(process, "_virgo_boundary", None) or {
                "provider": "host", "state": "established", "payload_started": True,
                "side_effects": "possible" if operation in {"write", "edit"} else "none",
                "cleanup": "executor_completed",
            }
            return result
        finally:
            try:
                process.cleanup_tree()
                for reader in readers:
                    reader.join(timeout=5)
                if any(reader.is_alive() for reader in readers):
                    raise ExecutionFailed("Filesystem worker I/O cleanup is incomplete")
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            except BaseException:
                self._retained = True
                raise
