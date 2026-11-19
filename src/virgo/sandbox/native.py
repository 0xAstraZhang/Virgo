"""Translate an operation's resources to the native process boundary."""
from __future__ import annotations

import sys

from ..execution import ExecutionFailed, ExecutionUnavailable, HostProcess
from .platform import NativePolicy, platform_backend


class NativeExecutor:
    def __init__(self):
        self.backend, self.path = platform_backend()
        self.identity = (self.backend, str(self.path))

    def policy(self, context, request=None):
        permissions = context.policy
        if permissions.unrestricted:
            raise ExecutionUnavailable("Unrestricted operations belong to the executor's host path")
        reads = permissions.allow_roots("read")
        writes = permissions.allow_roots("write")
        workspace = context.workspace
        if not permissions.allows(workspace, "read"):
            raise ExecutionUnavailable("Native commands require read access to their originating workspace")
        private = permissions.deny_roots("read")
        readonly = tuple(path for path in permissions.deny_roots("write")
                         if not any(path.is_relative_to(root) for root in private))
        # These are user-approved policy roots, not a model file tool target.
        # Native mounts/ACLs must never silently ignore a missing deny object.
        for path in (*private, *readonly):
            self._existing_root(path, deny=True)
        if sys.platform == "darwin":
            from .macos_process import control_root

            private = tuple(dict.fromkeys((*private, control_root())))
        grants = {}
        for path in (*reads, *writes):
            writable = permissions.allows(path, "write")
            if permissions.denied(path, "read"):
                continue
            if path == workspace or (path.is_relative_to(workspace)
                                     and (not writable or permissions.allows(workspace, "write"))):
                continue
            if workspace.is_relative_to(path):
                raise ExecutionUnavailable(f"A native grant cannot contain its originating workspace: {path}",
                                           reason="unsupported_path", target=path)
            self._existing_root(path)
            grants[path] = writable
        return NativePolicy(workspace, not permissions.allows(workspace, "write"),
                            permissions.network_allowed, private,
                            tuple(sorted(grants.items(), key=lambda item: len(item[0].parts))),
                            extra_runtime_paths=request.runtime_paths if request is not None else (),
                            read_only_paths=readonly)

    @staticmethod
    def _existing_root(path, *, deny=False):
        try:
            valid = path.is_absolute() and path.resolve(strict=True) == path and (path.is_dir() or path.is_file())
        except (OSError, RuntimeError):
            valid = False
        if not valid:
            kind = "deny rule" if deny else "grant"
            raise ExecutionUnavailable(f"Native {kind} requires an exact existing file or directory: {path}",
                                       reason="unsupported_path", target=path)

    def run_process(self, request, policy, cancellation=None):
        envelope = self.policy(policy, request)
        proc = envelope.spawn(request.argv, request.cwd, request.env, request.stdout,
                              cancellation=cancellation, stdin=request.stdin,
                              stderr=request.stderr, env_overrides=request.env_overrides)
        return NativeProcess(proc, policy)


class NativeProcess:
    def __init__(self, process, policy):
        self._scratch = process._virgo_sandbox_tmp
        self._process = HostProcess(process) if sys.platform == "linux" else process
        self.pid = process.pid
        self.stdin = getattr(process, "stdin", None)
        self.stdout = getattr(process, "stdout", None)
        self.stderr = getattr(process, "stderr", None)
        native = process._virgo_sandbox
        effective = {**policy.resources.flags(),
                     "runtime_read_dependencies": native.get("runtime_read_dependencies", []),
                     "ambient_read_access": native.get("ambient_read_access")}
        self._virgo_boundary = {
            **policy.describe(), "effective": {"file_tools": effective, "native_process": effective},
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
