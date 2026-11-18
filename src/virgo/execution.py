"""Platform-neutral execution contracts and direct host operations.

This module must not import an optional isolation implementation. A context is
captured for its originating task and passed unchanged to deferred operations.
"""
from __future__ import annotations

import errno
import io
import os
import signal
import subprocess
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ExecutionUnavailable(RuntimeError):
    """The requested boundary could not be established before payload release."""

    def __init__(self, message, *, reason=None, dimension=None, target=None):
        super().__init__(message)
        self.reason, self.dimension, self.target = reason, dimension, target


class ExecutionFailed(RuntimeError):
    """Execution/control failed after release; effects may already exist."""


class ExecutionCancelled(InterruptedError):
    pass


class FileAccessDenied(PermissionError):
    def __init__(self, message, *, dimension="filesystem", target=None):
        super().__init__(message)
        self.dimension = dimension
        self.target = str(target) if target is not None else None


class ProcessHandle(Protocol):
    returncode: int | None

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill_tree(self) -> None: ...
    def cleanup_tree(self) -> None: ...


class ExecutionContext(Protocol):
    provider: str
    workspace: Path

    def check_read(self, path: Path) -> None: ...
    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes | None: ...
    def open_mutation(self, path: Path, *, create: bool): ...
    def walk_files(self, path: Path, ignore: set[str], *, max_depth: int | None = None): ...
    def open_output(self, cwd: Path, prefix: str): ...
    def read_output_tail(self, path: Path, max_bytes: int) -> tuple[bytes, int]: ...
    def spawn(self, argv, cwd, env, stdout, output_path=None, cancellation=None): ...
    def describe(self) -> dict: ...


class ExecutionProvider(Protocol):
    name: str

    def capture_runtime(self, runtime) -> None: ...
    def for_context(self, context) -> ExecutionContext: ...
    def preflight(self, event): ...
    def before_execute(self, event): ...
    def instructions(self, runtime) -> str: ...
    def status(self, session, args): ...


class HostProcess:
    """Own a POSIX process group until its leader is reaped."""

    def __init__(self, process):
        self._process = process
        self.pid = process.pid
        self.stdout = process.stdout
        self._lock = threading.RLock()
        self._cleanup_error = None

    @property
    def returncode(self):
        return self._process.returncode

    def _leader_exited(self):
        # Leave the child waitable. Its PID pins the group ID until our sole
        # reaper has signalled surviving members, including on normal exit.
        if hasattr(os, "waitid"):
            return os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None

        # Python 3.11/3.12 on macOS do not expose waitid. kqueue observes
        # exit without reaping and reports ESRCH if the child is already dead.
        import select

        event = select.kevent(self.pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD,
                              fflags=select.KQ_NOTE_EXIT)
        with closing(select.kqueue()) as queue:
            ready = queue.control([event], 1, 0)
        if ready and ready[0].flags & select.KQ_EV_ERROR and ready[0].data != errno.ESRCH:
            raise OSError(ready[0].data, os.strerror(ready[0].data))
        return bool(ready)

    def poll(self):
        with self._lock:
            if self._cleanup_error is not None:
                raise self._cleanup_error
            if self.returncode is not None or not self._leader_exited():
                return self.returncode
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as error:
                # Darwin reports EPERM for a group containing only the zombie
                # leader. After releasing it, only probe: never signal a reused ID.
                self._process.wait()
                try:
                    os.killpg(self.pid, 0)
                except ProcessLookupError:
                    return self.returncode
                except PermissionError:
                    pass
                self._cleanup_error = error
                raise
            return self._process.wait()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self._process.args, timeout)
            time.sleep(0.01)
        return self.returncode

    def _signal_running(self, sig):
        with self._lock:
            if self.poll() is not None:
                return
            try:
                os.killpg(self.pid, sig)
            except (ProcessLookupError, PermissionError) as error:
                # Darwin can stop accepting signals before a dying child is
                # waitable, including when a concurrent caller already killed it.
                try:
                    self.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    raise error from None

    def kill_tree(self):
        self._signal_running(signal.SIGTERM)
        try:
            self.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._signal_running(signal.SIGKILL)

    def cleanup_tree(self):
        if self.poll() is None:
            self.kill_tree()
            self.wait(timeout=5)


@dataclass(frozen=True)
class HostContext:
    workspace: Path
    provider: str = "host"

    def check_read(self, path: Path) -> None:
        pass

    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes | None:
        if max_bytes is not None and path.stat().st_size > max_bytes:
            return None
        with path.open("rb") as stream:
            return stream.read() if max_bytes is None else stream.read(max_bytes + 1)

    @contextmanager
    def open_mutation(self, path: Path, *, create: bool):
        existed = path.exists()
        with io.BytesIO(b"" if create else path.read_bytes()) as stream:
            def commit():
                if create:
                    path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(stream.getvalue())
            yield stream, existed, commit

    def walk_files(self, path: Path, ignore: set[str], *, max_depth: int | None = None):
        for root, dirs, files in os.walk(path):
            dirs[:] = [name for name in dirs if name not in ignore]
            if max_depth is not None and len(Path(root).relative_to(path).parts) >= max_depth - 1:
                dirs.clear()
            for name in files:
                file = Path(root) / name
                yield file, file.stat() if file.exists() else None

    def validate(self, cwd: Path) -> Path:
        return cwd

    def open_output(self, cwd: Path, prefix: str):
        from .tools.output import open_output

        return open_output(self.workspace, prefix)

    def read_output_tail(self, path: Path, max_bytes: int) -> tuple[bytes, int]:
        with path.open("rb") as stream:
            total = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, total - max_bytes))
            return stream.read(max_bytes), total

    def spawn(self, argv, cwd, env, stdout, output_path=None, cancellation=None):
        if cancellation is not None and cancellation.is_set():
            raise ExecutionCancelled("Execution cancelled before process start")
        if os.name == "nt":
            from .tools.windows_process import WindowsProcess

            return WindowsProcess(argv, cwd=cwd, env=env, stdout=stdout)
        return HostProcess(subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=subprocess.STDOUT, start_new_session=True))

    def describe(self) -> dict:
        return {"provider": "host", "state": "host", "workspace": str(self.workspace),
                "requested": "host", "effective": "host operating-system authority"}


def cleanup_process(proc) -> None:
    cleanup = getattr(proc, "cleanup_tree", None)
    if cleanup is not None:
        cleanup()
