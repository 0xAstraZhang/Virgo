"""Translate an operation's resources to the native process boundary."""
from __future__ import annotations

import sys
from pathlib import Path

from ...execution import ExecutionFailed, ExecutionUnavailable, HostProcess
from ...harness.execution_modes import FULL_NETWORK
from .platform import NativePolicy, platform_backend


class NativeExecutor:
    def __init__(self):
        self.backend, self.path = platform_backend()
        self.identity = (self.backend, str(self.path))

    def policy(self, context):
        resources = context.resources
        if resources.network and resources.network != FULL_NETWORK:
            raise ExecutionUnavailable("The native backend requires a complete Shell network grant",
                                       reason="unsupported_policy", dimension="network_allowed")
        reads = tuple(map(Path, resources.read_paths))
        writes = tuple(map(Path, resources.write_paths))
        workspace = context.workspace
        if workspace not in (*reads, *writes):
            raise ExecutionUnavailable("Native commands require read access to their originating workspace")
        private = context.private_paths
        if sys.platform == "darwin":
            from .macos_process import control_root

            private = (*private, control_root())
        grants = {}
        for path in (*reads, *writes):
            writable = path in writes
            if path == workspace or (path.is_relative_to(workspace) and (not writable or workspace in writes)):
                continue
            if (workspace.is_relative_to(path) or any(
                    path.is_relative_to(root) or root.is_relative_to(path) for root in private)
                    or writable and any(path.is_relative_to(root) or root.is_relative_to(path)
                                        for root in context.protected_paths())):
                raise ExecutionUnavailable(f"Requested native scope overlaps a protected resource: {path}",
                                           reason="protected_path", target=path)
            if not (path.is_dir() or path.is_file()) or path.resolve(strict=True) != path:
                raise ExecutionUnavailable(f"Native grant requires the exact existing file or directory: {path}",
                                           reason="unsupported_path", target=path)
            grants[path] = writable
        return NativePolicy(workspace, workspace not in writes, bool(resources.network), private,
                            tuple(sorted(grants.items(), key=lambda item: len(item[0].parts))))

    def spawn(self, policy, argv, cwd, env, stdout, output_path, cancellation):
        envelope = self.policy(policy)
        proc = envelope.spawn(argv, cwd, env, stdout, output_path, cancellation)
        return NativeProcess(proc, policy)


class NativeProcess:
    def __init__(self, process, policy):
        self._scratch = process._virgo_sandbox_tmp
        self._process = HostProcess(process) if sys.platform == "linux" else process
        self.pid = process.pid
        self._virgo_boundary = {
            **policy.describe(), "effective": {"file_tools": policy.resources.flags(),
                                               "native_process": policy.resources.flags()},
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
            self._virgo_boundary["cleanup"] = "failed"
            failure = ExecutionFailed(f"Could not release native sandbox resources: {cause}")
            failure.boundary = self._virgo_boundary
            raise failure from cause
        self._scratch = None
        self._virgo_boundary["cleanup"] = "executor_completed"
