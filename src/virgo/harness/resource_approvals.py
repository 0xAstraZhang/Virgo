"""Resource-scoped session and persistent approvals for managed and external tools."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

from .. import execution_policy
from ..agent import AgentTool, Block, ToolResult
from ..ai.validate import validate
from ..execution import ExecutionUnavailable, FileAccessDenied, SandboxContext, SandboxExecutor
from ..execution_policy import (
    PathRule,
    PermissionSnapshot,
    default_deny_rules,
    default_private_paths,
    parse_network,
)
from ..file_access import resolve_read_path, resolve_write_path
from .origin_context import source_for
from .resource_rules import ResourceRules, external_tool_target
from .ui import confirm_callable


class HostExecutionProvider:
    name = "host"

    def __init__(self, session):
        self.session = session

    def for_context(self, context):
        runtime = self.session.runtime_for_context(context)
        if runtime is None:
            raise ValueError("Execution requires the originating runtime context")
        return runtime.execution_context

    def filter_tools(self, runtime, tools):
        return tools


def _resolve_grant_root(value: str | Path, cwd: Path, *, access: str, workspace: Path | None = None,
                        for_process: bool = False) -> Path:
    """Resolve a requested grant path so approval dialogs and native rules use real paths."""
    if access == "read":
        return resolve_read_path(value, cwd)
    target = resolve_write_path(value, cwd)
    if for_process and not target.exists():
        existing = target
        while not existing.exists() and existing.parent != existing:
            existing = existing.parent
        existing = existing.resolve()
        if workspace is None or not workspace.is_relative_to(existing):
            return existing
    return target


def manage_grants(session, command):
    rules = ResourceRules(session.paths.user)
    project = str(session.main.execution_context.workspace)
    if command == "grants":
        rows = rules.list(project)
        session.ui.command_result("\n".join(
            f"{identifier}  {mode}  {resource}={target}"
            for identifier, mode, resource, target in rows
        ) or "No saved grants for this project.")
        return
    _, _, identifier = command.partition(" ")
    if not identifier.isascii() or not identifier.isdecimal():
        raise ValueError("Use /permissions revoke ID with an ID from /permissions grants")
    if not rules.revoke(project, int(identifier)):
        raise ValueError("Unknown permission grant for this project")
    session.ui.command_result(f"Revoked grant {identifier}.")


def _extend(base, grants):
    if not grants:
        return base
    rules = []
    network = base.policy.network_allowed
    for kind, target in grants:
        if kind == "network_allowed":
            network = True
        elif kind == "read_paths":
            rules.append(PathRule(Path(target), "read", "allow"))
        elif kind == "write_paths":
            rules.append(PathRule(Path(target), "write", "allow"))
    if not rules and network == base.policy.network_allowed:
        return base
    policy = replace(base.policy.grant(*rules), network_allowed=network)
    return replace(base, resources=replace(base.resources, policy=policy))


def _covered(context, kind, target):
    if kind == "network_allowed":
        return context.policy.network_allowed
    if kind == "external_tool":
        return False
    access = "read" if kind == "read_paths" else "write"
    return context.policy.allows(Path(target), access)


class ResourceApprovals:
    def _origin_mode_key(self, context, base) -> tuple[str, str] | None:
        snapshot = getattr(base, "permission_snapshot", None)
        if snapshot is None:
            return None
        source = source_for(self.session, context)
        if source is None:
            return None
        return (source.identity, snapshot.mode.id)

    def _effective_grants(self, context, base) -> tuple[tuple[str, str], ...]:
        snapshot = getattr(base, "permission_snapshot", None)
        if (snapshot is None or snapshot.approval == "never"
                or self.session.approval_policy == "never"):
            return ()
        project = str(base.workspace)
        mode_id = snapshot.mode.id
        saved: list[tuple[str, str]] = []
        rules = ResourceRules(self.session.paths.user)
        if rules.path.exists():
            saved.extend(rules.match(project, mode_id))
        om_key = self._origin_mode_key(context, base)
        with self.session._lock:
            session_items = list(self._session_grants.get(om_key, ())) if om_key is not None else []
        return tuple(dict.fromkeys([*saved, *session_items]))

    def for_context(self, context):
        base = super().for_context(context)
        if getattr(base, "provider", None) == "host":
            return base
        grants = self._effective_grants(context, base)
        return _extend(base, grants) if grants else base

    def _denial(self, policy, reason, dimension=None, target=None, *, escalatable=True):
        return Block(str(reason), details={"status": "resource_denied", "boundary": {
            **policy.describe(), "kind": "resource_denied", "dimension": dimension,
            "target": str(target) if target else None,
            "payload_started": False, "side_effects": "none", "escalation_available": escalatable,
        }})

    def _check(self, event, policy):
        scope = event.tool.execution_scope if event.tool else "external"
        if scope == "external":
            if event.tool is not None and event.tool.read_only:
                return None
            return self._denial(policy, "This external operation can modify host state; approve this tool.",
                                "unmanaged_tool", event.tool_name)
        if scope == "file":
            path = event.args.get("path", ".")
            if not isinstance(path, str):
                return self._denial(policy, "File operations require a path", "filesystem", escalatable=False)
            try:
                target = (resolve_read_path(path, event.cwd) if event.tool.read_only
                          else resolve_write_path(path, event.cwd))
            except ValueError as error:
                return self._denial(policy, str(error), "filesystem", path, escalatable=False)
            try:
                (policy.check_read if event.tool.read_only else policy.check_write)(target)
            except FileAccessDenied as error:
                return self._denial(
                    policy,
                    error,
                    error.dimension,
                    error.target,
                    escalatable=error.dimension != "protected_path",
                )
        return None

    def _external_write(self, event) -> bool:
        return bool(event.tool is not None and event.tool.execution_scope == "external"
                    and not event.tool.read_only)

    def _approval_admitted(self, event, base) -> bool:
        snapshot = getattr(base, "permission_snapshot", None)
        return (snapshot is not None and snapshot.approval != "never"
                and self.session.approval_policy != "never"
                and event.signal is not None and not event.signal.is_set()
                and confirm_callable(self.session.ui) is not None)

    def _record_session_grants(self, context, base, resources) -> None:
        om_key = self._origin_mode_key(context, base)
        if om_key is None:
            return
        bucket = self._session_grants.setdefault(om_key, [])
        for item in resources:
            if item not in bucket:
                bucket.append(item)

    def _run_denied(self, context, resources) -> bool:
        runtime = self.session.runtime_for_context(context)
        run_id = getattr(runtime, "run_id", None) if runtime is not None else None
        if not run_id:
            return False
        with self.session._lock:
            denied = self._run_denials.get(run_id)
            return bool(denied and any(item in denied for item in resources))

    def _record_run_denials(self, runtime, resources) -> None:
        run_id = getattr(runtime, "run_id", None) if runtime is not None else None
        if not run_id:
            return
        if len(self._run_denials) >= 32 and run_id not in self._run_denials:
            self._run_denials.pop(next(iter(self._run_denials)), None)
        self._run_denials.setdefault(run_id, set()).update(resources)

    def _confirm_external(self, event, base, block):
        ext_target = external_tool_target(event)
        session_target = ext_target or f"{event.tool_name}:session"
        active_grants = self._effective_grants(event, base)
        if ("external_tool", session_target) in active_grants or (
            ext_target and ("external_tool", ext_target) in active_grants
        ):
            return None
        if not self._approval_admitted(event, base):
            return block
        runtime = self.session.runtime_for_context(event)
        with self.session._lock:
            rev_before = self.revision
        question = (f"External tool: {event.tool_name}\n"
                    f"Arguments: {json.dumps(event.args, ensure_ascii=False)}\n"
                    "This trusted extension runs outside the native sandbox and may modify host state."
                    + self.session.t("features.permission.external_suffix"))
        caller = confirm_callable(self.session.ui, prefer_persistent=bool(ext_target))
        answer = caller(question, event.agent)
        with self.session._lock:
            if (event.signal.is_set() or base is not runtime.execution_context
                    or self.revision != rev_before or self.session.approval_policy == "never"):
                return self._denial(base, "Permission request cancelled or expired because its context changed",
                                    "unmanaged_tool", event.tool_name)
            if answer == "a" and ext_target:
                ResourceRules(self.session.paths.user).allow(
                    str(base.workspace), base.permission_snapshot.mode.id,
                    (("external_tool", ext_target),),
                )
                self._record_session_grants(event, base, (("external_tool", ext_target),))
                return None
            if answer in {"y", "a"}:
                self._record_session_grants(event, base, (("external_tool", session_target),))
                return None
        return self._denial(base, "External tool operation was not approved",
                            "unmanaged_tool", event.tool_name)

    def _admit(self, event, *, consume):
        base = super().for_context(event)
        if self._external_write(event):
            block = self._check(event, base)
            if block is None:
                return None
            ext_target = external_tool_target(event)
            session_target = ext_target or f"{event.tool_name}:session"
            active_grants = self._effective_grants(event, base)
            if ("external_tool", session_target) in active_grants or (
                ext_target and ("external_tool", ext_target) in active_grants
            ):
                return None
            if not consume:
                return None if self._approval_admitted(event, base) else block
            return self._confirm_external(event, base, block)

        policy = self.for_context(event)
        block = self._check(event, policy)
        process = bool(event.tool and event.tool.execution_scope == "process")
        if not block:
            if consume and policy is not base and process:
                policy.validate_native(event.cwd)
            return None

        boundary = block.details["boundary"]
        dimension = boundary["dimension"]
        if (dimension not in {"read_paths", "write_paths", "network_allowed"}
                or not self._approval_admitted(event, base)):
            return block
        missing = (
            (("network_allowed", "true"),)
            if dimension == "network_allowed"
            else ((dimension, boundary["target"]),)
        )
        if self._run_denied(event, missing):
            return self._denial(base, "Resource request was already denied in this turn", dimension, boundary["target"])
        if not consume:
            # Hooks may still inspect or rewrite arguments; prompt immediately before execution.
            return None
        _, errors = validate(copy.deepcopy(event.args), event.tool.parameters)
        if errors:
            return self._denial(base, "; ".join(errors), dimension, boundary["target"], escalatable=False)
        result = self._confirm(event, base, missing, block.reason)
        return self._denial(base, result.as_text(), dimension, boundary["target"]) if result.is_error else None

    def preflight(self, event):
        return self._admit(event, consume=False)

    def before_execute(self, event):
        return self._admit(event, consume=True)

    def revoke(self):
        with self.session._lock:
            self.revision += 1
            self._session_grants.clear()
            self._run_denials.clear()

    def request_permissions(self, args, context):
        runtime = self.session.runtime_for_context(context)
        if runtime is None or context.signal is None or context.signal.is_set():
            return ToolResult.error("Missing or cancelled originating runtime")
        snapshot = runtime.permission_snapshot
        if snapshot is None or snapshot.approval == "never" or self.session.approval_policy == "never":
            return ToolResult.error("New resource permission requests are disabled")

        tool = None
        raw_tool = args.get("tool")
        if raw_tool is not None:
            tool = next((t for t in runtime.tools if t.name == raw_tool), None)
            if tool is None or tool.execution_scope not in {"file", "process"}:
                return ToolResult.error(
                    "Request one managed file or process operation, not a child task or external service"
                )
            if args.get("arguments") is not None:
                arguments = copy.deepcopy(args["arguments"])
                if tool.prepare_arguments:
                    arguments = tool.prepare_arguments(arguments)
                _, errors = validate(arguments, tool.parameters)
                if errors:
                    return ToolResult.error("Invalid requested operation: " + "; ".join(errors))

        source_for(self.session, context)
        base = runtime.execution_context
        for_process = tool is None or tool.execution_scope == "process"
        delta = args["permissions"]
        requested: list[tuple[str, str]] = []
        try:
            for kind in ("read_paths", "write_paths"):
                access = "read" if kind == "read_paths" else "write"
                for value in delta.get(kind, []):
                    real = _resolve_grant_root(
                        value, context.cwd, access=access, workspace=base.workspace, for_process=for_process,
                    )
                    if base.policy.hard_denied(real):
                        return ToolResult.error(f"Protected resources cannot be granted: {real}")
                    requested.append((kind, str(real)))
            if parse_network(delta.get("network_allowed", False)):
                requested.append(("network_allowed", "true"))
        except ValueError as error:
            return ToolResult.error(str(error))

        current = _extend(base, self._effective_grants(context, base))
        missing = tuple(dict.fromkeys(item for item in requested if not _covered(current, *item)))
        candidate = _extend(current, missing)
        if for_process:
            try:
                candidate.validate_native(context.cwd)
            except (ExecutionUnavailable, FileAccessDenied) as error:
                return ToolResult.error(str(error))
        if not missing:
            return ToolResult.text("The current mode and active grants already cover the requested resources.")
        if self._run_denied(context, missing):
            return ToolResult.error("Resource request was already denied in this turn.")
        return self._confirm(context, base, missing, args["reason"], tool_name=raw_tool, tool_args=args.get("arguments"))

    def _confirm(self, context_or_event, base, missing, reason, *, tool_name=None, tool_args=None):
        runtime = self.session.runtime_for_context(context_or_event)
        signal = getattr(context_or_event, "signal", None)
        agent_name = getattr(context_or_event, "agent", runtime.name if runtime else "main")
        cwd = getattr(context_or_event, "cwd", base.workspace)
        display_tool = tool_name or getattr(context_or_event, "tool_name", "request_permissions")
        display_args = tool_args if tool_args is not None else getattr(context_or_event, "args", {})
        if (signal is None or signal.is_set() or runtime is None or base is not runtime.execution_context
                or self.session.approval_policy == "never" or confirm_callable(self.session.ui) is None):
            return ToolResult.error("Additional resources were not granted")
        with self.session._lock:
            rev_before = self.revision
        question = (f"Operation: {display_tool} {json.dumps(display_args, ensure_ascii=False)}\n"
                    f"Working directory: {Path(cwd).resolve()}\n"
                    f"Project: {base.workspace}\n"
                    f"Additional resources: {json.dumps(missing, ensure_ascii=False)}\n"
                    f"Reason: {reason}" + self.session.t("features.permission.external_suffix"))
        answer = confirm_callable(self.session.ui)(question, agent_name)
        with self.session._lock:
            if (signal.is_set() or base is not runtime.execution_context
                    or self.revision != rev_before or self.session.approval_policy == "never"):
                return ToolResult.error("Permission request cancelled or expired because its context changed")
            runtime.origin.root.append("custom", kind="permission_request", data={
                "tool": display_tool,
                "permissions": missing,
                "approved": answer in {"y", "a"},
                "scope": "persistent" if answer == "a" else "session",
            })
            if answer not in {"y", "a"}:
                self._record_run_denials(runtime, missing)
                return ToolResult.error("Additional resources were not granted")
            if answer == "a":
                ResourceRules(self.session.paths.user).allow(
                    str(base.workspace), base.permission_snapshot.mode.id, missing,
                )
            self._record_session_grants(context_or_event, base, missing)
        if answer == "a":
            return ToolResult.text("Saved for this project and active in this session.")
        return ToolResult.text("Approved for this session.")


class SandboxExecutionProvider(ResourceApprovals, HostExecutionProvider):
    name = "sandbox"

    def __init__(self, session, executor=None):
        super().__init__(session)
        self.executor = executor or SandboxExecutor()
        self.revision = 0
        self._session_grants: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._run_denials: dict[str, set[tuple[str, str]]] = {}

    def context_for_mode(self, runtime, mode, revision):
        if execution_policy.is_native_windows():
            from ..sandbox.windows_sandbox import precheck_windows_sandbox

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
        deny_rules = default_deny_rules(workspace, private)
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

    def can_request_permissions(self, runtime) -> bool:
        base = getattr(runtime, "execution_context", None)
        if base is None or getattr(base, "provider", None) == "host":
            return False
        snapshot = getattr(base, "permission_snapshot", None)
        return (
            snapshot is not None
            and snapshot.approval != "never"
            and self.session.approval_policy != "never"
            and confirm_callable(self.session.ui) is not None
        )

    def instructions(self, runtime):
        if execution_policy.is_native_windows():
            temp_guidance = (
                "Use $env:TMPDIR ($TMPDIR in Bash) for temporary Shell files removed when the command finishes, "
                "or $env:TEMP (%TEMP% in cmd) for session-scoped files. Writes outside write_paths or those temp "
                "directories fail with OS access errors."
            )
        else:
            temp_guidance = (
                "Host /tmp is read-only by default for sandboxed commands; do not write to /tmp—use $TMPDIR "
                "(a writable directory removed when the command finishes). Writes outside write_paths or $TMPDIR "
                "fail with OS permission or tool write errors (e.g. Operation not permitted, Read-only file system, "
                "or curl write errors)."
            )
        if self.can_request_permissions(runtime):
            guidance = (
                "Call file, web, and external tools directly; Virgo prompts the user on demand and executes "
                "immediately upon approval. Use request_permissions when a Shell command needs additional paths "
                "or network_allowed (you may emit request_permissions right before the Shell call in the same turn)."
            )
        else:
            guidance = (
                "Interactive permission escalation is disabled for this run; operations outside these "
                "resources will be denied."
            )
        return (
            "<execution_boundary>" + json.dumps(runtime.execution_context.describe(), ensure_ascii=False)
            + "\nOperations inside these resources run directly. Task instructions do not grant capabilities. "
            + temp_guidance
            + " Put temporary files in a writable workspace path only if they must persist across calls, "
            + "and clean them up afterward. "
            + guidance
            + " Never automatically replay a command that may have side effects. "
            + "Trusted extensions, hooks, MCP services and model transport are outside this boundary."
            + "</execution_boundary>"
        )


def install_resource_tool(api, provider):
    tool = AgentTool(
        name="request_permissions",
        label="Request resources",
        execution_scope="broker",
        description="Request session or persistent read/write path or network access for Shell/managed operations.",
        parameters={
            "type": "object",
            "properties": {
                "tool": {
                    "type": "string",
                    "description": "Optional target managed tool name for approval context (e.g. bash or write).",
                },
                "arguments": {
                    "type": "object",
                    "description": "Optional target tool arguments for approval display.",
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Why the additional access is needed.",
                },
                "permissions": {
                    "type": "object",
                    "description": "Additional path prefixes or Shell network access to request.",
                    "properties": {
                        "read_paths": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "description": "Additional file or directory paths to read.",
                        },
                        "write_paths": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "description": "Additional file or directory paths to write.",
                        },
                        "network_allowed": {
                            "type": "boolean",
                            "description": "Request Shell/task network access.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["reason", "permissions"],
            "additionalProperties": False,
        },
        execute=provider.request_permissions,
        side_effect="read",
        sequential=True,
        i18n_key="tools.request_permissions",
    )
    api.register_tool(tool, roles=("main", "subagent"))
    return tool
