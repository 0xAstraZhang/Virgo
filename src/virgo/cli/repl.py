"""Interactive REPL. The agent runs on a worker thread so Ctrl-C can abort it cleanly."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from .. import __version__
from ..ai.types import UserMessage
from ..harness.session import AgentSession
from .console import ConsoleBroker, TerminalUI

EXIT = {"/exit", "/quit", "exit", "quit", ":q"}


def _setup_readline(history: Path, session: AgentSession) -> None:
    try:
        import readline
    except ImportError:
        return
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
        f"Virgo {__version__} · {session.model.spec} · effort {session.effort} · permissions {session.permission_mode}\n"
        f"{session.cwd}  —  /help for commands, Ctrl-C to interrupt, /exit to quit",
        "dim",
    )
    if session.main.messages:
        ui.print(f"(resumed session with {len(session.main.messages)} messages)", "dim")
    worker: list[threading.Thread | None] = [None]
    deferred: list[str] = []

    def busy() -> bool:
        return worker[0] is not None and worker[0].is_alive()

    def start(fn) -> None:
        def wrap():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                ui.print(f"✗ {type(e).__name__}: {e}", "error")
            finally:
                broker.push("done")

        worker[0] = threading.Thread(target=wrap, daemon=True, name="virgo-run")
        worker[0].start()

    broker.start_reader()
    if initial:
        start(lambda: session.prompt(initial))
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
                ui.print("[interrupting…]", "warn")
                continue
            if time.time() - last_interrupt < 2:
                break
            last_interrupt = time.time()
            ui.print("(Ctrl-C again or /exit to quit)", "dim")
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
                continue
            if line in EXIT:
                if busy():
                    session.abort()
                break
            cmd = session.commands.get(line[1:].split(" ", 1)[0]) if line.startswith("/") else None
            if cmd is not None and cmd.concurrent:
                cmd.fn(session, line.partition(" ")[2])  # read-only: run inline, even while busy
                if not busy():
                    ui.prompt()
                continue
            if busy():
                if line.startswith("/"):
                    deferred.append(line)
                    ui.print("(will run when the agent finishes)", "dim")
                else:
                    session.main.agent.steer(UserMessage(line))
                    ui.print("[steering message queued]", "dim")
                continue
            start(lambda line=line: session.prompt(line))
        elif kind == "wake":
            if not busy() and session.wakeups:
                start(session.run_pending)
        elif kind == "done":
            if deferred:
                start(lambda line=deferred.pop(0): session.prompt(line))
            elif session.wakeups:
                start(session.run_pending)
            else:
                ui.prompt()
    ui.print("bye", "dim")
    session.shutdown()
