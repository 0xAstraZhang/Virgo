"""Core-owned sandbox configuration, caller binding and file-tool ceiling."""
from __future__ import annotations

import os
from pathlib import Path

from ..agent import Block
from ..tools.sandbox import (
    PROTECTED_NAMES,
    SandboxPolicy,
    SandboxUnavailable,
    platform_backend,
    validate_config,
)


class SandboxManager:
    def __init__(self, session):
        self.session = session
        config = session.settings.get("sandbox", {})
        validate_config(config)
        self.mode = config.get("mode", "workspace-write")
        if self.mode != "disabled":
            from ..tools.fs import EDIT, WRITE, make_file_tool
            from .extensions import ROLES_ALL

            for tool in (WRITE, EDIT):
                session.replace_tools(f"tool:{tool.name}", [(make_file_tool(tool, self.for_context), ROLES_ALL)],
                                      owner="core")
            scope = ("Windows uses LPAC object ACLs with an independent write restriction. "
                     "Reads include the workspace, runtimes and Windows' ambient LPAC grants. "
                     if os.name == "nt" else
                     "Only the workspace, system runtime paths and private scratch are exposed. ")
            session.sections.append((90, "core.sandbox", lambda _role: (
                f"<sandbox>Core shell processes have a configured {self.mode} native sandbox ceiling with no network. "
                + scope +
                "Read-only task capabilities can only narrow this policy. Sandbox startup failure does not "
                "authorize a native retry. Core file mutations have separate application-level checks. "
                "The broker, Python extensions, hooks and remote tools are not OS-sandboxed.</sandbox>"
            )))
        session.commands.register("sandbox", self.status, "show native command sandbox status",
                                  source="core", concurrent=True, description_key="sandbox.description")

    def status(self, session, args):
        backend = session.t("sandbox.host_execution")
        if self.mode != "disabled":
            try:
                backend = session.t("sandbox.backend_unverified", backend=platform_backend()[0])
            except SandboxUnavailable as error:
                backend = session.t("sandbox.backend_unavailable", reason=str(error))
        session.ui.print(session.t("sandbox.status", mode=self.mode,
                                   effective=self.effective_mode(session.main), backend=backend))
        session.ui.print(session.t("sandbox.scope"))
        if os.name == "nt" and self.mode != "disabled":
            session.ui.print(session.t("sandbox.windows_scope"))

    def effective_mode(self, runtime) -> str:
        if self.mode == "disabled":
            return self.mode
        return "read-only" if self.session.services["permissions"].is_read_only(runtime) else self.mode

    def for_context(self, context) -> SandboxPolicy | None:
        if self.mode == "disabled":
            return None
        runtime = self.session.runtime_for_context(context)
        if runtime is None:
            raise SandboxUnavailable("Sandbox execution requires the originating runtime context")
        workspace = Path(runtime.origin.workspace)
        if os.name != "nt":
            workspace = workspace.resolve()
        if self.session.paths.user.resolve().is_relative_to(workspace):
            raise SandboxUnavailable("Sandbox workspace must not contain Virgo's user credentials or session store")
        return SandboxPolicy(self.effective_mode(runtime), workspace)

    def _check(self, event):
        if self.mode == "disabled" or event.tool_name not in {"write", "edit"}:
            return None
        policy = self.for_context(event)
        path = event.args.get("path")
        if not isinstance(path, str):
            return Block("Sandbox file operations require a path")
        target = (event.cwd / Path(path).expanduser()).resolve()
        protected_roots = (*(policy.workspace / name for name in PROTECTED_NAMES),
                           *policy.runtime_paths())
        protected = any(target.is_relative_to(root.resolve()) for root in protected_roots)
        if policy.mode == "read-only" or not target.is_relative_to(policy.workspace) or protected:
            return Block(self.session.t("sandbox.file_denied", path=str(target)),
                         details={"sandbox": policy.describe(), "status": "sandbox_denied"})
        return None

    preflight = _check
    before_execute = _check
