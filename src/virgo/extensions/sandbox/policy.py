"""Resource policy and checked broker file access, for native execution."""
from __future__ import annotations

import hashlib
import os
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from ...execution import ExecutionUnavailable, FileAccessDenied
from ...harness.execution_modes import PermissionPreset, PermissionSnapshot

PROTECTED_NAMES = (".git", ".virgo", "AGENTS.md", "CLAUDE.md")


@dataclass(frozen=True)
class SandboxPolicy:
    workspace: Path
    resources: PermissionPreset
    private_paths: tuple[Path, ...]
    executor: object
    permission_snapshot: PermissionSnapshot | None = None
    provider: str = "sandbox"
    spool_home: Path | None = None

    @property
    def revision(self) -> int:
        return self.permission_snapshot.revision if self.permission_snapshot is not None else 0

    @property
    def network_allowed(self):
        return "egress" in self.resources.network

    def private_path(self, path: Path) -> bool:
        return any(path.is_relative_to(root) for root in self.private_paths)

    def runtime_paths(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys(Path(value).resolve() for value in (sys.prefix, sys.base_prefix)))

    def read_roots(self) -> tuple[Path, ...]:
        return tuple(Path(p) for p in (*self.resources.read_paths, *self.resources.write_paths))

    def protected_paths(self) -> tuple[Path, ...]:
        return (*(self.workspace / name for name in PROTECTED_NAMES), *self.runtime_paths())
    def writable_roots(self) -> tuple[Path, ...]:
        """Roots whose inode links are already inside the writable task scope."""
        return tuple(dict.fromkeys(Path(value) for value in self.resources.write_paths))

    def check_write(self, path: Path):
        if self.private_path(path) or any(path.is_relative_to(root) for root in self.protected_paths()):
            raise FileAccessDenied(f"Protected resource is not writable: {path}", dimension="protected_path", target=path)
        if not any(path.is_relative_to(Path(root)) for root in self.resources.write_paths):
            raise FileAccessDenied(f"No write grant for {path}", dimension="write_paths", target=path)

    def _output_reader(self, path):
        return replace(self, private_paths=(), resources=replace(self.resources, read_paths=(str(path),), write_paths=()))

    def _owns_private_output(self, path):
        # The spool can sit inside private storage when the workspace contains
        # the state directory. Ownership is the exact spool directory, so the
        # read grant covers only the output files the broker created there.
        root, parts = self.spool_location()
        return path.parent == root.joinpath(*parts)

    def check_read(self, path: Path):
        from .files import check_read

        check_read(path, self._output_reader(path) if self._owns_private_output(path) else self)

    def read_bytes(self, path: Path, *, max_bytes=None):
        from .files import read_bytes

        reader = self._output_reader(path) if self._owns_private_output(path) else self
        return read_bytes(path, reader, max_bytes=max_bytes)

    def open_mutation(self, path: Path, *, create: bool):
        from .files import open_mutation

        return open_mutation(path, self, create=create)

    def walk_files(self, path, ignore, *, max_depth=None):
        from .files import walk_files

        return walk_files(path, self, ignore, max_depth=max_depth)

    def validate(self, cwd: Path) -> Path:
        cwd = Path(cwd).resolve()
        try:
            self.check_read(cwd)
        except FileAccessDenied as error:
            raise ExecutionUnavailable(str(error), reason="invalid_cwd", dimension=error.dimension,
                                       target=error.target) from error
        if not cwd.is_dir():
            raise ExecutionUnavailable(f"Working directory is unavailable: {cwd}")
        return cwd

    def validate_native(self, cwd: Path):
        self.validate(cwd)
        return self.executor.policy(self).validate(cwd)

    def spool_location(self):
        if self.spool_home is None:
            return self.workspace, (".virgo", "outputs")
        workspace_id = hashlib.sha256(os.fsencode(self.workspace)).hexdigest()
        return self.spool_home, ("outputs", workspace_id)

    def open_output(self, cwd, prefix):
        self.validate(cwd)
        root, parts = self.spool_location()
        if self.spool_home is not None:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "nt":
            from .windows_files import open_output

            try:
                return open_output(root, parts, prefix)
            except OSError as error:
                raise ExecutionUnavailable("Protected output location is unavailable", reason="invalid_output",
                                           dimension="output_path", target=str(root.joinpath(*parts))) from error
        directory = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(root, flags)
            for part in parts:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = child
            name = f"{prefix}-{uuid.uuid4().hex}.log"
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            return root.joinpath(*parts, name), os.fdopen(fd, "wb", buffering=0)
        except OSError as error:
            raise ExecutionUnavailable("Protected output location is unavailable", reason="invalid_output",
                                       dimension="output_path", target=str(root.joinpath(*parts))) from error
        finally:
            if directory is not None:
                os.close(directory)

    def spawn(self, argv, cwd, env, stdout, output_path=None, cancellation=None):
        cwd = self.validate(cwd)
        return self.executor.spawn(self, argv, cwd, env, stdout, output_path, cancellation)

    def read_output_tail(self, path: Path, max_bytes: int) -> tuple[bytes, int]:
        from .files import read_bytes

        # Only Core's owned spool references reach this method. The exception is
        # for one file, never its directory; retained handles reject aliases.
        root, parts = self.spool_location()
        if path.parent != root.joinpath(*parts):
            raise FileAccessDenied("Output is outside the originating workspace", target=path)
        return read_bytes(path, self._output_reader(path), max_bytes=max_bytes, tail=True)

    def describe(self):
        return {"provider": "sandbox", "backend": self.executor.backend, "workspace": str(self.workspace),
                "requested": self.resources.flags(), "effective": {"file_tools": self.resources.flags(), "native_process": None},
                "revision": self.revision, "state": "launch_unverified",
                "native_support": "checked_at_launch",
                "workspace_link_contract": "host writers preserve permissions across link and rename operations",
                "windows_network": "AppContainer and host firewall policy apply" if os.name == "nt" else None,
                "runtime_read_dependencies": [str(p) for p in self.runtime_paths()],
                "unmanaged": ["broker", "trusted Python extensions", "hooks", "MCP services", "model transport"]}
