"""Required, single-owner execution provider binding for an AgentSession."""
from __future__ import annotations

import importlib
from pathlib import Path

from ..execution import HostContext
from ..tools.fs import EDIT, GLOB, GREP, READ, WRITE, make_file_tool
from .execution_modes import HOST_MODE


class HostExecutionProvider:
    name = "host"
    selected_mode = HOST_MODE
    selected_mode_id = "host"
    revision = 0
    modes = {}

    def __init__(self, session):
        self.session = session

    def capture_runtime(self, runtime):
        parent = runtime.parent
        if parent is not None:
            runtime.execution_context = getattr(runtime, "execution_context", None) or parent.execution_context
        else:
            workspace = Path(runtime.accounting_store.cwd).resolve()
            runtime.execution_context = HostContext(workspace)

    def for_context(self, context):
        runtime = self.session.runtime_for_context(context)
        if runtime is None:
            raise ValueError("Execution requires the originating runtime context")
        return runtime.execution_context

    def mode_for(self, runtime):
        return HOST_MODE

    def effective_mode(self, runtime):
        return "Host"

    def preflight(self, event):
        self.for_context(event)

    before_execute = preflight

    def instructions(self, runtime):
        return ("<execution_boundary>Host execution: tools run with the host account's operating-system rights. "
                "No Sandbox is loaded. Task instructions express user intent, not technical isolation."
                "</execution_boundary>")

    def status(self, session, args):
        session.ui.command_result("Host execution — host operating-system authority; Sandbox is not loaded.")

    def command(self, session, args):
        if args.strip() not in {"", "show", "status"}:
            raise ValueError("Host has no resource approval controls. Start with --execution sandbox to use them.")
        self.status(session, args)


def install_execution(session):
    selected = session.settings.get("execution.provider", "host")
    if selected == "host":
        session.register_execution_provider(HostExecutionProvider(session), owner="core.host")
    else:
        # Required setup deliberately does not use the optional extension loader.
        # No runtime, project context or task-capable plugin exists until it succeeds.
        from .extensions import ExtensionAPI

        api = ExtensionAPI(session, "sandbox")
        try:
            importlib.import_module("virgo.extensions.sandbox").setup(api)
        except BaseException:
            api._rollback()
            raise
        if "execution" not in session.services or session.services["execution"].name != selected:
            raise RuntimeError("Required execution provider was not registered")
    provider = session.services["execution"]
    for tool in (READ, GLOB, GREP, WRITE, EDIT):
        session.replace_tools(f"tool:{tool.name}", [(make_file_tool(tool, provider.for_context), ("main", "subagent"))],
                              owner="core")
    session.commands.register("permissions", provider.command, "show execution boundary and resource permissions",
                              source="core", concurrent=True, argument_hint="[show]")
