"""Platform-neutral execution contracts and direct host operations.

This module must not import an optional isolation implementation. A context is
captured for its originating task and passed unchanged to deferred operations.
"""
from __future__ import annotations

import io
import os
import subprocess
from contextlib import contextmanager
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
    def walk_files(self, path: Path, ignore: set[str]): ...
    def open_output(self, cwd: Path, prefix: str): ...
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
    """Uniform process handle; POSIX Host cleanup covers its live process group."""

    def __init__(self, process):
        self.process = process
        self.pid = process.pid

    def __getattr__(self, name):
        return getattr(self.process, name)

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout)

    def kill_tree(self):
        from .tools.bash import kill_tree

        kill_tree(self.process)

    def cleanup_tree(self):
        if self.process.poll() is None:
            self.kill_tree()
            self.process.wait(timeout=5)


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

    def walk_files(self, path: Path, ignore: set[str]):
        for root, dirs, files in os.walk(path):
            dirs[:] = [name for name in dirs if name not in ignore]
            for name in files:
                file = Path(root) / name
                yield file, file.stat() if file.exists() else None

    def validate(self, cwd: Path) -> Path:
        return cwd

    def open_output(self, cwd: Path, prefix: str):
        from .tools.output import open_output

        return open_output(self.workspace, prefix)

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
