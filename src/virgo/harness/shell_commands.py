"""Per-session shell selection, shared by fullscreen and plain slash commands."""
from __future__ import annotations

import os

from ..tools.shell import Shell, default_shell, discover_shells, make_shell_tool, probe_shell
from .extensions import ROLES_ALL

USAGE = 'Usage: /shell [name|absolute path|auto] (quote paths containing spaces)'


class ShellSelection:
    def __init__(self, session, catalog: tuple[Shell, ...] | None = None):
        self.session = session
        # Startup authority is a ceiling; later probes also check the current
        # policy so neither direction of a mode change broadens authority.
        self._probe_versions = session.settings.get("execution.mode") == "no-limit"
        # Share only the startup snapshot; each session owns its selection and additions.
        self.catalog = tuple(discover_shells(execute=self._probe_versions)) if catalog is None else catalog
        self.available = list(self.catalog)
        self.current: Shell | None = None
        choice = os.environ.get("VIRGO_SHELL") or session.settings.get("shell.executable", "auto")
        try:
            self.current = default_shell(self.available, t=session.t) if choice == "auto" else self.probe(choice)
            if choice == "auto" and catalog is not None:
                self.current = self.probe(self.current.executable)
            self.remember(self.current)
        except (ValueError, TypeError) as error:
            self.current = None
            session.ui.print(session.t("harness.shell.unavailable", error=error), "warn")

    def probe(self, value: str) -> Shell:
        execute = self._probe_versions and self.session.services["execution"].name == "host"
        return probe_shell(value, t=self.session.t, execute=execute)

    def remember(self, shell: Shell) -> None:
        for index, item in enumerate(self.available):
            if item.executable == shell.executable:
                self.available[index] = shell
                return
        self.available.append(shell)

    def install_tools(self) -> None:
        s = self.session
        tools = []
        if self.current is None:
            s.replace_tools("shell", tools, owner="core.shell")
            return
        jobs = s.services["jobs"]

        def execution(ctx):
            return s.services["execution"].for_context(ctx)

        def on_unavailable(ctx, args, error):
            provider = s.services["execution"]
            handler = getattr(provider, "confirm_no_limit_once", None)
            return handler(ctx, args, error) if callable(handler) else None

        if jobs.background_enabled:
            # Children keep a foreground-only shell; the main runtime may background a command.
            tools.append((make_shell_tool(self.current, execution=execution, on_unavailable=on_unavailable), ("subagent",)))
            tools.append((make_shell_tool(self.current, jobs, execution=execution, on_unavailable=on_unavailable), ("main",)))
        else:
            tools.append((make_shell_tool(self.current, execution=execution, on_unavailable=on_unavailable), ROLES_ALL))
        s.replace_tools("shell", tools, owner="core.shell")

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
            shell = service.probe(shell.executable)
        else:
            shell = service.probe(default_shell(service.available, t=session.t).executable) if automatic else service.probe(choice)
        service.select(shell, automatic=automatic)
    except (ValueError, OSError) as error:
        session.ui.print(session.t("harness.shell.failed", error=error, usage=session.t("harness.shell.usage")), "error")
        return
    session.ui.print(
        session.t("harness.shell.saved", label=shell.label, tool=shell.tool_name, syntax=shell.syntax), "ok"
    )
