"""Resource policy and checked broker file access, for native execution."""
from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..execution import ExecutionUnavailable, FileAccessDenied, read_file_bytes, walk_file_entries
from ..harness.execution_modes import PermissionPreset, PermissionSnapshot

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
    outputs: object = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        if self.outputs is None:
            from ..output_store import private_output_store

            object.__setattr__(self, "outputs", private_output_store())

    def run_file_operation(self, operation, arguments, *, cwd=None, cancellation=None):
        return self.executor.run_file_operation(operation, arguments, cwd=cwd or self.workspace,
                                                policy=self, cancellation=cancellation)

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
        if self.spool_home is None:
            return False
        root, parts = self.spool_location()
        return path.parent == root.joinpath(*parts)

    def check_read(self, path: Path):
        from .files import check_read

        check_read(path, self._output_reader(path) if self._owns_private_output(path) else self)

    def read_bytes(self, path: Path, *, max_bytes=None):
        return read_file_bytes(self, path, max_bytes=max_bytes)

    def open_mutation(self, path: Path, *, create: bool):
        from .files import open_mutation

        return open_mutation(path, self, create=create)

    def walk_files(self, path, ignore, *, max_depth=None):
        return walk_file_entries(self, path, ignore, max_depth=max_depth)

    def validate(self, cwd: Path) -> Path:
        cwd = Path(cwd).resolve()
        try:
            self.check_read(cwd)
        except FileAccessDenied as error:
            raise ExecutionUnavailable(str(error), reason="invalid_cwd", dimension=error.dimension,
                                       target=error.target) from error
        if not cwd.is_dir():
            raise ExecutionUnavailable(f"Working directory is unavailable: {cwd}")
        if any(root.is_relative_to(self.workspace) for root in self.private_paths):
            raise ExecutionUnavailable("Workspace must not contain private broker storage")
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
        return self.outputs.create(prefix)

    def run_process(self, request, cancellation=None):
        self.validate(request.cwd)
        return self.executor.run_process(request, self, cancellation)

    def read_output_tail(self, record, max_bytes):
        data, total, _ = self.outputs.read(record, limit=max_bytes, tail=True)
        return data, total

    def describe(self):
        return {"provider": "sandbox", "backend": self.executor.backend, "workspace": str(self.workspace),
                "requested": self.resources.flags(), "effective": {"file_tools": self.resources.flags(), "native_process": None},
                "revision": self.revision, "state": "launch_unverified",
                "native_support": "checked_at_launch",
                "workspace_link_contract": "host writers preserve permissions across link and rename operations",
                "windows_network": "AppContainer and host firewall policy apply" if os.name == "nt" else None,
                "runtime_read_dependencies": [str(p) for p in self.runtime_paths()],
                "unmanaged": ["broker", "trusted Python extensions", "hooks", "MCP services", "model transport"]}
