"""Broker-only resource tests; never evidence of native isolation."""

from pathlib import Path
from types import SimpleNamespace

from virgo.extensions.sandbox.policy import SandboxPolicy
from virgo.harness.execution_modes import PermissionPreset, parse_network


def file_policy(workspace, *, read_paths=None, write_paths=(), network_allowed=False, private_paths=()):
    workspace = Path(workspace).resolve()
    reads = (str(workspace),) if read_paths is None else tuple(map(str, read_paths))
    resources = PermissionPreset("test", "Test", reads, tuple(map(str, write_paths)), parse_network(network_allowed))
    return SandboxPolicy(workspace, resources, tuple(private_paths), SimpleNamespace(backend="not-launched"))


def sandbox_config(preset="workspace-edit"):
    return {"execution": {"mode": preset}}
