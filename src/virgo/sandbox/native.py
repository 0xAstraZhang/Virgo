"""Translate an operation's PermissionPolicy to the native process boundary."""
from __future__ import annotations

import sys
from pathlib import Path

from ..execution import ExecutionFailed, ExecutionUnavailable, HostProcess
from .platform import NativePolicy, platform_backend


class NativeExecutor:
    def __init__(self):
        self.backend, self.path = platform_backend()
        self.identity = (self.backend, str(self.path))

    def policy(self, context):
        permissions = context.policy
        if permissions.unrestricted:
            raise ExecutionUnavailable("Unrestricted operations belong to the executor's host path")
        reads = permissions.allow_roots("read")
        writes = permissions.allow_roots("write")
        workspace = context.workspace
        if not permissions.allows(workspace, "read"):
            raise ExecutionUnavailable("Native commands require read access to their originating workspace")
        private = permissions.deny_roots("read")
        readonly = tuple(
            path for path in permissions.deny_roots("write")
            if not any(path.is_relative_to(root) for root in private)
        )
        if sys.platform == "darwin":
            from .macos_process import control_root

            private = tuple(dict.fromkeys((*private, control_root())))
        grants: dict[Path, bool] = {}
        for path in (*reads, *writes):
            writable = permissions.allows(path, "write")
            if permissions.denied(path, "read"):
                continue
            if path == workspace or (path.is_relative_to(workspace)
                                     and (not writable or permissions.allows(workspace, "write"))):
                continue
            if workspace.is_relative_to(path):
                raise ExecutionUnavailable(
                    f"A native grant cannot contain its originating workspace: {path}",
                    reason="unsupported_path", target=path,
                )
            self._existing_grant(path)
            grants[path] = writable
        return NativePolicy(
            workspace,
            not permissions.allows(workspace, "write"),
            permissions.network_allowed,
            private,
            tuple(sorted(grants.items(), key=lambda item: len(item[0].parts))),
            read_only_paths=readonly,
        )

    @staticmethod
    def _existing_grant(path: Path) -> None:
        try:
            valid = path.is_absolute() and path.resolve(strict=True) == path and (path.is_dir() or path.is_file())
        except (OSError, RuntimeError):
            valid = False
        if not valid:
            raise ExecutionUnavailable(
                f"Native grant requires the exact existing file or directory: {path}",
                reason="unsupported_path", target=path,
            )

    def run_process(self, request, context, cancellation=None):
        envelope = self.policy(context)
        proc = envelope.spawn(
            request.argv, request.cwd, request.env, request.stdout,
            request.output_path, cancellation, stdin=request.stdin,
            stderr=request.stderr, env_overrides=request.env_overrides,
        )
        return NativeProcess(proc, context)


class NativeProcess:
    def __init__(self, process, context):
        self._scratch = process._virgo_sandbox_tmp
        self._process = HostProcess(process) if sys.platform == "linux" else process
        self.pid = process.pid
        self.stdin = getattr(process, "stdin", None)
        self.stdout = getattr(process, "stdout", None)
        self.stderr = getattr(process, "stderr", None)
        native = getattr(process, "_virgo_sandbox", {})
        effective = {
            **context.resources.flags(),
            "runtime_read_dependencies": native.get("runtime_read_dependencies", []),
            "ambient_read_access": native.get("ambient_read_access"),
        }
        self._virgo_boundary = {
            **context.describe(),
            "effective": {"file_tools": context.resources.flags(), "native_process": effective},
            "state": "established", "payload_started": True, "side_effects": "possible", "cleanup": "pending",
        }

    @property
    def returncode(self):
        return self._process.returncode

    def poll(self):
        return self._process.poll()

    def wait(self, timeout=None):
        return self._process.wait(timeout=timeout)

    def kill_tree(self):
        self._process.kill_tree()

    def cleanup_tree(self):
        if self._scratch is None:
            return
        try:
            self._process.cleanup_tree()
            self._scratch.cleanup()
        except Exception as cause:
            if getattr(self._process, "tree_drained", False):
                try:
                    self._scratch.cleanup()
                    self._scratch = None
                except Exception:
                    pass
            self._virgo_boundary["cleanup"] = "failed"
            failure = ExecutionFailed(f"Could not release native sandbox resources: {cause}")
            failure.boundary = self._virgo_boundary
            raise failure from cause
        self._scratch = None
        self._virgo_boundary["cleanup"] = "executor_completed"
