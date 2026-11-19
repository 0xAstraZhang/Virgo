"""Explicit execution contexts used by real filesystem-worker acceptance."""

import sys
from pathlib import Path

from virgo.execution import ExecutionContext, SandboxExecutor
from virgo.execution_policy import PathRule, PermissionPolicy
from virgo.harness.execution_modes import PermissionPreset
from virgo.sandbox import PROTECTED_NAMES


def file_policy(workspace, *, read_paths=None, write_paths=(), network_allowed=False, private_paths=(), executor=None):
    workspace = Path(workspace).resolve()
    executor = executor or SandboxExecutor()
    reads = (workspace,) if read_paths is None else tuple(map(Path, read_paths))
    control = tuple(path for name in PROTECTED_NAMES
                    if (path := workspace / name).exists() or path.is_symlink())
    runtime = (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), executor.files.directory)
    policy = PermissionPolicy((
        *(PathRule(path, "read") for path in reads),
        *(PathRule(Path(path), "write") for path in write_paths),
        *(PathRule(Path(path), "read", "deny") for path in private_paths),
        *(PathRule(path, "write", "deny") for path in (*control, *runtime)),
    ), network_allowed)
    return ExecutionContext(workspace, PermissionPreset("test", "Test", policy), executor)


def sandbox_config(preset="workspace-edit"):
    return {"execution": {"mode": preset}}
