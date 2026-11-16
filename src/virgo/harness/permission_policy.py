"""Deterministic authorization, independent work intent and inherited capabilities."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent.types import Block, ToolCallEvent
from ..tools.fs import IGNORE_DIRS, glob_match
from ..tools.shell import SHELL_TOOLS
from .origin_context import origin_key, origin_store, origin_workspace, source_for
from .permission_scope import ApprovalBoundary, command_boundary, command_path_targets, inside
from .permissions import (
    approval_policy_label,
    normalize_approval_policy,
    read_only_tool_allowed,
)
from .work_modes import normalize_work_mode

if TYPE_CHECKING:
    from .session import AgentSession

def parse_rule(rule: str) -> tuple[str, str | None]:
    m = re.fullmatch(r"\s*([^()\s]+)\s*(?:\((.*)\))?\s*", rule)
    if not m:
        return rule, None
    return m.group(1), m.group(2)


def _subjects(ev: ToolCallEvent) -> list[str]:
    a = ev.args
    if ev.tool_name in SHELL_TOOLS:
        return [a.get("command", "")]
    if ev.tool_name == "web_fetch" and isinstance(a.get("url"), str):
        return [a["url"]]
    if ev.tool_name == "web_search" and isinstance(a.get("query"), str):
        return [a["query"]]
    if isinstance(a.get("path"), str):
        p = Path(os.path.expanduser(a["path"]))
        absolute = (p if p.is_absolute() else ev.cwd / p).resolve()
        out = [a["path"], str(absolute)]
        try:
            out.append(str(absolute.relative_to(ev.cwd.resolve())))
        except ValueError:
            pass
        return out
    return [json.dumps(a, sort_keys=True, default=str)]


def rule_matches(rule: str, ev: ToolCallEvent, *, allow: bool = False) -> bool:
    tool, pattern = parse_rule(rule)
    if not fnmatch.fnmatchcase(ev.tool_name, tool):
        return False
    if pattern is None:
        return True
    if allow and ev.tool_name in SHELL_TOOLS:
        # A command grant is exact; shell text prefixes are not argument boundaries.
        return ev.args.get("command", "") == pattern
    pats = [pattern, os.path.expanduser(pattern)]
    return any(fnmatch.fnmatchcase(s, p) for s in _subjects(ev) for p in pats)


class Permissions:
    def __init__(self, session: AgentSession):
        self.session = session
        self._work_mode = normalize_work_mode(session.settings.get("workMode", "default"))
        self.revision = 0
        self.session_rules: list[str] = []
        self.session_approvals: set[str] = set()
        self.denials: dict[str, str] = {}

    @property
    def work_mode(self) -> str:
        return self.work_mode_for(getattr(self.session, "main", None))

    def _root_runtime(self, runtime):
        if runtime is not None and runtime.session is not self.session:
            raise ValueError("Work mode runtime must belong to this session")
        while runtime is not None and runtime.parent is not None:
            runtime = runtime.parent
        return runtime

    def work_mode_for(self, runtime) -> str:
        root = self._root_runtime(runtime)
        return root.work_mode if root is not None else self._work_mode

    def is_read_only(self, runtime) -> bool:
        if self.work_mode_for(runtime) == "plan":
            return True
        while runtime is not None:
            if runtime.read_only:
                return True
            runtime = runtime.parent
        return False

    def approval_for(self, runtime=None) -> str:
        approval = normalize_approval_policy(self.session.approval_policy)
        while runtime is not None:
            if runtime.session is not self.session:
                raise ValueError("Approval runtime must belong to this session")
            ceiling = runtime.approval_policy
            if ceiling is not None and normalize_approval_policy(ceiling) == "never":
                approval = "never"
            runtime = runtime.parent
        return approval

    def restore_work_mode(self, runtime, override: str | None = None) -> None:
        """Restore work intent only, publishing it after persistence succeeds."""
        records = runtime.store.custom("work_mode") if runtime.store is not None else []
        saved = normalize_work_mode(records[-1]["mode"]) if records else None
        mode = normalize_work_mode(override) if override is not None else (
            "plan" if self._work_mode == "plan" else saved or self._work_mode
        )
        if runtime.store is not None and saved != mode and (saved is not None or mode != "default"):
            runtime.store.append("custom", kind="work_mode", data={"mode": mode})
        runtime.work_mode = mode
        runtime.work_mode_revision = getattr(runtime, "work_mode_revision", -1) + 1

    def set_work_mode(self, mode: str, *, runtime=None) -> None:
        mode = normalize_work_mode(mode)
        with self.session._lock:
            self.session.assert_state_change_allowed()
            root = self._root_runtime(runtime or getattr(self.session, "main", None))
            if self.work_mode_for(root) == mode:
                return
            if root is not None and self.session.runtime_is_busy(root):
                raise ValueError(self.session.t("work_mode.busy"))
            if root is not None and root.store is not None:
                root.store.append("custom", kind="work_mode", data={"mode": mode})
            if root is None:
                self._work_mode = mode
            else:
                root.work_mode = mode
                root.work_mode_revision += 1
            self.revision += 1
            self.session.notify_state_change("work_mode_changed", root, mode)

    def _event_runtime(self, event):
        source = source_for(self.session, event)
        if source is None:
            raise ValueError("Permission checks require the originating runtime context")
        runtime = self.session.runtime_for_context(event)
        if runtime is None:
            raise ValueError("Permission checks require the originating runtime context")
        return runtime

    def _approval_key(self, event: ToolCallEvent) -> str:
        path = event.args.get("path")
        target = str((event.cwd / os.path.expanduser(path)).resolve()) if isinstance(path, str) else None
        if event.tool_name == "bash":
            target = command_path_targets(event.args.get("command", ""), event.cwd)
        shell = getattr(event.tool, "shell", None)
        return json.dumps([
            origin_key(self.session, event), event.tool_name, str(event.cwd.resolve()),
            event.args, target, shell.environment() if shell else None,
        ], sort_keys=True, ensure_ascii=False)

    def _snapshot(self, event: ToolCallEvent) -> tuple:
        runtime = self._event_runtime(event)
        root = self._root_runtime(runtime)
        return (
            self.approval_for(runtime), self.is_read_only(runtime), self._approval_key(event),
            root.work_mode_revision if root is not None else self.revision,
        )

    def _boundary(self, ev: ToolCallEvent) -> ApprovalBoundary | None:
        workspace = origin_workspace(self.session, ev)
        if ev.tool_name == "powershell":
            return ApprovalBoundary("features.permission.scope.powershell")
        if ev.tool_name == "bash":
            boundary = command_boundary(ev.args.get("command", ""), ev.cwd, workspace)
            sandbox = self.session.services.get("sandbox")
            if boundary is None and (sandbox is None or sandbox.mode == "disabled"):
                return ApprovalBoundary("features.permission.scope.host_shell")
            return boundary
        if ev.tool_name in {"read", "write", "edit", "glob", "grep"}:
            path = ev.args.get("path", ".")
            if not isinstance(path, str) or not inside(path, ev.cwd, workspace):
                target = (ev.cwd / os.path.expanduser(path)).resolve() if isinstance(path, str) else path
                return ApprovalBoundary("features.permission.scope.action_path", {"path": target})
            if ev.tool_name in {"glob", "grep"}:
                root = (ev.cwd / os.path.expanduser(path)).resolve()
                try:
                    for directory, names, files in os.walk(root):
                        names[:] = [name for name in names if name not in IGNORE_DIRS]
                        for name in files:
                            candidate = Path(directory) / name
                            # Mirror the tools' own file selection (same matcher, same base path).
                            if ev.tool_name == "glob":
                                rel = candidate.relative_to(root).as_posix()
                                if not glob_match(rel, ev.args.get("pattern", "*")):
                                    continue
                            elif pattern := ev.args.get("glob"):
                                try:
                                    displayed = candidate.relative_to(ev.cwd.resolve())
                                except ValueError:
                                    displayed = candidate
                                if not glob_match(displayed.as_posix(), pattern):
                                    continue
                            if candidate.is_symlink() and not inside(str(candidate), ev.cwd, workspace):
                                return ApprovalBoundary(
                                    "features.permission.scope.search_link",
                                    {"path": candidate, "target": candidate.resolve()},
                                )
                except (OSError, RuntimeError):
                    return ApprovalBoundary("features.permission.scope.search_paths")
            return None
        if ev.tool_name.startswith("mcp__"):
            return ApprovalBoundary("features.permission.scope.external")
        if ev.tool_name == "web_fetch":
            return ApprovalBoundary("features.permission.scope.fetch")
        if ev.tool_name == "web_search":
            return ApprovalBoundary("features.permission.scope.search")
        if ev.tool and ev.tool.read_only:
            return None
        return ApprovalBoundary("features.permission.scope.unknown_tool")


    def decide(self, event: ToolCallEvent) -> tuple[str, str]:
        runtime = self._event_runtime(event)
        started = time.monotonic()

        def finish(decision, reason, source, *, audit_reason=None):
            self._record(event, decision, reason if audit_reason is None else audit_reason, source, started)
            return decision, reason

        if self.is_read_only(runtime) and not read_only_tool_allowed(event.tool):
            return finish("deny", self.session.t("features.permission.read_only"), "read-only")
        for rule in self.session.settings.get("permissions.deny", []):
            if rule_matches(rule, event):
                return finish("deny", self.session.t("features.permission.denied_rule", rule=rule), "deny-rule")
        key = self._approval_key(event)
        if key in self.denials:
            return finish("deny", self.denials[key], "previous-denial")
        if key in self.session_approvals:
            return finish("allow", "", "session-approval")
        for rule in self.session.settings.get("permissions.allow", []):
            if rule_matches(rule, event, allow=True):
                return finish("allow", "", "allow-rule", audit_reason=rule)
        boundary = self._boundary(event)
        if boundary is None:
            return finish("allow", "", "workspace")
        if self.approval_for(runtime) == "never":
            return finish("deny", self.session.t("features.permission.never"), "approval-never")
        reason = self.session.t("features.permission.approval_required_manual", reason=boundary.describe(self.session.t))
        return finish("ask", reason, "manual-approval")

    def _record(self, event, decision, reason, source, started):
        arguments = json.dumps(event.args, sort_keys=True, ensure_ascii=False, default=str)
        runtime = self._event_runtime(event)
        origin_store(self.session, event).append("custom", kind="permission_decision", data={
            "tool_call_id": event.call_id, "tool": event.tool_name, "agent": event.agent,
            "cwd": str(event.cwd), "work_mode": self.work_mode_for(runtime),
            "read_only": self.is_read_only(runtime), "approval_policy": self.approval_for(runtime),
            "source": source, "decision": decision, "reason": reason,
            "arguments_sha256": hashlib.sha256(arguments.encode()).hexdigest(),
            "duration": round(time.monotonic() - started, 6),
        })

    def remember(self, event: ToolCallEvent) -> str:
        if event.tool_name == "powershell" or (
            event.tool_name == "bash" and command_path_targets(event.args.get("command", ""), event.cwd) is None
        ):
            self.session.ui.print(self.session.t("features.permission.cannot_remember"), "warn")
            return ""
        self.session_approvals.add(self._approval_key(event))
        rule = describe(event)
        self.session_rules.append(rule)
        return rule

    def revoke(self) -> None:
        with self.session._lock:
            self.session.assert_state_change_allowed()
            self.session_approvals.clear()
            self.session_rules.clear()
            self.denials.clear()
            self.revision += 1

    def _allow(self, event: ToolCallEvent):
        event._permission_snapshot = self._snapshot(event)
        event._permission_boundary = self._boundary(event)
        return None

    def before_execute(self, event: ToolCallEvent):
        if (getattr(event, "_permission_snapshot", None) == self._snapshot(event)
                and getattr(event, "_permission_boundary", None) == self._boundary(event)):
            return None
        return self.on_tool_call(event, self.session)

    def preflight(self, event: ToolCallEvent):
        return self.on_tool_call(event, self.session)

    def on_tool_call(self, event: ToolCallEvent, _session):
        try:
            return self._on_tool_call(event)
        except Exception:  # Extension dispatch must never turn authorization errors into permission.
            return Block(self.session.t("features.permission.check_failed"))

    def _on_tool_call(self, event: ToolCallEvent):
        # A changing UI approval cannot authorize another request or recurse forever.
        for _ in range(3):
            snapshot = self._snapshot(event)
            decision, reason = self.decide(event)
            if snapshot != self._snapshot(event):
                continue
            if decision == "allow":
                return self._allow(event)
            if decision == "deny":
                return Block(reason)
            who = "" if event.agent == "main" else f"[{event.agent}] "
            started = time.monotonic()
            answer = self.session.ui.confirm(
                f"{who}{describe(event)}\n  ({reason})" + self.session.t("features.permission.allow_suffix"), event.agent
            )
            if snapshot != self._snapshot(event):
                self._record(event, "superseded", "Authorization context changed during confirmation.", "human", started)
                continue
            self._record(event, "allow" if answer in {"y", "a"} else "deny", {
                "y": "Allowed once.", "a": "Allowed for this session.", "n": "Denied by user.",
            }.get(answer, "No approver available."), "human", started)
            if answer is None:
                return Block(self.session.t("features.permission.no_approver", reason=reason))
            if answer == "a":
                self.remember(event)
                return self._allow(event)
            if answer == "y":
                return self._allow(event)
            reason = self.session.t("features.permission.user_denied")
            if answer == "n":
                self.denials[self._approval_key(event)] = reason
            return Block(reason)
        return Block(self.session.t("features.permission.check_failed"))


def describe(event: ToolCallEvent) -> str:
    if event.tool_name in SHELL_TOOLS:
        return f"{event.tool_name}: {event.args.get('command', '')}"
    if "path" in event.args:
        return f"{event.tool_name}: {event.args['path']}"
    text = json.dumps(event.args, ensure_ascii=False, default=str)
    return f"{event.tool_name}: {text[:200]}"


def install_permissions(session: AgentSession) -> Permissions:
    """Install mandatory authorization and independent execution boundaries."""
    permissions = Permissions(session)
    session.register_permission_policy(permissions)
    from .sandbox import SandboxManager

    sandbox = SandboxManager(session)
    session.services["sandbox"] = sandbox
    session.add_permission_policy(sandbox, owner="core.sandbox")

    def approvals_command(session, args):
        value = args.strip()
        try:
            if value == "revoke":
                permissions.revoke()
            elif value:
                session.approval_policy = normalize_approval_policy(value)
        except ValueError as error:
            session.ui.print(str(error), "error")
            return
        lines = [session.t("features.permission.status", label=approval_policy_label(session.approval_policy, session.t)),
                 session.t("work_mode.status", mode=session.t("work_mode." + permissions.work_mode))]
        for key in ("allow", "deny"):
            if rules := session.settings.get(f"permissions.{key}", []):
                lines.append(f"{session.t('features.permission.' + key)}: {', '.join(rules)}")
        if permissions.session_rules:
            lines.append(session.t("features.permission.session_rules", rules=", ".join(permissions.session_rules)))
        session.ui.command_result("\n".join(lines))

    def mode_command(session, args):
        value = args.strip()
        try:
            if value:
                permissions.set_work_mode(value)
        except ValueError as error:
            session.ui.print(str(error), "error")
            return
        session.ui.command_result(session.t("work_mode.status", mode=session.t("work_mode." + permissions.work_mode)))
        session.ui.command_result(session.t("work_mode.approval_unchanged"))

    session.commands.register("mode", mode_command, "show or set work mode (default|plan)",
                              argument_hint="[default|plan]", description_key="work_mode.description",
                              source="core", concurrent=True)
    session.commands.register("permissions", approvals_command, "show approval policy, select ask|never or revoke remembered approvals",
                              argument_hint="[ask|never|revoke]", description_key="features.commands.permissions", source="core")
    return permissions
