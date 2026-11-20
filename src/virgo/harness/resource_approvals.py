"""Core authorization for an exact resource-bound tool operation."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path

from ..agent import Block, ToolCallEvent, ToolResult
from ..ai.validate import validate
from ..execution import ExecutionContext, ExecutionUnavailable, FileAccessDenied
from ..execution_policy import PathRule, lexical_path, parse_network
from ..file_access import resolve_read_path, resolve_write_path
from .origin_context import origin_key, source_for
from .resource_rules import ResourceRules, external_operation_id, operation_id
from .ui import UI


@dataclass(frozen=True)
class Grant:
    key: str
    policy: ExecutionContext
    expires: float
    runtime: object
    run_id: str | None
    targets: tuple
    saved: tuple = ()


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


def _resolve_grant_root(value: str | Path, cwd: Path) -> Path:
    """Resolve a requested grant root so approval dialogs and native rules use real paths (S3-02)."""
    return lexical_path(value, cwd).resolve()


def manage_grants(session, command):
    rules = ResourceRules(session.paths.user)
    project = str(session.main.execution_context.workspace)
    if command == "grants":
        rows = rules.list(project)
        session.ui.command_result("\n".join(
            f"{identifier}  {mode}  {cwd}  {op}  {resource}={target}"
            for identifier, mode, cwd, op, resource, target in rows
        ) or "No saved grants for this project.")
        return
    _, _, identifier = command.partition(" ")
    if not identifier.isascii() or not identifier.isdecimal():
        raise ValueError("Use /permissions revoke ID with an ID from /permissions grants")
    if not rules.revoke(project, int(identifier)):
        raise ValueError("Unknown permission grant for this project")
    session.ui.command_result(f"Revoked grant {identifier}.")


def _extend(base, grants):
    rules = []
    network = base.policy.network_allowed
    for kind, target in grants:
        if kind == "network_allowed":
            network = True
        elif kind == "read_paths":
            rules.append(PathRule(Path(target), "read", "allow"))
        elif kind == "write_paths":
            rules.append(PathRule(Path(target), "write", "allow"))
    policy = replace(base.policy.with_rules(*rules), network_allowed=network)
    return replace(base, resources=replace(base.resources, policy=policy))


def _covered(context, kind, target):
    if kind == "network_allowed":
        return context.policy.network_allowed
    access = "read" if kind == "read_paths" else "write"
    return context.policy.allows(Path(target), access)


class ResourceApprovals:
    def for_context(self, context):
        base = super().for_context(context)
        runtime = self.session.runtime_for_context(context)
        dispatch = context if isinstance(context, ToolCallEvent) else getattr(context, "dispatch_event", None)
        if dispatch is None:
            return base
        active = self._active.get((id(runtime), id(dispatch)))
        if active is not None:
            grant, event = active
            if event is not dispatch or event.origin is not context.origin or grant.run_id != runtime.run_id:
                raise ExecutionUnavailable("Operation grant no longer belongs to this caller")
            return grant.policy
        return base

    def _key(self, event):
        runtime = self.session.runtime_for_context(event)
        if runtime is None:
            raise ValueError("Permission requests require the originating runtime")
        shell = getattr(event.tool, "shell", None)
        path = event.args.get("path")
        target = None
        if isinstance(path, str):
            try:
                resolver = resolve_read_path if (event.tool and event.tool.read_only) else resolve_write_path
                target = str(resolver(path, event.cwd))
            except ValueError:
                target = path
        # Bind the exact effective environment without retaining credential values.
        environment = hashlib.sha256(json.dumps(dict(os.environ), sort_keys=True).encode()).hexdigest()
        return json.dumps([environment, origin_key(self.session, event), runtime.diagnostic_id, runtime.run_id,
                           id(event.tool), event.tool_name, event.args, str(event.cwd.resolve()), target,
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
            if event.tool is not None and event.tool.read_only:
                return None
            return self._denial(policy, "This external operation can modify host state; approve this exact call.",
                                "unmanaged_tool", event.tool_name)
        if scope == "file":
            path = event.args.get("path", ".")
            if not isinstance(path, str):
                return self._denial(policy, "File operations require a path", "filesystem")
            try:
                target = (resolve_read_path(path, event.cwd) if event.tool.read_only
                          else resolve_write_path(path, event.cwd))
            except ValueError as error:
                return self._denial(policy, str(error), "filesystem", path)
            try:
                (policy.check_read if event.tool.read_only else policy.check_write)(target)
            except FileAccessDenied as error:
                return self._denial(policy, error, error.dimension, error.target)
        return None

    def _scope(self, event, base):
        return (str(base.workspace), base.permission_snapshot.mode.id, str(event.cwd.resolve()), operation_id(event))

    def _saved(self, event, base):
        if (base.permission_snapshot.approval == "never" or self.session.approval_policy == "never"
                or not event.tool or event.tool.execution_scope not in {"file", "process", "network"}):
            return ()
        rules = ResourceRules(self.session.paths.user)
        if not rules.path.exists():
            return ()
        return tuple(rules.match(self._scope(event, base)))

    def _external_write(self, event) -> bool:
        return bool(event.tool is not None and event.tool.execution_scope == "external"
                    and not event.tool.read_only)

    def _confirm_callable(self):
        ui = self.session.ui
        once = getattr(ui, "confirm_once", None)
        if callable(once) and getattr(once, "__func__", None) is not UI.confirm_once:
            return once
        confirm = getattr(ui, "confirm", None)
        if callable(confirm) and getattr(confirm, "__func__", None) is not UI.confirm:
            return confirm
        return None

    def _external_admitted(self, event, base) -> bool:
        snapshot = base.permission_snapshot
        return (snapshot is not None and snapshot.approval != "never"
                and self.session.approval_policy != "never"
                and event.signal is not None and not event.signal.is_set()
                and self._confirm_callable() is not None)

    def _external_scope(self, event, base):
        return (str(base.workspace), base.permission_snapshot.mode.id,
                str(event.cwd.resolve()), external_operation_id(event))

    def _external_saved_match(self, event, base) -> bool:
        fingerprint = getattr(event.tool, "config_fingerprint", None)
        if not fingerprint:
            return False
        if base.permission_snapshot.approval == "never" or self.session.approval_policy == "never":
            return False
        rules = ResourceRules(self.session.paths.user)
        if not rules.path.exists():
            return False
        return ("external_tool", fingerprint) in rules.match(self._external_scope(event, base))

    def _confirm_external(self, event, base, block):
        fingerprint = getattr(event.tool, "config_fingerprint", None)
        if fingerprint and self._external_saved_match(event, base):
            return None
        if not self._external_admitted(event, base):
            return block
        question = (f"External tool: {event.tool_name}\n"
                    f"Arguments: {json.dumps(event.args, ensure_ascii=False)}\n"
                    "This trusted extension runs outside the native sandbox and may modify host state."
                    + self.session.t("features.permission.external_suffix"))
        if fingerprint:
            confirm = getattr(self.session.ui, "confirm", None)
            if callable(confirm) and getattr(confirm, "__func__", None) is not UI.confirm:
                answer = confirm(question, event.agent)
            else:
                answer = self._confirm_callable()(question, event.agent)
            if answer == "a":
                ResourceRules(self.session.paths.user).allow(
                    self._external_scope(event, base),
                    (("external_tool", fingerprint),),
                )
                return None
            if answer == "y":
                return None
        else:
            answer = self._confirm_callable()(question, event.agent)
            if answer == "y":
                return None
        return self._denial(base, "External tool operation was not approved",
                            "unmanaged_tool", event.tool_name)

    def _admit(self, event, *, consume):
        base = super().for_context(event)
        if self._external_write(event):
            block = self._check(event, base)
            if block is None:
                return None
            if self._external_saved_match(event, base):
                return None
            if not consume:
                return None if self._external_admitted(event, base) else block
            return self._confirm_external(event, base, block)
        with self.session._lock:
            grant = None
            if self._pending:
                key = self._key(event)
                grant = self._pending.pop(key, None) if consume else self._pending.get(key)
            block = self._check(event, base)
            process = event.tool and event.tool.execution_scope == "process"
            if grant is None and not block and not process:
                return None
            saved = self._saved(event, base)
            if grant and (not self._valid(grant, event) or not set(grant.saved).issubset(saved)):
                self._pending.pop(key, None)
                grant = None
            policy = grant.policy if grant else _extend(base, saved) if saved else base
            block = self._check(event, policy)
            if not block:
                if consume and policy is not base:
                    if process:
                        policy.validate_native(event.cwd)
                    runtime = self.session.runtime_for_context(event)
                    self._active[(id(runtime), id(event))] = (
                        grant or Grant("", policy, 0, runtime, runtime.run_id, ()), event)
                return None
            boundary = block.details["boundary"]
            dimension = boundary["dimension"]
            confirm = getattr(self.session.ui, "confirm", None)
            if (not event.tool or event.tool.execution_scope != "file" or dimension not in {"read_paths", "write_paths"}
                    or event.signal is None or event.signal.is_set()
                    or base.permission_snapshot.approval == "never" or self.session.approval_policy == "never"
                    or not callable(confirm) or getattr(confirm, "__func__", None) is UI.confirm):
                return block
            if not consume:
                # Hooks may still change arguments. Ask after their final form is known.
                return None
            _, errors = validate(copy.deepcopy(event.args), event.tool.parameters)
            if errors:
                return self._denial(base, "; ".join(errors), dimension, boundary["target"])
            # S3-02 & S3-03: Grant the single real target directly without requiring parent access.
            missing = ((dimension, boundary["target"]),)
            policy = _extend(policy, missing)
        # Waiting for a user must not hold the session lock.
        result = self._confirm(event, base, saved, policy, missing, block.reason, dispatch=True)
        return self._denial(base, result.as_text(), dimension, boundary["target"]) if result.is_error else None

    def preflight(self, event):
        return self._admit(event, consume=False)

    def before_execute(self, event):
        # Only the dispatch of the final, validated arguments consumes the grant.
        return self._admit(event, consume=True)

    def _result(self, result, event, session):
        with self.session._lock:
            runtime = self.session.runtime_for_context(event)
            self._active.pop((id(runtime), id(event)), None)
        return result

    def _discard(self, runtime):
        with self.session._lock:
            self._pending = {k: g for k, g in self._pending.items() if g.runtime is not runtime}
            self._active = {k: g for k, g in self._active.items() if k[0] != id(runtime)}
            self._denied = {key: owner for key, owner in self._denied.items() if owner is not runtime}

    def _event(self, event, runtime):
        if event.type == "agent_end":
            self._discard(runtime)

    def revoke(self):
        with self.session._lock:
            self.revision += 1
            self._pending.clear()
            self._active.clear()
            self._denied.clear()

    def request_permissions(self, args, context):
        runtime = self.session.runtime_for_context(context)
        if runtime is None or context.signal is None or context.signal.is_set():
            return ToolResult.error("Missing or cancelled originating runtime")
        snapshot = runtime.permission_snapshot
        # The captured approval ceiling is independent of later visible settings.
        if snapshot is None or snapshot.approval == "never" or self.session.approval_policy == "never":
            return ToolResult.error("New resource permission requests are disabled")
        tool = next((t for t in runtime.tools if t.name == args["tool"]), None)
        if tool is None or tool.execution_scope not in {"file", "process", "network"}:
            return ToolResult.error("Request one managed file, process or network operation, not a child task or external service")
        arguments = copy.deepcopy(args["arguments"])
        if tool.prepare_arguments:
            arguments = tool.prepare_arguments(arguments)
        arguments, errors = validate(arguments, tool.parameters)
        if errors:
            return ToolResult.error("Invalid requested operation: " + "; ".join(errors))
        event = ToolCallEvent(context.call_id + ":request", tool.name, arguments, tool, context.agent,
                              context.cwd, signal=context.signal, origin=context.origin)
        source_for(self.session, event)
        base = runtime.execution_context
        delta = args["permissions"]
        requested = []
        try:
            for kind in ("read_paths", "write_paths"):
                access = "read" if kind == "read_paths" else "write"
                for value in delta.get(kind, []):
                    # S3-02: Broker resolves grant roots so symlinks like /tmp -> /private/tmp
                    # display and compile using their real path.
                    real = _resolve_grant_root(value, event.cwd)
                    if base.policy.denied(real, access):
                        return ToolResult.error(f"Protected resources cannot be granted: {real}")
                    requested.append((kind, str(real)))
            if parse_network(delta.get("network_allowed", False)):
                requested.append(("network_allowed", "true"))
        except ValueError as error:
            return ToolResult.error(str(error))
        saved = self._saved(event, base)
        current = _extend(base, saved)
        missing = tuple(dict.fromkeys(item for item in requested if not _covered(current, *item)))
        policy = _extend(current, missing)
        if tool.execution_scope == "process":
            try:
                policy.validate_native(context.cwd)
            except (ExecutionUnavailable, FileAccessDenied) as error:
                return ToolResult.error(str(error))
        if block := self._check(event, policy):
            return ToolResult.error(block.reason, block.details)
        if not missing:
            return ToolResult.text("The current mode and saved grants already cover the requested resources.")
        return self._confirm(event, base, saved, policy, missing, args["reason"], dispatch=False)

    def _confirm(self, event, base, saved, policy, missing, reason, *, dispatch):
        runtime = self.session.runtime_for_context(event)
        scope = self._scope(event, base)
        if (event.signal is None or event.signal.is_set() or base is not runtime.execution_context
                or self.session.approval_policy == "never"):
            return ToolResult.error("The operation's permission context is no longer active")
        targets = _targets(path for kind, path in missing if kind != "network_allowed")
        with self.session._lock:
            key = self._key(event)
            if key in self._denied:
                return ToolResult.error("This operation was denied; do not repeat or bypass it")
        question = (f"Operation: {event.tool_name} {json.dumps(event.args, ensure_ascii=False)}\n"
                    f"Working directory: {event.cwd.resolve()}\n"
                    f"Additional resources: {json.dumps(missing, ensure_ascii=False)}\n"
                    f"Reason: {reason}" + self.session.t("features.permission.external_suffix"))
        answer = self.session.ui.confirm(question, event.agent)
        with self.session._lock:
            if (event.signal.is_set() or base is not runtime.execution_context or key != self._key(event) or self.session.approval_policy == "never"
                    or _targets(t[0] for t in targets) != targets or saved != self._saved(event, base)):
                return ToolResult.error("Permission request cancelled or expired because its context changed")
            runtime.origin.root.append("custom", kind="permission_request", data={
                "tool": event.tool_name, "arguments_sha256": hashlib.sha256(scope[-1].encode()).hexdigest(),
                "permissions": missing, "approved": answer in {"y", "a"},
                "scope": "persistent" if answer == "a" else "operation",
                "expires_after_seconds": 300 if answer == "y" and not dispatch else None,
            })
            if answer not in {"y", "a"}:
                self._denied[key] = runtime
                return ToolResult.error("Additional resources were not granted")
            if answer == "a":
                ResourceRules(self.session.paths.user).allow(scope, missing)
            grant = Grant(key, policy, time.monotonic() + 300, runtime, runtime.run_id, targets, saved)
            if dispatch:
                self._active[(id(runtime), id(event))] = grant, event
                return ToolResult.text("Authorized for dispatch")
            if answer == "a":
                return ToolResult.text("Saved for this exact operation. No operation has executed.")
            self._pending[key] = grant
        return ToolResult.text("Approved once for 5 minutes in this run. Call the exact tool and arguments. "
                               "No operation has executed; the task boundary is unchanged.")
