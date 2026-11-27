"""Core execution composition facade and common executor.

A context is captured for its originating task and passed unchanged to deferred
operations. Native backend discovery is lazy so explicit No-Limit execution does
not depend on an isolation backend being installed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from . import execution_policy
from .execution_policy import (
    HOST_MODE,
    ExecutionMode,
    PermissionPolicy,
    PermissionSnapshot,
)
from .execution_types import (
    ExecutionCancelled,
    ExecutionContext,
    ExecutionFailed,
    ExecutionUnavailable,
    FileAccessDenied,
    ProcessHandle,
    ProcessRequest,
    _merge_environment,
    cleanup_process,
    environment_overrides,
)
from .file_access import (
    MAX_WALK_FILES,
    check_read,
    check_write,
    open_mutation,
    read_bytes,
    walk_files,
)
from .output_store import OutputStore, private_output_store
from .process import HostProcess


def is_windows_sandbox_supported() -> bool:
    """Return True when native Windows supports unelevated Standard AppContainer (Win11 null.sys Feature 60692039)."""
    if not execution_policy.is_native_windows():
        return False
    from .sandbox.windows_acl import is_windows_appcontainer_supported

    return bool(is_windows_appcontainer_supported())


def default_execution_mode() -> str:
    """Default execution mode: Workspace-Edit on supported Tier 1 hosts, No-Limit on unsupported Windows."""
    if execution_policy.is_native_windows() and not is_windows_sandbox_supported():
        return "no-limit"
    return "workspace-edit"


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
        return {"linux": "bubblewrap", "darwin": "seatbelt", "win32": "windows-appcontainer"}.get(
            sys.platform, "unsupported"
        )

    def close_idle(self):
        native = self.__dict__.get("native")
        if native is not None and hasattr(native, "close_idle"):
            native.close_idle()

    def close(self):
        native = self.__dict__.get("native")
        if native is not None and hasattr(native, "close"):
            native.close()

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
            from .process import WindowsProcess

            return WindowsProcess(
                request.argv, cwd=request.cwd, env=env, stdin=request.stdin,
                stdout=request.stdout, stderr=request.stderr,
            )
        return HostProcess(subprocess.Popen(
            request.argv, cwd=str(request.cwd), env=env,
            stdin=request.stdin, stdout=request.stdout, stderr=request.stderr, start_new_session=True,
        ))


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
        return read_bytes(path, self.policy, max_bytes=max_bytes)

    def open_mutation(self, path: Path, *, create: bool, max_read_bytes: int | None = None):
        return open_mutation(path, self.policy, create=create, max_read_bytes=max_read_bytes)

    def walk_files(self, path: Path, ignore: set[str], *, max_depth: int | None = None,
                   max_files: int | None = None, respect_gitignore: bool = False):
        return walk_files(
            path, self.policy, ignore, max_depth=max_depth,
            max_files=MAX_WALK_FILES if max_files is None else max_files,
            respect_gitignore=respect_gitignore, gitignore_root=self.workspace,
        )

    def open_output(self, cwd: Path, prefix: str):
        return self.outputs.create(prefix)

    def run_process(self, request: ProcessRequest, cancellation=None):
        return self.executor.run_process(request, cancellation=cancellation)

    def describe(self) -> dict:
        return {
            "provider": "host", "state": "host", "workspace": str(self.workspace),
            "requested": "host", "effective": "host operating-system authority",
        }


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

    def private_path(self, path: Path) -> bool:
        return any(path.is_relative_to(root) for root in self.private_paths)

    def runtime_paths(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys(Path(value).resolve() for value in (sys.prefix, sys.base_prefix)))

    def check_read(self, path: Path) -> None:
        check_read(path, self.policy)

    def check_write(self, path: Path) -> None:
        check_write(path, self.policy)

    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes | None:
        return read_bytes(path, self.policy, max_bytes=max_bytes)

    def open_mutation(self, path: Path, *, create: bool, max_read_bytes: int | None = None):
        return open_mutation(path, self.policy, create=create, max_read_bytes=max_read_bytes)

    def walk_files(self, path: Path, ignore: set[str], *, max_depth: int | None = None,
                   max_files: int | None = None, respect_gitignore: bool = False):
        return walk_files(
            path, self.policy, ignore, max_depth=max_depth,
            max_files=MAX_WALK_FILES if max_files is None else max_files,
            respect_gitignore=respect_gitignore, gitignore_root=self.workspace,
        )

    def validate(self, cwd: Path) -> Path:
        cwd = Path(cwd).resolve()
        if self.private_path(cwd):
            raise ExecutionUnavailable(
                f"Working directory is inside private Virgo storage: {cwd}",
                reason="invalid_cwd", dimension="protected_path", target=cwd,
            )
        if execution_policy.is_native_windows():
            home = Path.home().resolve()
            if (
                self.workspace == home or home.is_relative_to(self.workspace)
                or any(root.is_relative_to(self.workspace) for root in self.private_paths)
            ):
                raise ExecutionUnavailable(
                    "Native Windows sandbox does not support running with your home or "
                    "Virgo private directory as the workspace. Switch to a project directory "
                    "or use No-Limit (--permission no-limit).",
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
            "windows_network": "AppContainer and host firewall policy apply" if os.name == "nt" else None,
            "runtime_read_dependencies": [str(p) for p in self.runtime_paths()],
            "unmanaged": ["broker", "trusted Python extensions", "hooks", "MCP services", "model transport"],
        }


__all__ = [
    "ExecutionCancelled",
    "ExecutionContext",
    "ExecutionFailed",
    "ExecutionUnavailable",
    "FileAccessDenied",
    "HostContext",
    "HostProcess",
    "ProcessHandle",
    "ProcessRequest",
    "SandboxContext",
    "SandboxExecutor",
    "cleanup_process",
    "default_execution_mode",
    "environment_overrides",
    "is_windows_sandbox_supported",
]
