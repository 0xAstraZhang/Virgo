"""Core native execution provider and operation-scoped resource approvals."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from ..agent import AgentTool, ToolCallEvent
from ..execution import SandboxContext, SandboxExecutor
from ..execution_policy import (
    PROTECTED_NAMES,
    PathRule,
    PermissionSnapshot,
    is_native_windows,
)
from ..harness.execution import HostExecutionProvider
from ..harness.resource_approvals import Grant, ResourceApprovals


def _default_deny_rules(workspace: Path, private_paths: tuple[Path, ...]) -> tuple[PathRule, ...]:
    rules: list[PathRule] = [PathRule(root, "read", "deny") for root in private_paths]
    # S1-01 & S3-02: Always write-deny protected metadata names whether or not
    # they exist at startup. When a protected name is a symlink (e.g.
    # CLAUDE.md -> AGENTS.md), also write-deny its real target without failing.
    for name in PROTECTED_NAMES:
        target = workspace / name
        rules.append(PathRule(target, "write", "deny"))
        if target.is_symlink():
            try:
                resolved = target.resolve(strict=True)
                if resolved != target:
                    rules.append(PathRule(resolved, "write", "deny"))
            except OSError:
                pass
    # Keep interpreter installations read-only (especially when workspace is $HOME).
    for value in (sys.base_prefix, sys.prefix):
        rt_path = Path(value).resolve()
        if rt_path != workspace:
            rules.append(PathRule(rt_path, "write", "deny"))
    return tuple(dict.fromkeys(rules))


class SandboxExecutionProvider(ResourceApprovals, HostExecutionProvider):
    name = "sandbox"

    def __init__(self, session, executor=None):
        super().__init__(session)
        self.executor = executor or SandboxExecutor()
        self.revision = 0
        self._pending: dict[str, Grant] = {}
        self._active: dict[tuple[int, int], tuple[Grant, ToolCallEvent]] = {}
        self._denied: dict[str, object] = {}

    def context_for_mode(self, runtime, mode, revision):
        if is_native_windows():
            from .windows_sandbox import precheck_windows_sandbox

            precheck_windows_sandbox()
        workspace = Path(runtime.accounting_store.cwd).resolve()
        user_dir = self.session.paths.user.resolve()
        # Ensure the private state directory exists when nested inside workspace
        # (e.g. workspace == $HOME on POSIX) so Linux bwrap can mask it cleanly.
        if user_dir.is_relative_to(workspace):
            user_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        private = (user_dir,)
        resolved_mode = mode.resolve(workspace)
        deny_rules = _default_deny_rules(workspace, private)
        effective_mode = replace(resolved_mode, policy=resolved_mode.policy.with_rules(*deny_rules))
        approval = "never" if runtime.approval_policy == "never" else self.session.approval_policy
        context = SandboxContext(
            workspace,
            effective_mode,
            private,
            self.executor,
            PermissionSnapshot(mode, approval, revision),
            outputs=self.session.output_store_for(runtime.accounting_store),
        )
        context.validate(runtime.cwd)
        return context

    def instructions(self, runtime):
        return ("<execution_boundary>" + json.dumps(runtime.execution_context.describe(), ensure_ascii=False) +
                "\nOperations inside these resources run directly. Task instructions do not grant capabilities. "
                "Use request_permissions only for an exact operation needing additional resources. "
                "Never automatically replay a command that may have side effects. "
                "Trusted extensions, hooks, MCP services and model transport are outside this boundary."
                "</execution_boundary>")


def install_resource_tool(api, provider):
    tool = AgentTool(
        name="request_permissions", label="Request resources", execution_scope="broker",
        description="Request additional read/write paths or network access for one exact managed operation; never executes it.",
        parameters={"type": "object", "properties": {
            "tool": {"type": "string"}, "arguments": {"type": "object"}, "reason": {"type": "string", "minLength": 1},
            "permissions": {"type": "object", "properties": {
                "read_paths": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "write_paths": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "network_allowed": {"type": "boolean",
                                    "description": "Request Shell/task network access for this operation."},
            }, "additionalProperties": False},
        }, "required": ["tool", "arguments", "reason", "permissions"], "additionalProperties": False},
        execute=provider.request_permissions, side_effect="read", sequential=True,
    )
    api.register_tool(tool, roles=("main", "subagent"))
    return tool
