"""Per-session shell selection, shared by fullscreen and plain slash commands."""
from __future__ import annotations

import os

from ..tools.shell import Shell, ShellTool, default_shell, discover_shells, make_shell_tool, probe_shell
from .extensions import ROLES_ALL

USAGE = 'Usage: /shell [name|absolute path|auto] (quote paths containing spaces)'


class ShellSelection:
    def __init__(self, session, catalog: tuple[Shell, ...] | None = None):
        self.session = session
        # Share only the startup snapshot; each session owns its selection and additions.
        self.catalog = tuple(discover_shells()) if catalog is None else catalog
        self.available = list(self.catalog)
        self.current: Shell | None = None
        choice = os.environ.get("VIRGO_SHELL") or session.settings.get("shell.executable", "auto")
        try:
            self.current = default_shell(self.available, t=session.t) if choice == "auto" else probe_shell(choice, t=session.t)
            if choice == "auto" and catalog is not None:
                self.current = probe_shell(self.current.executable, t=session.t)
            self.remember(self.current)
        except (ValueError, TypeError) as error:
            self.current = None
            session.ui.print(session.t("harness.shell.unavailable", error=error), "warn")

    def remember(self, shell: Shell) -> None:
        for index, item in enumerate(self.available):
            if item.executable == shell.executable:
                self.available[index] = shell
                return
        self.available.append(shell)

    def install_tools(self) -> None:
        s = self.session
        s._tools = [(tool, roles) for tool, roles in s._tools if not isinstance(tool, ShellTool)]
        if self.current is None:
            return
        s.register_tool(make_shell_tool(self.current), ROLES_ALL)
        if jobs := s.services.get("jobs"):
            s.register_tool(make_shell_tool(self.current, jobs), ("main",))

    def select(self, shell: Shell, *, automatic=False) -> None:
        s = self.session
        with s._lock:
            if s.busy:
                raise ValueError(s.t("harness.shell.busy"))
            # Save first so a settings-write failure leaves the live binding and prompt intact.
            s.settings.save_user({"shell": {"executable": "auto" if automatic else shell.executable}})
            self.current = shell
            self.remember(shell)
            self.install_tools()
            s.main.tools = s.tools_for("main")
            s.main.agent.cfg.tools = s.main.tools
            s.main.rebuild_system()
            try:
                s.store.append("custom", kind="shell_change", data={
                    "shell": shell.kind, "executable": shell.executable, "version": shell.version,
                })
            except OSError as error:
                s.ui.print(s.t("harness.shell.record_failed", error=error), "warn")


def configure_shell(session, args: str) -> None:
    service = session.services["shell"]
    if session.busy:
        session.ui.print(session.t("harness.shell.busy"), "error")
        return
    choice = args.strip()
    if len(choice) >= 2 and choice[0] == choice[-1] and choice[0] in {"'", '"'}:
        choice = choice[1:-1]
    try:
        automatic = choice == "auto"
        if not choice:
            current = service.current
            options = list(service.available)
            # Keep the selected interpreter first so Enter preserves it.
            options.sort(key=lambda shell: shell != current)
            labels = [("✓ " if shell == current else "") + shell.label for shell in options]
            if not session.ui.interactive:
                session.ui.print(session.t("harness.shell.current", shell=current.label if current else session.t("common.unavailable")))
                session.ui.print(session.t("harness.shell.available", shells="\n".join(labels), usage=session.t("harness.shell.usage")))
                return
            if not options:
                session.ui.print(session.t("harness.shell.none"), "error")
                return
            label = session.ui.select(session.t("harness.shell.picker"), labels)
            if label is None:
                session.ui.print(session.t("harness.shell.kept"), "dim")
                return
            shell = options[labels.index(label)]
            # It may have disappeared while the picker was open.
            shell = probe_shell(shell.executable, t=session.t)
        else:
            shell = probe_shell(default_shell(service.available, t=session.t).executable, t=session.t) if automatic else probe_shell(choice, t=session.t)
        service.select(shell, automatic=automatic)
    except (ValueError, OSError) as error:
        session.ui.print(session.t("harness.shell.failed", error=error, usage=session.t("harness.shell.usage")), "error")
        return
    session.ui.print(
        session.t("harness.shell.saved", label=shell.label, tool=shell.tool_name, syntax=shell.syntax), "ok"
    )
