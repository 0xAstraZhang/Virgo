"""Required, single-owner execution provider binding for an AgentSession."""
from __future__ import annotations

import importlib
import json
from dataclasses import replace
from pathlib import Path

from ..execution import HostContext
from ..tools.fs import EDIT, GLOB, GREP, READ, WRITE, make_file_tool
from .execution_modes import DEFAULT_MODES, HOST_MODE


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
            from .extensions import ExtensionAPI

            extension = importlib.import_module("virgo.extensions.sandbox")
            provider = extension.SandboxExecutionProvider(self.session)
            api = ExtensionAPI(self.session, "sandbox")
            try:
                api.on("tool_result", provider._result)
                api.on("agent_event", provider._event)
                self.resource_tool = extension.install_resource_tool(api, provider)
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
            return HostContext(Path(runtime.accounting_store.cwd).resolve())
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
                if self.sandbox is not None:
                    self.sandbox._discard(runtime)
            previous = getattr(runtime, "execution_context", None)
            runtime.execution_context = context
            if previous is not None and previous.provider != context.provider and hasattr(runtime, "agent"):
                runtime.rebuild_system()

    def for_context(self, event):
        base = super().for_context(event)
        return base if base.provider == "host" else self.sandbox.for_context(event)

    def preflight(self, event):
        if super().for_context(event).provider != "host":
            return self.sandbox.preflight(event)

    def before_execute(self, event):
        if super().for_context(event).provider != "host":
            return self.sandbox.before_execute(event)

    def instructions(self, runtime):
        if runtime.execution_context.provider == "host":
            return ("<execution_boundary>No-Limit: Host tools run with the current account's rights. "
                    "Task instructions express user intent.</execution_boundary>")
        return self.sandbox.instructions(runtime)

    def revoke(self):
        if self.sandbox is not None:
            self.sandbox.revoke()

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
            self.session.notify_state_change("permission_mode_changed", runtime, self.selected_mode)

    def status(self, session, args):
        with session._lock:
            context = session.main.execution_context
            current = self.mode_for(session.main)
            facts = {**context.describe(), "current_mode": current.name, "next_mode": self.selected_mode.name,
                     "web_allowed": current.web_allowed, "shell_network": current.flags()["network_allowed"]}
            session.ui.command_result(f"{current.name} ({context.provider.title()})\n" + json.dumps(facts, ensure_ascii=False))

    def command(self, session, args):
        value = args.strip()
        if value in {"", "show", "status"}:
            self.status(session, args)
        elif value == "list":
            session.ui.command_result("\n".join(f"{m.id}: {m.name}" for m in self.modes.values()))
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
    if provider.selected_mode != HOST_MODE:
        provider.load_sandbox()
    for tool in (READ, GLOB, GREP, WRITE, EDIT):
        session.replace_tools(f"tool:{tool.name}", [(make_file_tool(tool, provider.for_context), ("main", "subagent"))],
                              owner="core")
    session.commands.register("permissions", provider.command, "show execution boundary and resource permissions",
                              source="core", concurrent=True, argument_hint="[show|list|read-only|workspace-edit|no-limit|grants|revoke ID]")
