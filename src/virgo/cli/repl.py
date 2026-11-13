"""Interactive REPL. The agent runs on a worker thread so Ctrl-C can abort it cleanly."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from .. import __version__
from ..features.permission import permission_mode_label
from ..harness.session import AgentSession
from .console import ConsoleBroker, TerminalUI
from .i18n import tr

EXIT = {"/exit", "/quit", "exit", "quit", ":q"}


def _setup_readline(history: Path, session: AgentSession) -> None:
    try:
        import readline
    except ImportError:
        return
    readline.set_auto_history(False)
    try:
        history.parent.mkdir(parents=True, exist_ok=True)
        if history.exists():
            readline.read_history_file(str(history))
        readline.set_history_length(1000)
        import atexit

        atexit.register(lambda: readline.write_history_file(str(history)))
        names = ["/" + n for n in [*session.commands.names(), *session.templates]]

        def complete(text, state):
            opts = [n for n in names if n.startswith(text)]
            return opts[state] if state < len(opts) else None

        readline.set_completer(complete)
        readline.set_completer_delims(" ")
        readline.parse_and_bind("tab: complete")
    except OSError:
        pass


def run_repl(
    session: AgentSession, ui: TerminalUI, broker: ConsoleBroker, initial: str | None = None
) -> None:
    _setup_readline(session.paths.user / "history", session)
    ui.print(
        tr(
            session,
            "ui.repl.welcome",
            version=__version__,
            model=session.model.spec,
            effort=session.effort,
            permissions=permission_mode_label(session.permission_mode, session.t),
            cwd=session.cwd,
        ),
        "dim",
    )
    if session.main.messages:
        ui.print(tr(session, "ui.repl.resumed", count=len(session.main.messages)), "dim")
    worker: list[threading.Thread | None] = [None]
    deferred: list[str] = []

    def busy() -> bool:
        return worker[0] is not None and worker[0].is_alive()

    def start(fn) -> None:
        session.main.agent.signal.clear()

        def wrap():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                session.report_error(e, phase="cli.repl_worker")
            finally:
                broker.push("done")

        worker[0] = threading.Thread(target=wrap, daemon=True, name="virgo-run")
        worker[0].start()

    broker.start_reader()
    if initial:
        start(lambda: session.prompt(initial))
        if not initial.startswith("/"):
            broker.resume_reading()
    else:
        ui.prompt()
    last_interrupt = 0.0
    while True:
        try:
            item = broker.events.get(timeout=0.25)
        except queue.Empty:
            continue
        except KeyboardInterrupt:
            if busy():
                session.abort()
                broker.cancel_pending()
                ui.print(tr(session, "ui.repl.interrupting"), "warn")
                continue
            if time.time() - last_interrupt < 2:
                break
            last_interrupt = time.time()
            ui.print(tr(session, "ui.repl.exit"), "dim")
            ui.prompt()
            continue
        kind = item[0]
        if kind == "eof":
            if busy():
                session.abort()
                worker[0].join(timeout=5)
            break
        if kind == "line":
            line = item[1].strip()
            if not line:
                if not busy():
                    ui.prompt()
                else:
                    broker.resume_reading()
                continue
            if line in EXIT:
                if busy():
                    session.abort()
                break
            cmd = session.commands.get(line[1:].split(" ", 1)[0]) if line.startswith("/") else None
            if cmd is not None and cmd.concurrent:
                try:
                    cmd.fn(session, line.partition(" ")[2])  # read-only: run inline, even while busy
                except Exception as e:  # noqa: BLE001 - a failing command must not end the REPL
                    session.report_error(e, phase="cli.command", context={"command": cmd.name})
                if not busy():
                    ui.prompt()
                else:
                    broker.resume_reading()
                continue
            if busy():
                if line.startswith("/"):
                    deferred.append(line)
                    ui.print(tr(session, "ui.repl.queued_command"), "dim")
                elif session.steer(line):
                    ui.print(tr(session, "ui.repl.steered"), "dim")
                else:
                    # The worker is running a command, or the agent is already past its last queue
                    # check: nothing would drain a steering message, so send it as the next prompt.
                    deferred.append(line)
                    ui.print(tr(session, "ui.repl.queued_prompt"), "dim")
                broker.resume_reading()
                continue
            start(lambda line=line: session.prompt(line))
            if cmd is None:
                broker.resume_reading()
        elif kind == "wake":
            if not busy() and session.wakeups and session.main.can_run():
                start(session.run_pending)
        elif kind == "done":
            if deferred:
                start(lambda line=deferred.pop(0): session.prompt(line))
            elif session.wakeups and session.main.can_run():
                start(session.run_pending)
            else:
                ui.prompt()
    ui.print(tr(session, "ui.repl.bye"), "dim")
    broker.close()
    session.shutdown()
