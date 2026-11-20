"""Core execution contracts, process ownership and the common executor.

A context is captured for its originating task and passed unchanged to deferred
operations. Native backend discovery is lazy so explicit No-Limit execution does
not depend on an isolation backend being installed.
"""
from __future__ import annotations

import errno
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Protocol

from .execution_policy import (
    HOST_MODE,
    ExecutionMode,
    PermissionPolicy,
    PermissionSnapshot,
    is_native_windows,
)
from .output_store import OutputStore, private_output_store

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    stdin: object
    stdout: object
    stderr: object

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill_tree(self) -> None: ...
    def cleanup_tree(self) -> None: ...


@dataclass(frozen=True)
class ProcessRequest:
    """An argv-based process operation; permission decisions are supplied separately."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    stdout: object = subprocess.PIPE
    stdin: object = subprocess.DEVNULL
    stderr: object = subprocess.STDOUT
    env_overrides: dict[str, str] = field(default_factory=dict)


def environment_overrides(values: dict[str, str]) -> dict[str, str]:
    """Validate explicit adapter grants without letting them alter launcher setup.

    Ambient credentials are still filtered by native backends. These additions
    must come from trusted configuration, never a model-supplied environment.
    """
    reserved = {"HOME", "TMPDIR", "TMP", "TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                "HOMEDRIVE", "HOMEPATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "ENV", "BASH_ENV"}
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str) or not _ENV_NAME.fullmatch(name):
            raise ExecutionUnavailable("Explicit environment grants require valid identifier names and string values",
                                       reason="invalid_environment")
        if any(ord(ch) < 0x20 and ch not in "\t\n\r" for ch in value):
            raise ExecutionUnavailable(f"Explicit environment value for {name} contains invalid control characters",
                                       reason="invalid_environment")
        if name.upper() in reserved or name.upper().startswith(("VIRGO", "LD_", "DYLD_", "PYTHON")):
            raise ExecutionUnavailable(f"Explicit environment cannot replace launcher variable {name}",
                                       reason="invalid_environment")
    return dict(values)


def _merge_environment(base: dict[str, str], overrides: dict[str, str], *,
                       case_insensitive: bool = os.name == "nt") -> dict[str, str]:
    if not overrides:
        return dict(base)
    if case_insensitive:
        override_keys = {key.upper() for key in overrides}
        merged = {key: value for key, value in base.items() if key.upper() not in override_keys}
        merged.update(overrides)
        return merged
    return {**base, **overrides}


class SandboxExecutor:
    """Establish the selected boundary before releasing any local payload."""

    @cached_property
    def native(self):
        from .sandbox.native import NativeExecutor

        return NativeExecutor()

    @property
    def backend(self):
        # A description is not a capability probe. Discovery and trust checks
        # belong to launch, not prompt construction or displaying permissions.
        return {"linux": "bubblewrap", "darwin": "seatbelt", "win32": "windows-lpac"}.get(sys.platform, "unsupported")

    @property
    def identity(self):
        return (id(self), self.backend)

    def policy(self, context):
        return self.native.policy(context)

    def run_process(self, request: ProcessRequest, context=None, cancellation=None) -> ProcessHandle:
        if cancellation is not None and cancellation.is_set():
            raise ExecutionCancelled("Execution cancelled before process start")
        if not request.argv or not all(isinstance(part, str) for part in request.argv) or not request.argv[0]:
            raise ExecutionUnavailable("Process launch requires a nonempty command argv", reason="invalid_argv")
        if context is not None and not context.policy.unrestricted:
            return self.native.run_process(request, context, cancellation)
        env = _merge_environment(request.env, environment_overrides(request.env_overrides))
        if os.name == "nt":
            from .tools.windows_process import WindowsProcess

            return WindowsProcess(request.argv, cwd=request.cwd, env=env, stdin=request.stdin,
                                  stdout=request.stdout, stderr=request.stderr)
        return HostProcess(subprocess.Popen(
            request.argv, cwd=str(request.cwd), env=env,
            stdin=request.stdin, stdout=request.stdout, stderr=request.stderr, start_new_session=True,
        ))


class ExecutionContext(Protocol):
    provider: str
    workspace: Path
    policy: PermissionPolicy
    outputs: OutputStore

    def check_read(self, path: Path) -> None: ...
    def check_write(self, path: Path) -> None: ...
    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes | None: ...
    def open_mutation(self, path: Path, *, create: bool, max_read_bytes: int | None = None): ...
    def walk_files(self, path: Path, ignore: set[str], *, max_depth: int | None = None,
                   max_files: int | None = None): ...
    def open_output(self, cwd: Path, prefix: str): ...
    def run_process(self, request: ProcessRequest, cancellation=None) -> ProcessHandle: ...
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
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
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
                # Darwin can report EPERM while an exiting group is still
                # observable. After reaping the leader, only probe for exit.
                self._process.wait()
                deadline = time.monotonic() + 1
                while True:
                    try:
                        os.killpg(self.pid, 0)
                    except ProcessLookupError:
                        return self.returncode
                    except PermissionError:
                        pass
                    if time.monotonic() >= deadline:
                        self._cleanup_error = error
                        raise
                    time.sleep(0.01)
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
    executor: SandboxExecutor = field(default_factory=SandboxExecutor, compare=False, repr=False)
    resources: ExecutionMode = HOST_MODE
    outputs: OutputStore = field(default_factory=private_output_store, compare=False, repr=False)

    @property
    def policy(self) -> PermissionPolicy:
        return self.resources.policy

    @property
    def network_allowed(self) -> bool:
        return True

    @classmethod
    def unrestricted(cls, workspace: Path, executor: SandboxExecutor | None = None,
                     outputs: OutputStore | None = None) -> HostContext:
        return cls(
            Path(workspace).resolve(),
            executor=executor or SandboxExecutor(),
            outputs=outputs or private_output_store(),
        )

    def check_read(self, path: Path) -> None:
        pass

    def check_write(self, path: Path) -> None:
        pass

    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes | None:
        from .file_access import read_bytes

        return read_bytes(path, self.policy, max_bytes=max_bytes)

    def open_mutation(self, path: Path, *, create: bool, max_read_bytes: int | None = None):
        from .file_access import open_mutation

        return open_mutation(path, self.policy, create=create, max_read_bytes=max_read_bytes)

    def walk_files(self, path: Path, ignore: set[str], *, max_depth: int | None = None,
                   max_files: int | None = None):
        from .file_access import MAX_WALK_FILES, walk_files

        return walk_files(path, self.policy, ignore, max_depth=max_depth,
                          max_files=MAX_WALK_FILES if max_files is None else max_files)

    def validate(self, cwd: Path) -> Path:
        return Path(cwd).resolve()

    def open_output(self, cwd: Path, prefix: str):
        return self.outputs.create(prefix)

    def run_process(self, request: ProcessRequest, cancellation=None):
        return self.executor.run_process(request, cancellation=cancellation)

    def describe(self) -> dict:
        return {"provider": "host", "state": "host", "workspace": str(self.workspace),
                "requested": "host", "effective": "host operating-system authority"}


@dataclass(frozen=True)
class SandboxContext:
    """Broker execution context enforcing PermissionPolicy for file tools and native Shell."""

    workspace: Path
    resources: ExecutionMode
    private_paths: tuple[Path, ...]
    executor: SandboxExecutor = field(compare=False, repr=False)
    permission_snapshot: PermissionSnapshot | None = None
    provider: str = "sandbox"
    outputs: OutputStore = field(default_factory=private_output_store, compare=False, repr=False)

    @property
    def policy(self) -> PermissionPolicy:
        return self.resources.policy

    @property
    def revision(self) -> int:
        return self.permission_snapshot.revision if self.permission_snapshot is not None else 0

    @property
    def network_allowed(self) -> bool:
        return self.policy.network_allowed

    def private_path(self, path: Path) -> bool:
        return any(path.is_relative_to(root) for root in self.private_paths)

    def runtime_paths(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys(Path(value).resolve() for value in (sys.prefix, sys.base_prefix)))

    def read_roots(self) -> tuple[Path, ...]:
        return self.policy.allow_roots("read")

    def protected_paths(self) -> tuple[Path, ...]:
        return self.policy.deny_roots("write")

    def check_read(self, path: Path) -> None:
        from .file_access import check_read

        check_read(path, self.policy)

    def check_write(self, path: Path) -> None:
        from .file_access import check_write

        check_write(path, self.policy)

    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes | None:
        from .file_access import read_bytes

        return read_bytes(path, self.policy, max_bytes=max_bytes)

    def open_mutation(self, path: Path, *, create: bool, max_read_bytes: int | None = None):
        from .file_access import open_mutation

        return open_mutation(path, self.policy, create=create, max_read_bytes=max_read_bytes)

    def walk_files(self, path: Path, ignore: set[str], *, max_depth: int | None = None,
                   max_files: int | None = None):
        from .file_access import MAX_WALK_FILES, walk_files

        return walk_files(path, self.policy, ignore, max_depth=max_depth,
                          max_files=MAX_WALK_FILES if max_files is None else max_files)

    def validate(self, cwd: Path) -> Path:
        cwd = Path(cwd).resolve()
        if self.private_path(cwd):
            raise ExecutionUnavailable(
                f"Working directory is inside private Virgo storage: {cwd}",
                reason="invalid_cwd", dimension="protected_path", target=cwd,
            )
        if is_native_windows():
            home = Path.home().resolve()
            if (self.workspace == home or home.is_relative_to(self.workspace)
                    or any(root.is_relative_to(self.workspace) for root in self.private_paths)):
                raise ExecutionUnavailable(
                    "Native experimental Windows sandbox does not support running with your home or "
                    "Virgo private directory as the workspace. Use WSL2 for full Linux sandbox support "
                    "or switch to No-Limit (--permission no-limit).",
                    reason="unsupported_workspace", target=self.workspace,
                )
        try:
            self.check_read(cwd)
        except FileAccessDenied as error:
            raise ExecutionUnavailable(
                str(error), reason="invalid_cwd", dimension=error.dimension, target=error.target,
            ) from error
        if not cwd.is_dir():
            raise ExecutionUnavailable(f"Working directory is unavailable: {cwd}")
        return cwd

    def validate_native(self, cwd: Path):
        self.validate(cwd)
        return self.executor.policy(self).validate(cwd)

    def open_output(self, cwd: Path, prefix: str):
        self.validate(cwd)
        return self.outputs.create(prefix)

    def run_process(self, request: ProcessRequest, cancellation=None):
        self.validate(request.cwd)
        return self.executor.run_process(request, self, cancellation)

    def describe(self) -> dict:
        return {
            "provider": "sandbox",
            "backend": self.executor.backend,
            "workspace": str(self.workspace),
            "requested": self.resources.flags(),
            "effective": {"file_tools": self.resources.flags(), "native_process": None},
            "revision": self.revision,
            "state": "launch_unverified",
            "native_support": "checked_at_launch",
            "experimental": is_native_windows(),
            "windows_network": "AppContainer and host firewall policy apply" if os.name == "nt" else None,
            "runtime_read_dependencies": [str(p) for p in self.runtime_paths()],
            "unmanaged": ["broker", "trusted Python extensions", "hooks", "MCP services", "model transport"],
        }


def cleanup_process(proc) -> None:
    cleanup = getattr(proc, "cleanup_tree", None)
    if cleanup is not None:
        cleanup()
