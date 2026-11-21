"""Core native execution provider and resource-scoped approvals."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from ..agent import AgentTool
from ..execution import SandboxContext, SandboxExecutor
from ..execution_policy import (
    PROTECTED_NAMES,
    PathRule,
    PermissionSnapshot,
    default_private_paths,
    is_native_windows,
)
from ..harness.execution import HostExecutionProvider
from ..harness.resource_approvals import ResourceApprovals


def _default_deny_rules(workspace: Path, private_paths: tuple[Path, ...]) -> tuple[PathRule, ...]:
    rules: list[PathRule] = [PathRule(root, "read", "deny") for root in private_paths]
    # Protect project .virgo with confirm-on-write (write-deny, overridable by explicit user approval).
    for name in PROTECTED_NAMES:
        target = workspace / name
        if not any(target.is_relative_to(root) for root in private_paths):
            rules.append(PathRule(target, "write", "deny"))
        if target.is_symlink():
            try:
                resolved = target.resolve(strict=True)
                if resolved != target and not any(resolved.is_relative_to(root) for root in private_paths):
                    rules.append(PathRule(resolved, "write", "deny"))
            except OSError:
                pass
    return tuple(dict.fromkeys(rules))


class SandboxExecutionProvider(ResourceApprovals, HostExecutionProvider):
    name = "sandbox"

    def __init__(self, session, executor=None):
        super().__init__(session)
        self.executor = executor or SandboxExecutor()
        self.revision = 0
        self._session_grants: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._run_denials: dict[str, set[tuple[str, str]]] = {}

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
        inferred_home = user_dir.parent if user_dir.name == ".virgo" else None
        raw_private = (
            *default_private_paths(user_dir),
            *(default_private_paths(user_dir, home=inferred_home) if inferred_home is not None else ()),
        )
        # Keep user_dir always private; exclude other default home paths only if workspace is inside one.
        private = tuple(dict.fromkeys(
            p for p in raw_private
            if p == user_dir or not workspace.is_relative_to(p)
        ))
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
                "Call file, network, and external tools directly; Virgo prompts the user on demand and executes "
                "immediately upon approval. Use request_permissions when a Shell command needs additional paths "
                "or network_allowed (you may emit request_permissions right before the Shell call in the same turn). "
                "Never automatically replay a command that may have side effects. "
                "Trusted extensions, hooks, MCP services and model transport are outside this boundary."
                "</execution_boundary>")


def install_resource_tool(api, provider):
    tool = AgentTool(
        name="request_permissions", label="Request resources", execution_scope="broker",
        description="Request session or persistent read/write path or network access for Shell/managed operations.",
        parameters={"type": "object", "properties": {
            "tool": {"type": "string"}, "arguments": {"type": "object"}, "reason": {"type": "string", "minLength": 1},
            "permissions": {"type": "object", "properties": {
                "read_paths": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "write_paths": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "network_allowed": {"type": "boolean",
                                    "description": "Request Shell/task network access."},
            }, "additionalProperties": False},
        }, "required": ["reason", "permissions"], "additionalProperties": False},
        execute=provider.request_permissions, side_effect="read", sequential=True,
    )
    api.register_tool(tool, roles=("main", "subagent"))
    return tool
