"""Bounded pipe protocol for the trusted, isolated filesystem worker.

The Broker opens only its trusted snapshot. Model-supplied paths remain request
data until the worker is inside the selected operating-system boundary.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

MAX_MESSAGE = 16 * 1024 * 1024
OPERATIONS = frozenset({"read", "write", "edit", "glob", "grep", "read_bytes", "walk_files"})


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
        # Capture installation code before the first model-controlled operation,
        # including editable installs running from their own project.
        self.directory = Path(tempfile.mkdtemp(prefix="virgo-runtime-")).resolve(strict=True)
        try:
            for name in ("fs_worker.py", "fs_windows.py"):
                content = Path(__file__).with_name(name).read_bytes()
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
