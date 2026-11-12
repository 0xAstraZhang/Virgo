"""Per-session shell selection, shared by fullscreen and plain slash commands."""
from __future__ import annotations

import os

from ..tools.shell import Shell, ShellTool, default_shell, discover_shells, make_shell_tool, probe_shell
from .extensions import ROLES_ALL

USAGE = 'Usage: /shell [name|absolute path|auto] (quote paths containing spaces)'


class ShellSelection:
    def __init__(self, session):
        self.session = session
        self.current: Shell | None = None
        choice = os.environ.get("VIRGO_SHELL") or session.settings.get("shell.executable", "auto")
        try:
            self.current = default_shell() if choice == "auto" else probe_shell(choice)
        except (ValueError, TypeError) as error:
            session.ui.print(f"Shell unavailable: {error}. Use /shell to select an installed interpreter.", "warn")

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
                raise ValueError("Cannot switch shells while the agent is running.")
            # Save first so a settings-write failure leaves the live binding and prompt intact.
            s.settings.save_user({"shell": {"executable": "auto" if automatic else shell.executable}})
            self.current = shell
            self.install_tools()
            s.main.tools = s.tools_for("main")
            s.main.agent.cfg.tools = s.main.tools
            s.main.rebuild_system()
            try:
                s.store.append("custom", kind="shell_change", data={
                    "shell": shell.kind, "executable": shell.executable, "version": shell.version,
                })
            except OSError as error:
                s.ui.print(f"Shell selected, but the session record could not be saved: {error}", "warn")


def configure_shell(session, args: str) -> None:
    service = session.services["shell"]
    if session.busy:
        session.ui.print("Cannot switch shells while the agent is running.", "error")
        return
    choice = args.strip()
    if len(choice) >= 2 and choice[0] == choice[-1] and choice[0] in {"'", '"'}:
        choice = choice[1:-1]
    try:
        automatic = choice == "auto"
        if not choice:
            current = service.current
            options = discover_shells(current.executable if current else None)
            # Keep the selected interpreter first so Enter preserves it.
            options.sort(key=lambda shell: shell != current)
            labels = [("✓ " if shell == current else "") + shell.label for shell in options]
            if not session.ui.interactive:
                session.ui.print("Current shell: " + (current.label if current else "unavailable"))
                session.ui.print("Available shells:\n" + "\n".join(labels) + "\n" + USAGE)
                return
            if not options:
                session.ui.print("No supported shell is available. Install PowerShell 7 or Bash, then retry /shell.", "error")
                return
            label = session.ui.select("Command shell — choose an interpreter (saved as user default)", labels)
            if label is None:
                session.ui.print("Kept the current shell.", "dim")
                return
            shell = options[labels.index(label)]
            # It may have disappeared while the picker was open.
            shell = probe_shell(shell.executable)
        else:
            shell = default_shell() if automatic else probe_shell(choice)
        service.select(shell, automatic=automatic)
    except (ValueError, OSError) as error:
        session.ui.print(f"Shell selection failed: {error}\n{USAGE}", "error")
        return
    session.ui.print(
        f"shell: {shell.label}\nTool: {shell.tool_name}; syntax: {shell.syntax}. "
        "Saved as user default; project/runtime settings and VIRGO_SHELL take precedence on restart.", "ok"
    )
