"""Required, single-owner execution provider binding for an AgentSession."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from ..agent import Block
from ..execution import HostContext, SandboxExecutor
from ..execution_policy import DEFAULT_MODES, HOST_MODE
from ..tools.fs import EDIT, GLOB, GREP, READ, WRITE, make_file_tool
from ..tools.output import make_read_output_tool
from .high_risk import detect_high_risk_command
from .ui import UI

_BACKEND_UNAVAILABLE_REASONS = frozenset({
    "backend_unavailable",
    "windows_network_isolation_unavailable",
    "windows_null_device_unavailable",
    "windows_runtime_inaccessible",
    "windows_unsupported_os",
})


class HostExecutionProvider:
    name = "host"

    def __init__(self, session):
        self.session = session

    def for_context(self, context):
        runtime = self.session.runtime_for_context(context)
        if runtime is None:
            raise ValueError("Execution requires the originating runtime context")
        return runtime.execution_context


class ModeExecutionProvider(HostExecutionProvider):
    """Dispatch each tool using its originating task's immutable execution context."""

    modes = DEFAULT_MODES

    def __init__(self, session):
        super().__init__(session)
        self.startup_mode = session.settings.get("execution.mode")
        self.executor = SandboxExecutor()
        self.sandbox = None
        self.resource_tool = None

    @property
    def selected_mode_id(self):
        return getattr(getattr(self.session, "main", None), "permission_mode_id", self.startup_mode)

    @property
    def selected_mode(self):
        return self.modes[self.selected_mode_id]

    @property
    def name(self):
        return "host" if self.selected_mode_id == HOST_MODE.id else "sandbox"

    @property
    def revision(self):
        return self.sandbox.revision if self.sandbox is not None else 0

    def load_sandbox(self):
        if self.sandbox is None:
            from ..sandbox import SandboxExecutionProvider, install_resource_tool
            from .extensions import ExtensionAPI

            provider = SandboxExecutionProvider(self.session, self.executor)
            api = ExtensionAPI(self.session, "core:execution")
            try:
                self.resource_tool = install_resource_tool(api, provider)
            except BaseException:
                api._rollback()
                raise
            self.sandbox = provider
        return self.sandbox

    def mode_for(self, runtime):
        return runtime.execution_context.resources if runtime.permission_snapshot else HOST_MODE

    def effective_mode(self, runtime):
        return self.mode_for(runtime).name

    def _context(self, runtime, mode, revision):
        if mode == HOST_MODE:
            return HostContext(
                Path(runtime.accounting_store.cwd).resolve(),
                executor=self.executor,
                outputs=self.session.output_store_for(runtime.accounting_store),
            )
        return self.load_sandbox().context_for_mode(runtime, mode, revision)

    def capture_runtime(self, runtime):
        with self.session._lock:
            if runtime.parent is not None:
                context = getattr(runtime, "execution_context", None) or runtime.parent.execution_context
                snapshot = getattr(context, "permission_snapshot", None)
                if snapshot and runtime.approval_policy == "never" and snapshot.approval != "never":
                    context = replace(context, permission_snapshot=replace(snapshot, approval="never"))
            else:
                mode = self.modes[getattr(runtime, "permission_mode_id", self.startup_mode)]
                origin = getattr(runtime, "origin", None)
                context = self._context(runtime, mode, origin.root.preset_revision if origin else 0)
                runtime.permission_mode_id = mode.id
            previous = getattr(runtime, "execution_context", None)
            runtime.execution_context = context
            if previous is not None and previous.provider != context.provider and hasattr(runtime, "agent"):
                runtime.rebuild_system()

    def for_context(self, event):
        base = super().for_context(event)
        return base if base.provider == "host" else self.sandbox.for_context(event)

    def _confirm_callable(self):
        ui = self.session.ui
        once = getattr(ui, "confirm_once", None)
        if callable(once) and getattr(once, "__func__", None) is not UI.confirm_once:
            return once
        confirm = getattr(ui, "confirm", None)
        if callable(confirm) and getattr(confirm, "__func__", None) is not UI.confirm:
            return confirm
        return None

    def _approval_allowed(self, base, signal) -> bool:
        snapshot = getattr(base, "permission_snapshot", None)
        return (
            self.session.approval_policy != "never"
            and (snapshot is None or snapshot.approval != "never")
            and (signal is None or not signal.is_set())
            and self._confirm_callable() is not None
        )

    def _check_high_risk(self, event, base, *, confirm: bool):
        """Enforce high-risk shell command confirmation across all modes (S5-01)."""
        if event.tool is None or event.tool.execution_scope != "process":
            return None
        # Disable knob is honored ONLY in No-Limit mode.
        if base.provider == "host" and self.session.settings.get("permissions.confirmHighRisk", True) is False:
            return None
        command = event.args.get("command", "") if isinstance(event.args, dict) else ""
        match = detect_high_risk_command(command, event.cwd, base.workspace)
        if match is None:
            return None

        def deny(reason: str):
            return Block(reason, details={
                "status": "high_risk_denied",
                "category": match.category,
                "boundary": {
                    **base.describe(),
                    "kind": "high_risk_denied",
                    "category": match.category,
                    "payload_started": False,
                    "side_effects": "none",
                    "escalation_available": False,
                },
            })

        if not self._approval_allowed(base, event.signal):
            return deny(f"High-risk command blocked ({match.reason}). Interactive confirmation is required.")
        if not confirm:
            return None
        question = (
            f"High-risk command detected ({match.category}): {match.reason}\n"
            f"Command: {command}\n"
            f"Working directory: {Path(event.cwd).resolve()}\n"
            "Approve executing this command once? [y/n]"
        )
        answer = self._confirm_callable()(question, event.agent)
        if answer != "y":
            return deny(f"High-risk command was not approved: {match.reason}")
        return None

    @staticmethod
    def _is_backend_unavailable(error: Exception) -> bool:
        reason = getattr(error, "reason", None)
        if reason in _BACKEND_UNAVAILABLE_REASONS:
            return True
        if reason is not None:
            return False
        text = str(error).lower()
        return any(token in text for token in ("backend", "sandbox-exec", "bubblewrap", "libseccomp"))

    def confirm_no_limit_once(self, ctx, args, error):
        """Offer an explicit one-shot No-Limit (Host) fallback when the sandbox backend is unavailable (S5-02)."""
        if not self._is_backend_unavailable(error):
            return None
        runtime = self.session.runtime_for_context(ctx)
        if runtime is None:
            return None
        base = runtime.execution_context
        if base.provider == "host" or not self._approval_allowed(base, getattr(ctx, "signal", None)):
            return None
        command = args.get("command", "") if isinstance(args, dict) else ""
        mode_name = self.mode_for(runtime).name
        question = (
            f"Native sandbox is unavailable: {error}\n"
            f"Command: {command}\n"
            f"Working directory: {Path(ctx.cwd).resolve()}\n"
            f"Run this single command with No-Limit (Host) authority? "
            f"(Session remains in {mode_name}; switch permanently with /permissions no-limit.) [y/n]"
        )
        if self._confirm_callable()(question, ctx.agent) != "y":
            return None
        return HostContext(
            base.workspace,
            executor=self.executor,
            outputs=self.session.output_store_for(runtime.accounting_store),
        )

    def preflight(self, event):
        base = super().for_context(event)
        if base.provider != "host":
            block = self.sandbox.preflight(event)
            if block is not None:
                return block
        return self._check_high_risk(event, base, confirm=False)

    def before_execute(self, event):
        base = super().for_context(event)
        block = self._check_high_risk(event, base, confirm=True)
        if block is not None:
            return block
        if base.provider != "host":
            return self.sandbox.before_execute(event)
        return None

    def instructions(self, runtime):
        if runtime.execution_context.provider == "host":
            return ("<execution_boundary>No-Limit: Host tools run with the current account's rights. "
                    "Task instructions express user intent.</execution_boundary>")
        return self.sandbox.instructions(runtime)

    def revoke(self):
        if self.sandbox is not None:
            self.sandbox.revoke()

    def close(self):
        self.executor.close()

    def select_mode(self, identifier):
        with self.session._lock:
            self.session.assert_state_change_allowed()
            if identifier not in self.modes:
                raise ValueError("Choose read-only, workspace-edit or no-limit; use /permissions list")
            runtime = self.session.main
            if identifier == self.selected_mode_id:
                return
            revision = runtime.origin.root.preset_revision + 1
            context = self._context(runtime, self.modes[identifier], revision)
            runtime.permission_mode_id = identifier
            runtime.origin.root.preset_revision = revision
            if not runtime.execution_active and not self.session.runtime_is_busy(runtime):
                runtime.execution_context = context
                runtime.rebuild_system()
                if identifier == HOST_MODE.id:
                    try:
                        self.executor.close_idle()
                    except Exception as error:
                        self.session.report_error(error, phase="execution.cleanup", notify=False)
            self.session.notify_state_change("permission_mode_changed", runtime, self.selected_mode)

    def status(self, session, args):
        with session._lock:
            context = session.main.execution_context
            current = self.mode_for(session.main)
            facts = {**context.describe(), "current_mode": current.name, "next_mode": self.selected_mode.name,
                     "web_allowed": current.web_allowed, "shell_network": current.flags()["network_allowed"]}
            label = f"{current.name} ({context.provider.title()})"
            session.ui.command_result(f"{label}\n" + json.dumps(facts, ensure_ascii=False))

    def command(self, session, args):
        value = args.strip()
        if value in {"", "show", "status"}:
            self.status(session, args)
        elif value == "list":
            rows = [f"{mode.id}: {mode.name}" for mode in self.modes.values()]
            session.ui.command_result("\n".join(rows))
        elif value == "grants" or value.startswith("revoke "):
            from .resource_approvals import manage_grants
            manage_grants(session, value)
        elif value == "revoke":
            self.revoke()
        elif value in {"ask", "never"}:
            session.approval_policy = value
            self.revoke()
        else:
            self.select_mode(value)


def install_execution(session):
    provider = ModeExecutionProvider(session)
    session.register_execution_provider(provider, owner="core")
    # Registered before JobManager so LIFO shutdown drains background jobs first,
    # then tears down session-scoped Windows AppContainer profiles and ACLs.
    session.register_cleanup("core.execution", provider.close)
    if provider.selected_mode != HOST_MODE:
        provider.load_sandbox()
    for tool in (READ, GLOB, GREP, WRITE, EDIT):
        session.replace_tools(f"tool:{tool.name}", [(make_file_tool(tool, provider.for_context), ("main", "subagent"))],
                              owner="core")
    session.replace_tools("tool:read_output", [(make_read_output_tool(provider.for_context), ("main", "subagent"))],
                          owner="core")
    session.commands.register("permissions", provider.command, "show execution boundary and resource permissions",
                              source="core", concurrent=True, argument_hint="[show|list|read-only|workspace-edit|no-limit|grants|revoke ID]")
