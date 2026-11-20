"""Core native execution provider and operation-scoped resource approvals."""
from __future__ import annotations

import json
from pathlib import Path

from ..agent import AgentTool, ToolCallEvent
from ..execution import SandboxExecutor
from ..harness.execution import HostExecutionProvider
from ..harness.execution_modes import NETWORK_DIRECTIONS, PermissionSnapshot
from ..harness.resource_approvals import Grant, ResourceApprovals
from .policy import SandboxPolicy


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
        workspace = Path(runtime.accounting_store.cwd).resolve()
        approval = "never" if runtime.approval_policy == "never" else self.session.approval_policy
        policy = SandboxPolicy(workspace, mode.resolve(workspace),
                               (self.session.paths.user.resolve(),),
                               self.executor, PermissionSnapshot(mode, approval, revision),
                               spool_home=self.session.paths.user.resolve() if mode.id == "read-only" else None)
        policy.validate(runtime.cwd)
        return policy

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
                "network_allowed": {"type": ["boolean", "array"], "items": {"type": "string", "enum": list(NETWORK_DIRECTIONS)},
                                    "description": "Additional allowed directions. true requests all three."},
            }, "additionalProperties": False},
        }, "required": ["tool", "arguments", "reason", "permissions"], "additionalProperties": False},
        execute=provider.request_permissions, side_effect="read", sequential=True,
    )
    api.register_tool(tool, roles=("main", "subagent"))
    return tool
