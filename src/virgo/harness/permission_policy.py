"""Permissions: decide allow / ask / deny for every tool call.

Modes: ``auto-review`` (workspace changes pass, external/network actions require review or approval),
``accept-all`` (no permission checks), and ``plan`` (read-only investigation and planning).
Rules look like ``bash(npm test*)``, ``edit(src/**)``, ``read(~/.ssh/*)``, ``mcp__github__*``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent.types import Block, ToolCallEvent
from ..tools.fs import IGNORE_DIRS, glob_match
from ..tools.shell import SHELL_TOOLS
from .permission_context import (
    review_context_key,
    review_store,
    review_workspace,
    source_for,
)
from .permission_scope import (
    ReviewBoundary,
    command_boundary,
    command_path_targets,
    inside,
    is_read_only_command,
)
from .permissions import MODES as MODES
from .permissions import normalize_permission_mode as normalize_permission_mode
from .permissions import permission_mode_label as permission_mode_label
from .permissions import plan_tool_allowed as plan_tool_allowed

if TYPE_CHECKING:
    from .session import AgentSession

@dataclass(frozen=True)
class ReviewDecision:
    decision: str
    reason: str
    risk: str = "unknown"
    authorization: str = "unknown"
    evidence_ids: tuple[str, ...] = ()


class ReviewFailure(RuntimeError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


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


def rule_matches(rule: str, ev: ToolCallEvent) -> bool:
    tool, pattern = parse_rule(rule)
    if not fnmatch.fnmatchcase(ev.tool_name, tool):
        return False
    if pattern is None:
        return True
    pats = [pattern, os.path.expanduser(pattern)]
    return any(fnmatch.fnmatchcase(s, p) for s in _subjects(ev) for p in pats)


class Permissions:
    def __init__(self, session: AgentSession):
        self.session = session
        self.reviewer = None
        self.session_rules: list[str] = []
        self.session_approvals: set[str] = set()
        self.review_denials: dict[str, str] = {}
        self.agent_modes: dict[str, str] = {}  # child overrides may only restrict the session policy
        self.agent_parents: dict[str, str] = {}

    def mode_for(self, agent: str) -> str:
        mode = normalize_permission_mode(self.session.permission_mode)
        severity = {"accept-all": 0, "auto-review": 1, "plan": 2}
        seen = set()
        while agent not in seen:
            seen.add(agent)
            own = normalize_permission_mode(self.agent_modes.get(agent, mode))
            if severity[own] > severity[mode]:
                mode = own
            parent = self.agent_parents.get(agent)
            if parent is None:
                break
            agent = parent
        return mode

    def set_agent_parent(self, agent: str, parent: str | None) -> None:
        """Bind the dynamic permission ceiling to the originating parent."""
        if parent is None:
            self.agent_parents.pop(agent, None)
            return
        cursor, seen = parent, {agent}
        while cursor is not None:
            if cursor in seen:
                raise ValueError("Runtime permission parent relationships must be acyclic")
            seen.add(cursor)
            cursor = self.agent_parents.get(cursor)
        self.agent_parents[agent] = parent

    def set_reviewer(self, reviewer) -> None:
        """Install optional automatic review without replacing Core enforcement."""
        if reviewer is not None and not callable(reviewer):
            raise TypeError("Permission reviewer must be callable")
        self.reviewer = reviewer

    def set_agent_mode(self, agent: str, mode: str | None) -> None:
        if mode is None:
            self.agent_modes.pop(agent, None)
        else:
            self.agent_modes[agent] = normalize_permission_mode(mode)

    def _approval_key(self, ev: ToolCallEvent) -> str:
        path = ev.args.get("path")
        target = str((ev.cwd / os.path.expanduser(path)).resolve()) if isinstance(path, str) else None
        if ev.tool_name == "bash":
            target = command_path_targets(ev.args.get("command", ""), ev.cwd)
        shell = getattr(ev.tool, "shell", None)
        return json.dumps(
            [
                review_context_key(self.session, ev),
                ev.tool_name,
                str(ev.cwd.resolve()),
                ev.args,
                target,
                shell.environment() if shell else None,
            ],
            sort_keys=True,
            ensure_ascii=False,
        )

    def _snapshot(self, ev: ToolCallEvent) -> tuple:
        rules = self.session.settings.get("permissions", {})
        return (
            self.mode_for(ev.agent),
            self._approval_key(ev),
            json.dumps(rules, sort_keys=True),
            self._requires_denial_review(ev),
        )

    def _requires_denial_review(self, ev: ToolCallEvent) -> bool:
        # A changed command spelling must not turn a denied effect into a static
        # workspace allow. Keep read-only investigation available after a denial.
        if ev.tool_name == "bash":
            if is_read_only_command(ev.args.get("command", "")):
                return False
        elif plan_tool_allowed(ev.tool):
            return False
        source = source_for(self.session, ev)
        if source is None:
            return False
        records = source.records()
        last_user = max((i for i, record in enumerate(records) if record["role"] == "user"), default=-1)
        return any(
            record["role"] == "approval"
            and record.get("decision") == "deny"
            and (
                record.get("source") in {"auto-review", "previous-denial"}
                or (record.get("source") == "human" and record.get("reason") == "Denied by user.")
            )
            for record in records[last_user + 1 :]
        )

    def _boundary(self, ev: ToolCallEvent) -> ReviewBoundary | None:
        workspace = review_workspace(self.session, ev)
        if ev.tool_name == "powershell":
            return ReviewBoundary("features.permission.scope.powershell")
        if ev.tool_name == "bash":
            return command_boundary(ev.args.get("command", ""), ev.cwd, workspace)
        if ev.tool_name in {"read", "write", "edit", "glob", "grep"}:
            path = ev.args.get("path", ".")
            if not isinstance(path, str) or not inside(path, ev.cwd, workspace):
                target = (ev.cwd / os.path.expanduser(path)).resolve() if isinstance(path, str) else path
                return ReviewBoundary("features.permission.scope.action_path", {"path": target})
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
                                return ReviewBoundary(
                                    "features.permission.scope.search_link",
                                    {"path": candidate, "target": candidate.resolve()},
                                )
                except (OSError, RuntimeError):
                    return ReviewBoundary("features.permission.scope.search_paths")
            return None
        if ev.tool_name.startswith("mcp__"):
            return ReviewBoundary("features.permission.scope.external")
        if ev.tool_name == "web_fetch":
            return ReviewBoundary("features.permission.scope.fetch")
        if ev.tool_name == "web_search":
            return ReviewBoundary("features.permission.scope.search")
        if ev.tool and ev.tool.read_only:
            return None
        return ReviewBoundary("features.permission.scope.unknown_tool")

    def decide(self, ev: ToolCallEvent) -> tuple[str, str]:
        s = self.session.settings
        mode = self.mode_for(ev.agent)
        started = time.monotonic()
        snapshot = self._snapshot(ev)
        ev._review_audit = {}

        def finish(decision, reason, source, *, audit_reason=None):
            self._record(
                ev, mode, decision, audit_reason if audit_reason is not None else reason, source, started
            )
            return decision, reason

        if mode == "accept-all":
            return finish("allow", "", "accept-all")
        for rule in s.get("permissions.deny", []) or []:
            if rule_matches(rule, ev):
                return finish(
                    "deny", self.session.t("features.permission.denied_rule", rule=rule), "deny-rule"
                )
        if mode == "plan":
            read_only = plan_tool_allowed(ev.tool)
            if ev.tool_name == "bash":
                extra = set(s.get("permissions.readOnlyCommands", []) or [])
                read_only = is_read_only_command(ev.args.get("command", ""), extra)
            if not read_only:
                return finish("deny", self.session.t("features.permission.plan_readonly"), "plan")
            return finish("allow", "", "plan")
        action_key = self._approval_key(ev)
        if action_key in self.review_denials:
            return finish("deny", self.review_denials[action_key], "previous-denial")
        denial_review = self._requires_denial_review(ev)
        if not denial_review and action_key in self.session_approvals:
            return finish("allow", "", "session-approval")
        for rule in s.get("permissions.allow", []) or []:
            if not denial_review and rule_matches(rule, ev):
                return finish("allow", "", "allow-rule", audit_reason=rule)
        boundary = self._boundary(ev)
        if boundary is None and not denial_review:
            return finish("allow", "", "workspace")
        boundary_reason = boundary.describe(self.session.t) if boundary is not None else ""
        if denial_review:
            boundary_reason = (
                ReviewBoundary("features.permission.scope.previous_denial").describe(self.session.t)
                + boundary_reason
            )
        if self.reviewer is None:
            return finish("ask", self.session.t("features.permission.approval_required_manual", reason=boundary_reason), "manual-review")
        source = "auto-review"
        try:
            verdict = self.reviewer(ev, boundary_reason)
            decision, reason = verdict.decision, verdict.reason
        except Exception as error:  # noqa: BLE001 - failed reviews must go to a person, never fail open
            origin = source_for(self.session, ev)
            signal = getattr(ev, "signal", None)
            if signal is None and origin is not None:
                signal = origin.runtime.agent.signal
            if signal is not None and signal.is_set():
                return finish("deny", self.session.t("features.permission.interrupted"), "interrupted")
            failure = (
                error.kind
                if isinstance(error, ReviewFailure)
                else ("timeout" if isinstance(error, TimeoutError) else "request-error")
            )
            ev._review_audit["failure_kind"] = failure
            decision = "ask"
            reason = self.session.t(
                "features.permission.review_failed", failure=failure, reason=boundary_reason
            )
            source = "review-error"
        if snapshot != self._snapshot(ev):
            finish("superseded", reason, source)
            return self.decide(ev)
        if decision == "allow":
            return finish("allow", "", source, audit_reason=reason)
        if decision == "deny":
            reason = self.session.t("features.permission.denied", reason=reason)
            self.review_denials[self._approval_key(ev)] = reason
            return finish("deny", reason, source)
        return finish("ask", self.session.t("features.permission.approval_required", reason=reason), source)

    def _record(self, ev, mode, decision, reason, source, started):
        # Link to the existing tool call without duplicating command bodies or file contents.
        arguments = json.dumps(ev.args, sort_keys=True, ensure_ascii=False, default=str)
        store = review_store(self.session, ev)
        store.append(
            "custom",
            kind="permission_decision",
            data={
                "tool_call_id": ev.call_id,
                "tool": ev.tool_name,
                "agent": ev.agent,
                "cwd": str(ev.cwd),
                "mode": mode,
                "source": source,
                "decision": decision,
                "reason": reason,
                "arguments_sha256": hashlib.sha256(arguments.encode()).hexdigest(),
                "model": getattr(ev, "_review_model", self.session.small_model.spec)
                if source in {"auto-review", "review-error", "interrupted"}
                else None,
                "duration": round(time.monotonic() - started, 6),
                "review_context": getattr(ev, "_review_audit", {}),
            },
        )

    def remember(self, ev: ToolCallEvent) -> str:
        if ev.tool_name == "powershell" or (
            ev.tool_name == "bash" and command_path_targets(ev.args.get("command", ""), ev.cwd) is None
        ):
            self.session.ui.print(self.session.t("features.permission.cannot_remember"), "warn")
            return ""
        self.session_approvals.add(self._approval_key(ev))
        rule = describe(ev)
        self.session_rules.append(rule)
        return rule

    def _allow(self, ev: ToolCallEvent):
        ev._permission_snapshot = self._snapshot(ev)
        ev._permission_boundary = (
            self._boundary(ev) if self.mode_for(ev.agent) == "auto-review" else None
        )
        return None

    def before_execute(self, ev: ToolCallEvent):
        snapshot = self._snapshot(ev)
        unchanged = getattr(ev, "_permission_snapshot", None) == snapshot
        if unchanged and snapshot[0] == "auto-review":
            unchanged = getattr(ev, "_permission_boundary", None) == self._boundary(ev)
        if unchanged:
            return None
        return self.on_tool_call(ev, self.session)

    def preflight(self, ev: ToolCallEvent):
        return self.on_tool_call(ev, self.session)

    def on_tool_call(self, ev: ToolCallEvent, _session):
        try:
            return self._on_tool_call(ev)
        except Exception:  # noqa: BLE001 - EventBus swallows exceptions; permission hooks must fail closed.
            return Block(self.session.t("features.permission.check_failed"))

    def _on_tool_call(self, ev: ToolCallEvent):
        mode = self.mode_for(ev.agent)
        snapshot = self._snapshot(ev)
        decision, reason = self.decide(ev)
        if snapshot != self._snapshot(ev):
            return self.on_tool_call(ev, self.session)
        if decision == "allow":
            return self._allow(ev)
        if decision == "deny":
            return Block(reason)
        summary = describe(ev)
        who = "" if ev.agent == "main" else f"[{ev.agent}] "
        started = time.monotonic()
        answer = self.session.ui.confirm(
            f"{who}{summary}\n  ({reason})" + self.session.t("features.permission.allow_suffix"), ev.agent
        )
        if snapshot != self._snapshot(ev):
            self._record(
                ev, mode, "superseded", "Permission context changed during confirmation.", "human", started
            )
            return self.on_tool_call(ev, self.session)
        self._record(
            ev,
            mode,
            "allow" if answer in {"y", "a"} else "deny",
            {"y": "Allowed once.", "a": "Allowed for this session.", "n": "Denied by user."}.get(
                answer, "No approver available."
            ),
            "human",
            started,
        )
        if answer is None:
            return Block(self.session.t("features.permission.no_approver", reason=reason))
        if answer == "a":
            self.remember(ev)
            return self._allow(ev)
        if answer == "y":
            return self._allow(ev)
        reason = self.session.t("features.permission.user_denied")
        if answer == "n":
            self.review_denials[self._approval_key(ev)] = reason
        return Block(reason)


def describe(ev: ToolCallEvent) -> str:
    a = ev.args
    if ev.tool_name in SHELL_TOOLS:
        return f"{ev.tool_name}: {a.get('command', '')}"
    if "path" in a:
        return f"{ev.tool_name}: {a['path']}"
    text = json.dumps(a, ensure_ascii=False, default=str)
    return f"{ev.tool_name}: {text[:200]}"


def install_permissions(session: AgentSession) -> Permissions:
    """Construct the mandatory policy before loading any capability plugin."""
    perms = Permissions(session)
    session.register_permission_policy(perms)

    def cmd(session, args):
        a = args.strip()
        if a:
            try:
                session.permission_mode = normalize_permission_mode(a)
            except ValueError:
                session.ui.print(session.t("features.permission.invalid_mode", modes=", ".join(MODES)), "error")
                return
        s = session.settings
        label = permission_mode_label(session.permission_mode, session.t)
        lines = [session.t("features.permission.status", label=label)]
        for key in ("allow", "deny"):
            rules = s.get(f"permissions.{key}", []) or []
            if rules:
                lines.append(f"{session.t('features.permission.' + key)}: {', '.join(rules)}")
        if perms.session_rules:
            lines.append(session.t("features.permission.session_rules", rules=", ".join(perms.session_rules)))
        session.ui.print("\n".join(lines))

    session.commands.register(
        "permissions",
        cmd,
        "show or set permissions (auto-review|accept-all|plan)",
        argument_hint="[auto-review|accept-all|plan]",
        description_key="features.commands.permissions",
        source="core",
    )
    return perms
