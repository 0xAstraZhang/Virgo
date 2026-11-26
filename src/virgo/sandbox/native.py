"""Translate an operation's PermissionPolicy to the native process boundary."""
from __future__ import annotations

import atexit
import subprocess
import sys
import threading
import weakref
from pathlib import Path

from ..execution_types import ExecutionFailed, ExecutionUnavailable
from ..process import HostProcess
from .platform import NativePolicy, _CommandScratch, platform_backend

_LIVE_POSIX_SCRATCHES: weakref.WeakSet[_CommandScratch] = weakref.WeakSet()


def _cleanup_posix_scratches() -> None:
    for scratch in list(_LIVE_POSIX_SCRATCHES):
        try:
            scratch.cleanup()
        except Exception:
            pass


atexit.register(_cleanup_posix_scratches)


class NativeExecutor:
    def __init__(self):
        self.backend, self.path = platform_backend()
        self.identity = (self.backend, str(self.path))
        self._pool_lock = threading.Lock()
        self._windows_pool = None
        self._posix_session_scratch: _CommandScratch | None = None
        self._probed_launcher: Path | None = None

    def _get_windows_pool(self):
        if self.backend != "windows-appcontainer":
            return None
        with self._pool_lock:
            if self._windows_pool is None:
                from .windows_sandbox import WindowsSessionPool

                self._windows_pool = WindowsSessionPool()
            return self._windows_pool

    def _get_posix_session_dir(self) -> Path | None:
        if sys.platform == "win32":
            return None
        with self._pool_lock:
            if self._posix_session_scratch is None:
                scratch = _CommandScratch(prefix="virgo-session-")
                _LIVE_POSIX_SCRATCHES.add(scratch)
                self._posix_session_scratch = scratch
            return Path(self._posix_session_scratch.name)

    def _ensure_seatbelt_probed(self, launcher: Path) -> None:
        with self._pool_lock:
            if self._probed_launcher == launcher:
                return
        probe_bin = "/usr/bin/true" if Path("/usr/bin/true").is_file() else "/bin/sh"
        probe_args = [probe_bin] if probe_bin == "/usr/bin/true" else [probe_bin, "-c", "true"]
        try:
            probe = subprocess.run(
                [str(launcher), "-p", "(version 1)\n(allow default)", *probe_args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=5,
                start_new_session=True,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ExecutionUnavailable(
                f"seatbelt could not establish isolation: {error}",
                reason="backend_unavailable",
            ) from error
        if probe.returncode:
            reason = probe.stdout.decode("utf-8", errors="replace")[-1500:].strip()
            raise ExecutionUnavailable(
                f"seatbelt could not establish isolation (exit {probe.returncode}): {reason}",
                reason="backend_unavailable",
            )
        with self._pool_lock:
            self._probed_launcher = launcher

    def close_idle(self):
        with self._pool_lock:
            pool = self._windows_pool
        if pool is not None:
            pool.close_idle()

    def close(self):
        with self._pool_lock:
            pool = self._windows_pool
            self._windows_pool = None
            posix_scratch = self._posix_session_scratch
            self._posix_session_scratch = None
        if pool is not None:
            pool.close()
        if posix_scratch is not None:
            posix_scratch.cleanup()

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
        grants: dict[Path, bool] = {}
        for path in (*reads, *writes):
            writable = permissions.allows(path, "write")
            if permissions.denied(path, "read"):
                continue
            if path == workspace or (
                path.is_relative_to(workspace)
                and (not writable or (permissions.allows(workspace, "write")
                                      and not any(path.is_relative_to(ro) for ro in readonly)))
            ):
                continue
            if workspace.is_relative_to(path):
                raise ExecutionUnavailable(
                    f"A native grant cannot contain its originating workspace: {path}",
                    reason="unsupported_path", target=path,
                )
            resolved = self._resolve_existing_grant(path)
            if resolved is not None:
                grants[resolved] = grants.get(resolved, False) or writable
        return NativePolicy(
            workspace,
            not permissions.allows(workspace, "write"),
            permissions.network_allowed,
            private,
            tuple(sorted(grants.items(), key=lambda item: len(item[0].parts))),
            read_only_paths=readonly,
        )

    @staticmethod
    def _resolve_existing_grant(path: Path) -> Path | None:
        """Return canonical existing file/dir for native mount/ACL, skipping absent paths."""
        if not path.is_absolute():
            raise ExecutionUnavailable(
                f"Native grant requires an absolute path: {path}",
                reason="unsupported_path", target=path,
            )
        if not path.exists() and not path.is_symlink():
            return None
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ExecutionUnavailable(
                f"Native grant path cannot be resolved: {path} ({error})",
                reason="unsupported_path", target=path,
            ) from error
        if not (resolved.is_dir() or resolved.is_file()):
            raise ExecutionUnavailable(
                f"Native grant requires a regular file or directory: {path}",
                reason="unsupported_path", target=path,
            )
        return resolved

    def run_process(self, request, context, cancellation=None):
        envelope = self.policy(context)
        proc = envelope.spawn(
            request.argv,
            request.cwd,
            request.env,
            request.stdout,
            cancellation,
            stdin=request.stdin,
            stderr=request.stderr,
            env_overrides=request.env_overrides,
            session_pool=self._get_windows_pool(),
            posix_session_dir=self._get_posix_session_dir(),
            ensure_probed=self._ensure_seatbelt_probed if self.backend == "seatbelt" else None,
        )
        return NativeProcess(proc, context)


class NativeProcess:
    def __init__(self, process, context):
        self._scratch = process._virgo_sandbox_tmp
        self._process = process if callable(getattr(process, "cleanup_tree", None)) else HostProcess(process)
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
