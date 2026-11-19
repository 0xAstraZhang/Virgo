"""Core execution contracts, process ownership and the common executor.

A context is captured for its originating task and passed unchanged to deferred
operations. Native backend discovery is lazy so explicit No-Limit execution does
not depend on an isolation backend being installed.
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import Protocol

from .execution_policy import PermissionPolicy


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
    runtime_paths: tuple[Path, ...] = ()


def environment_overrides(values: dict[str, str]) -> dict[str, str]:
    """Validate explicit adapter grants without letting them alter launcher setup.

    Ambient credentials are still filtered by native backends. These additions
    must come from trusted configuration, never a model-supplied environment.
    """
    reserved = {"HOME", "TMPDIR", "TMP", "TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                "HOMEDRIVE", "HOMEPATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "ENV", "BASH_ENV"}
    for name, value in values.items():
        if (not isinstance(name, str) or not isinstance(value, str) or not name
                or "=" in name or "\0" in name or "\0" in value):
            raise ExecutionUnavailable("Explicit environment grants require valid string names and values",
                                       reason="invalid_environment")
        if name.upper() in reserved or name.upper().startswith(("VIRGO", "LD_", "DYLD_", "PYTHON")):
            raise ExecutionUnavailable(f"Explicit environment cannot replace launcher variable {name}",
                                       reason="invalid_environment")
    return dict(values)


class SandboxExecutor:
    """Establish the selected boundary before releasing any local payload."""

    def __init__(self):
        from .file_operations import FileOperations

        self.files = FileOperations()

    def close(self):
        self.files.close()

    def run_file_operation(self, operation, arguments, *, cwd, policy, cancellation=None):
        return self.files.run(self, operation, arguments, cwd, policy, cancellation)

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

    def run_process(self, request: ProcessRequest, policy, cancellation=None) -> ProcessHandle:
        if cancellation is not None and cancellation.is_set():
            raise ExecutionCancelled("Execution cancelled before process start")
        if not isinstance(getattr(policy, "policy", None), PermissionPolicy):
            raise ExecutionUnavailable("Execution requires an explicit effective permission policy")
        if not policy.policy.unrestricted:
            request = replace(request, runtime_paths=tuple(dict.fromkeys((*request.runtime_paths, self.files.root))))
            return self.native.run_process(request, policy, cancellation)
        env = {**request.env, **environment_overrides(request.env_overrides)}
        if os.name == "nt":
            from .tools.windows_process import WindowsProcess

            return WindowsProcess(request.argv, cwd=request.cwd, env=env, stdin=request.stdin,
                                  stdout=request.stdout, stderr=request.stderr)
        return HostProcess(subprocess.Popen(
            request.argv, cwd=str(request.cwd), env=env,
            stdin=request.stdin, stdout=request.stdout, stderr=request.stderr, start_new_session=True,
        ))


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
class ExecutionContext:
    """Origin-bound execution data; file access belongs to the worker process."""

    workspace: Path
    resources: object
    executor: SandboxExecutor = field(compare=False, repr=False)
    permission_snapshot: object = None
    outputs: object = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        if self.outputs is None:
            from .output_store import private_output_store

            object.__setattr__(self, "outputs", private_output_store())

    @classmethod
    def unrestricted(cls, workspace, *, executor=None, outputs=None):
        from .harness.execution_modes import HOST_MODE

        return cls(Path(workspace), HOST_MODE, executor or SandboxExecutor(), outputs=outputs)

    @property
    def policy(self):
        return self.resources.policy

    @property
    def provider(self):
        return "host" if self.policy.unrestricted else "sandbox"

    @property
    def revision(self):
        return self.permission_snapshot.revision if self.permission_snapshot is not None else 0

    @property
    def network_allowed(self):
        return self.policy.network_allowed

    def run_file_operation(self, operation, arguments, *, cwd=None, cancellation=None):
        return self.executor.run_file_operation(operation, arguments, cwd=cwd or self.workspace,
                                               policy=self, cancellation=cancellation)

    def run_process(self, request: ProcessRequest, cancellation=None):
        return self.executor.run_process(request, self, cancellation)

    def describe(self):
        if self.policy.unrestricted:
            return {"provider": "host", "state": "host", "workspace": str(self.workspace),
                    "requested": self.policy.as_dict(), "effective": "host operating-system authority"}
        return {"provider": "sandbox", "backend": self.executor.backend, "workspace": str(self.workspace),
                "requested": self.policy.as_dict(), "effective": None,
                "revision": self.revision, "state": "launch_unverified",
                "native_support": "checked_at_launch",
                "windows_network": "AppContainer and host firewall policy apply" if os.name == "nt" else None,
                "unmanaged": ["broker", "trusted Python extensions", "hooks", "MCP services", "model transport"]}


def file_result(result):
    if not result["ok"]:
        error = result.get("error", {})
        message = error.get("message", "Filesystem operation failed")
        if error.get("kind") == "permission_denied":
            raise FileAccessDenied(message, target=error.get("target"))
        raise OSError(error.get("errno") or 5, message)
    return result.get("details", {})


def read_file_bytes(context, path, *, max_bytes=None, follow_symlinks=False):
    import base64

    arguments = {"path": os.fspath(path), "follow_symlinks": follow_symlinks}
    if max_bytes is not None:
        arguments["max_bytes"] = max_bytes
    result = file_result(context.run_file_operation("read_bytes", arguments))
    data = result.get("data")
    return base64.b64decode(data, validate=True) if data is not None else None


def read_context(context, *, files=(), directories=()):
    """Read a component's bounded inputs once, retaining its captured authority."""
    import base64

    result = file_result(context.run_file_operation("read_context", {
        "files": [os.fspath(path) for path in files],
        "directories": [{**item, "path": os.fspath(item["path"])} for item in directories],
    }))
    for entries in (result["files"], *(group["files"] for group in result["directories"])):
        for entry in entries:
            data = entry["data"]
            entry["data"] = base64.b64decode(data, validate=True) if data is not None else None
    return result


def walk_file_entries(context, path, ignore, *, max_depth=None):
    from types import SimpleNamespace

    result = file_result(context.run_file_operation("walk_files", {
        "path": os.fspath(path), "ignore": sorted(ignore), "max_depth": max_depth,
    }))
    return ((Path(item["path"]), SimpleNamespace(st_mtime=item["mtime"], st_size=item["size"],
                                                st_mode=item["mode"])) for item in result["files"])


def cleanup_process(proc) -> None:
    cleanup = getattr(proc, "cleanup_tree", None)
    if cleanup is not None:
        cleanup()
