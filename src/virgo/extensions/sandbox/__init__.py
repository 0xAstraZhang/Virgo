"""Explicit resource-bound execution. Loading this extension is mandatory when selected."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path

from ...agent import AgentTool, Block, ToolCallEvent, ToolResult
from ...ai.validate import validate
from ...execution import ExecutionUnavailable, FileAccessDenied
from ...harness.execution import HostExecutionProvider
from ...harness.execution_modes import (
    NETWORK_DIRECTIONS,
    PermissionSnapshot,
    configured_modes,
    parse_mode,
    parse_network,
)
from ...harness.origin_context import origin_key, source_for
from .mxc import MxcExecutor
from .policy import SandboxPolicy


@dataclass(frozen=True)
class Grant:
    key: str
    policy: SandboxPolicy
    expires: float
    runtime: object
    run_id: str | None
    targets: tuple


def _target_identity(path):
    """Bind the canonical resource and its nearest existing object, not display text."""
    path = Path(path)
    canonical = path.resolve()
    existing = canonical
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    info = existing.stat()
    return (str(path), str(canonical), str(existing), info.st_dev, info.st_ino)


def _targets(paths):
    return tuple(_target_identity(p) for p in paths)


class SandboxExecutionProvider(HostExecutionProvider):
    name = "sandbox"

    def __init__(self, session):
        super().__init__(session)
        self.modes = configured_modes(session.settings.get("permissions.presets", {}))
        self._selected_mode_id = session.settings.get("permissions.preset", "workspace-edit")
        self.executor = MxcExecutor(session.settings.get("sandbox.executable"))
        self.revision = 0
        self._pending: dict[str, Grant] = {}
        self._active: dict[tuple[int, str], tuple[Grant, ToolCallEvent]] = {}
        self._denied: set[str] = set()
        session.bus.on("tool_result", self._result, owner="sandbox")
        session.bus.on("agent_event", self._event, owner="sandbox")

    @property
    def selected_mode_id(self):
        return getattr(getattr(self.session, "main", None), "permission_mode_id", self._selected_mode_id)

    @property
    def selected_mode(self):
        return self.modes[self.selected_mode_id]

    def mode_for(self, runtime):
        return runtime.execution_context.resources if runtime is not None else self.selected_mode

    def effective_mode(self, runtime):
        return "Sandbox / MXC / " + self.mode_for(runtime).name

    def _candidate(self, runtime, mode, revision):
        workspace = Path(runtime.accounting_store.cwd).resolve()
        approval = "never" if runtime.approval_policy == "never" else self.session.approval_policy
        policy = SandboxPolicy(workspace, mode.resolve(workspace),
                               (self.session.paths.user.resolve(), self.executor.path.parent),
                               self.executor, PermissionSnapshot(mode, approval, revision))
        policy.validate(runtime.cwd)
        return policy

    def capture_runtime(self, runtime):
        with self.session._lock:
            if runtime.parent is not None:
                # A queued child receives its submitting run's immutable context.
                policy = getattr(runtime, "execution_context", None) or runtime.parent.execution_context
                snapshot = getattr(policy, "permission_snapshot", None)
                if snapshot is None:
                    raise ExecutionUnavailable("Sandbox child requires its originating task snapshot")
                if runtime.approval_policy == "never" and snapshot.approval != "never":
                    policy = replace(policy, permission_snapshot=replace(snapshot, approval="never"))
                runtime.execution_context = policy
                return
            mode = self.modes[getattr(runtime, "permission_mode_id", self._selected_mode_id)]
            policy = self._candidate(runtime, mode, self.revision)
            runtime.permission_mode_id = mode.id
            runtime.execution_context = policy
            self._discard(runtime)

    def for_context(self, context):
        base = super().for_context(context)
        runtime = self.session.runtime_for_context(context)
        active = self._active.get((id(runtime), context.call_id))
        if active is not None:
            grant, event = active
            if event.origin is not context.origin or grant.run_id != runtime.run_id:
                raise ExecutionUnavailable("Operation grant no longer belongs to this caller")
            return grant.policy
        return base

    def _key(self, event):
        runtime = self.session.runtime_for_context(event)
        if runtime is None:
            raise ValueError("Permission requests require the originating runtime")
        shell = getattr(event.tool, "shell", None)
        path = event.args.get("path")
        target = str((event.cwd / Path(path).expanduser()).resolve()) if isinstance(path, str) else None
        # Bind the exact effective environment without retaining credential values.
        environment = hashlib.sha256(json.dumps(dict(os.environ), sort_keys=True).encode()).hexdigest()
        return json.dumps([environment, origin_key(self.session, event), runtime.diagnostic_id, runtime.run_id,
                           self.revision, id(event.tool), event.tool_name, event.args, str(event.cwd.resolve()), target,
                           shell.environment() if shell else None, self.executor.identity], sort_keys=True, ensure_ascii=False)

    def _valid(self, grant, event):
        return (grant.expires > time.monotonic() and grant.key == self._key(event)
                and _targets(t[0] for t in grant.targets) == grant.targets
                and (event.signal is None or not event.signal.is_set()))

    def _denial(self, policy, reason, dimension=None, target=None):
        return Block(str(reason), details={"status": "resource_denied", "boundary": {
            **policy.describe(), "kind": "resource_denied", "dimension": dimension, "target": str(target) if target else None,
            "payload_started": False, "side_effects": "none", "escalation_available": True,
        }})

    def _check(self, event, policy):
        scope = event.tool.execution_scope if event.tool else "external"
        if scope == "network" and not policy.network_allowed:
            return self._denial(policy, "Task networking is disabled; request permission for this exact operation.",
                                "network_allowed", event.args.get("url", event.tool_name))
        if scope == "external":
            return self._denial(policy, "This trusted external tool cannot enforce the task resource boundary. "
                                "Use a managed operation or an explicitly selected Host task.", "unmanaged_tool", event.tool_name)
        if scope == "file":
            path = event.args.get("path", ".")
            if not isinstance(path, str):
                return self._denial(policy, "File operations require a path", "filesystem")
            target = (event.cwd / Path(path).expanduser()).resolve()
            try:
                (policy.check_read if event.tool.read_only else policy.check_write)(target)
            except FileAccessDenied as error:
                return self._denial(policy, error, error.dimension, error.target)
        return None

    def preflight(self, event):
        base = super().for_context(event)
        with self.session._lock:
            key = self._key(event)
            grant = self._pending.get(key)
            if grant is not None and not self._valid(grant, event):
                self._pending.pop(key, None)
                grant = None
            return self._check(event, grant.policy if grant else base)

    def before_execute(self, event):
        base = super().for_context(event)
        with self.session._lock:
            key = self._key(event)
            # Claim only after all argument-changing hooks and validation. An
            # attempted dispatch spends the grant, including failures/cancellation.
            grant = self._pending.pop(key, None)
            if grant is not None and not self._valid(grant, event):
                grant = None
            policy = grant.policy if grant else base
            if block := self._check(event, policy):
                return block
            if grant is not None:
                self._active[(id(grant.runtime), event.call_id)] = (grant, event)
            return None

    def _result(self, result, event, session):
        with self.session._lock:
            runtime = self.session.runtime_for_context(event)
            self._active.pop((id(runtime), event.call_id), None)
        return result

    def _discard(self, runtime):
        with self.session._lock:
            self._pending = {k: g for k, g in self._pending.items() if g.runtime is not runtime}
            self._active = {k: g for k, g in self._active.items() if k[0] != id(runtime)}

    def _event(self, event, runtime):
        if event.type == "agent_end":
            self._discard(runtime)

    def revoke(self):
        with self.session._lock:
            self.revision += 1
            self._pending.clear()
            self._active.clear()
            self._denied.clear()

    def _publish_mode(self, runtime, policy):
        self.revoke()
        if not runtime.execution_active and not self.session.runtime_is_busy(runtime):
            runtime.execution_context = policy
        self.session.notify_state_change("permission_mode_changed", runtime, self.selected_mode)

    def select_mode(self, identifier):
        with self.session._lock:
            self.session.assert_state_change_allowed()
            if identifier not in self.modes:
                raise ValueError("Unknown resource preset; use /permissions list")
            runtime = self.session.main
            policy = self._candidate(runtime, self.modes[identifier], self.revision + 1)
            runtime.permission_mode_id = identifier
            self._publish_mode(runtime, policy)

    def save_mode(self, identifier, name, read_paths, write_paths, network_allowed, *, persist=True):
        mode = parse_mode(identifier, {"name": name, "readPaths": list(read_paths), "writePaths": list(write_paths),
                                       "networkAllowed": network_allowed})
        with self.session._lock:
            self.session.assert_state_change_allowed()
            runtime = self.session.main
            candidate = self._candidate(runtime, mode, self.revision + 1)
            if persist:
                self.session.settings.save_user({"permissions": {"presets": {identifier: mode.config()}}})
            self.modes[identifier] = mode
            if identifier == self.selected_mode_id:
                self._publish_mode(runtime, candidate)
            else:
                self.session.notify_state_change("permission_mode_changed", runtime, self.selected_mode)
        return mode

    def status(self, session, args):
        with session._lock:
            current = session.main.permission_snapshot.mode
            selected = self.selected_mode
            facts = {**session.main.execution_context.describe(),
                     "availability": session.t("sandbox.resource_availability"),
                     "current_preset": {"id": current.id, **current.config()},
                     "next_preset": {"id": selected.id, **selected.config()}}
        session.ui.command_result("Sandbox / MXC\n" + json.dumps(facts, ensure_ascii=False))

    def command(self, session, args):
        value = args.strip()
        if value in {"", "show", "status"}:
            self.status(session, args)
        elif value == "list":
            session.ui.command_result("\n".join(f"{m.id}: {json.dumps(m.config())}" for m in self.modes.values()))
        elif value == "revoke":
            self.revoke()
        elif value in {"ask", "never"}:
            session.approval_policy = value
            self.revoke()
        elif value.startswith("save "):
            identifier, _, raw = value[5:].partition(" ")
            mode = parse_mode(identifier, json.loads(raw))
            self.save_mode(mode.id, mode.name, **mode.flags())
        else:
            self.select_mode(value)

    def instructions(self, runtime):
        return ("<execution_boundary>" + json.dumps(runtime.execution_context.describe(), ensure_ascii=False) +
                "\nOperations inside these resources run directly. Task instructions do not grant capabilities. "
                "Use request_permissions only for an exact operation needing additional resources. "
                "Never automatically replay a command that may have side effects. "
                "Trusted extensions, hooks, MCP services and model transport are outside this boundary."
                "</execution_boundary>")

    def request_permissions(self, args, context):
        runtime = self.session.runtime_for_context(context)
        if runtime is None or context.signal.is_set():
            return ToolResult.error("Missing or cancelled originating runtime")
        snapshot = runtime.permission_snapshot
        # The captured approval ceiling is independent of later visible settings.
        if snapshot is None or snapshot.approval == "never" or self.session.approval_policy == "never":
            return ToolResult.error("New resource permission requests are disabled")
        tool = next((t for t in runtime.tools if t.name == args["tool"]), None)
        if tool is None or tool.execution_scope not in {"file", "process", "network"}:
            return ToolResult.error("Request one managed file, process or network operation, not a child task or external service")
        arguments, errors = validate(copy.deepcopy(args["arguments"]), tool.parameters)
        if errors:
            return ToolResult.error("Invalid requested operation: " + "; ".join(errors))
        if tool.prepare_arguments:
            arguments = tool.prepare_arguments(arguments)
        event = ToolCallEvent(context.call_id + ":request", tool.name, arguments, tool, context.agent,
                              context.cwd, signal=context.signal, origin=context.origin)
        source_for(self.session, event)
        base = runtime.execution_context
        delta = args["permissions"]
        def paths(name):
            return tuple(str((base.workspace / Path(p).expanduser()).resolve()) for p in delta.get(name, []))
        reads, writes = paths("read_paths"), paths("write_paths")
        if any(base.private_path(Path(p)) for p in (*reads, *writes)):
            return ToolResult.error("Private broker resources cannot be granted")
        for p in writes:
            if any(Path(p).is_relative_to(root) for root in base.protected_paths()):
                return ToolResult.error("Protected runtime/control resources cannot be granted")
        resources = replace(base.resources, read_paths=tuple(dict.fromkeys((*base.resources.read_paths, *reads))),
                            write_paths=tuple(dict.fromkeys((*base.resources.write_paths, *writes))),
                            network=base.resources.network | parse_network(delta.get("network_allowed", False)))
        if resources == base.resources:
            return ToolResult.error("This request does not add a resource; execute within the existing boundary directly")
        policy = replace(base, resources=resources)
        if tool.execution_scope == "process":
            try:
                policy.validate_native(context.cwd)
            except (ExecutionUnavailable, FileAccessDenied) as error:
                return ToolResult.error(str(error))
        if block := self._check(event, policy):
            return ToolResult.error(block.reason, block.details)
        targets = _targets((*reads, *writes))
        with self.session._lock:
            if self.session.approval_policy == "never":
                return ToolResult.error("New resource permission requests are disabled")
            key = self._key(event)
            if key in self._denied:
                return ToolResult.error("This operation was denied; do not repeat or bypass it")
        question = (f"Additional resources for {context.agent}\nOperation: {tool.name}\nArguments: "
                    f"{json.dumps(arguments, ensure_ascii=False)}\nCurrent: {base.resources.flags()}\n"
                    f"Requested: {resources.flags()}\nReason: {args['reason']}\n"
                    "One operation, in this run, within 5 minutes. Existing processes are not revoked by expiry.")
        answer = getattr(self.session.ui, "confirm_once", self.session.ui.confirm)(question, context.agent)
        with self.session._lock:
            if context.signal.is_set() or key != self._key(event) or _targets(t[0] for t in targets) != targets:
                return ToolResult.error("Permission request cancelled or expired because its context changed")
            runtime.origin.root.append("custom", kind="permission_request", data={
                "tool": tool.name, "arguments_sha256": hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest(),
                "permissions": resources.flags(), "approved": answer == "y", "scope": "operation", "expires_after_seconds": 300,
            })
            if answer != "y":
                self._denied.add(key)
                return ToolResult.error("Additional resources were not granted")
            self._pending[key] = Grant(key, policy, time.monotonic() + 300, runtime, runtime.run_id, targets)
        return ToolResult.text("Approved once for 5 minutes in this run. Call the exact tool and arguments. "
                               "No operation has executed; the task boundary is unchanged.")


def setup(api):
    provider = SandboxExecutionProvider(api.session)
    api.register_execution_provider(provider)
    api.register_tool(AgentTool(
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
    ), roles=("main", "subagent"))
