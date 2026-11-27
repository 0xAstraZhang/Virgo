"""Broker-only resource tests; never evidence of native isolation."""

from pathlib import Path
from types import SimpleNamespace

from virgo.execution import SandboxContext
from virgo.execution_policy import (
    ExecutionMode,
    PathRule,
    PermissionPolicy,
    default_deny_rules,
    parse_network,
)


def file_policy(workspace, *, read_paths=None, write_paths=(), network_allowed=False, private_paths=()):
    workspace = Path(workspace).resolve()
    reads = (workspace,) if read_paths is None else tuple(Path(p).resolve() for p in read_paths)
    writes = tuple(Path(p).resolve() for p in write_paths)
    privates = tuple(Path(p).resolve() for p in private_paths)
    rules = (
        *(PathRule(p, "read", "allow") for p in reads),
        *(PathRule(p, "write", "allow") for p in writes),
        *default_deny_rules(workspace, privates),
    )
    mode = ExecutionMode("test", "Test", PermissionPolicy(rules, parse_network(network_allowed)))
    return SandboxContext(workspace, mode, privates, SimpleNamespace(backend="not-launched"))


def sandbox_config(preset="workspace-edit"):
    return {"execution": {"mode": preset}}
