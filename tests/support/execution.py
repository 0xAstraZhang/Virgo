"""Explicit execution contexts used by real filesystem-worker acceptance."""

from pathlib import Path

from virgo.execution import SandboxExecutor
from virgo.harness.execution_modes import PermissionPreset, parse_network
from virgo.sandbox.policy import SandboxPolicy


def file_policy(workspace, *, read_paths=None, write_paths=(), network_allowed=False, private_paths=(), executor=None):
    workspace = Path(workspace).resolve()
    reads = (str(workspace),) if read_paths is None else tuple(map(str, read_paths))
    resources = PermissionPreset("test", "Test", reads, tuple(map(str, write_paths)), parse_network(network_allowed))
    return SandboxPolicy(workspace, resources, tuple(private_paths), executor or SandboxExecutor())


def sandbox_config(preset="workspace-edit"):
    return {"execution": {"mode": preset}}
