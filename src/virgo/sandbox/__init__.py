"""Core native execution provider and operation-scoped resource approvals."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from ..agent import AgentTool, ToolCallEvent
from ..execution import ExecutionContext, SandboxExecutor
from ..execution_policy import PathRule
from ..harness.execution import HostExecutionProvider
from ..harness.execution_modes import PermissionSnapshot
from ..harness.resource_approvals import Grant, ResourceApprovals

PROTECTED_NAMES = (".git", ".virgo", "AGENTS.md", "CLAUDE.md")


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
        private = self.session.paths.user.resolve()
        # These are trusted control locations captured for the originating task,
        # never file-tool targets supplied by the model. Explicit deny rules are
        # carried to every native launch, including one-shot approved operations.
        control = tuple(path for name in PROTECTED_NAMES
                        if (path := workspace / name).exists() or path.is_symlink())
        runtime_roots = tuple(dict.fromkeys((Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                                            self.executor.files.root)))
        resources = mode.resolve(workspace)
        resources = replace(resources, policy=resources.policy.with_rules(
            PathRule(private, "read", "deny"),
            *(PathRule(path, "write", "deny") for path in (*control, *runtime_roots)),
        ))
        return ExecutionContext(workspace, resources, self.executor,
                             PermissionSnapshot(mode, approval, revision))

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
                "network_allowed": {"type": "boolean", "description": "Allow networking for this exact operation."},
            }, "additionalProperties": False},
        }, "required": ["tool", "arguments", "reason", "permissions"], "additionalProperties": False},
        execute=provider.request_permissions, side_effect="read", sequential=True,
    )
    api.register_tool(tool, roles=("main", "subagent"))
    return tool
