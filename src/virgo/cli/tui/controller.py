"""Scheduling and lifecycle for the fullscreen client, independent of Textual."""

from __future__ import annotations

import copy
import itertools
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

from ...ai.types import UserMessage
from ...harness.session import AgentSession
from ...harness.store import SessionStore
from ...harness.ui import UI
from ..i18n import tr
from ..repl import EXIT
from .bridge import FullscreenUI, RunFinished, StagedFullscreenUI, Update


class RunController:
    """All methods except worker closures run on the UI thread.

    ``poll`` returns presentation updates and advances the scheduler. A worker
    remains busy until its completion is consumed here, avoiding overlapping
    calls during the gap between a fast worker exiting and the next UI frame.
    """

    def __init__(self, session: AgentSession, ui: FullscreenUI) -> None:
        self.session = session
        self.ui = ui
        self.busy = False
        self.closing = False
        self.closed = False
        self.browsing = False
        self._lifecycle_lock = threading.RLock()
        self._switch_candidate: AgentSession | None = None
        self._started = False
        self._interrupted = False
        self._worker: threading.Thread | None = None
        self._helpers: dict[int, threading.Thread] = {}
        self._ids = itertools.count(1)
        self._deferred: deque[str] = deque()
        self._steering: list[str] = []
        self._initial_reset: Update | None = None
        self._shutdown_worker: threading.Thread | None = None
        self._unsubscribers = [
            session.bus.on("session_start", self._session_reset, priority=-100),
            session.bus.on("agent_event", self._agent_event, priority=100),
        ]
        ui.bind_signal("main", session.main.agent.signal)

    @property
    def can_switch(self) -> bool:
        return not self.busy and not self.closing and not self._helpers and not self._deferred

    def open_session(self, path: Path | str, *, initial: str | None = None) -> bool:
        """Open a saved conversation without retaining another workspace's services."""
        if not self.can_switch:
            return False

        def open_saved() -> None:
            previous = self.session
            target = previous.live_store(path)
            if target is None:
                target = SessionStore.load(Path(path))
                target.build_context()  # Validate before starting another workspace's services.
            if target.cwd.resolve() == previous.cwd:
                if self.closing:
                    return
                previous.open_session(path)
            else:
                if not target.cwd.is_dir():
                    raise FileNotFoundError(tr(self, "ui.session.missing_workspace", path=target.cwd))
                candidate = AgentSession(
                    target.cwd, ui=StagedFullscreenUI(self.ui), session=path, **previous.session_options
                )
                with self._lifecycle_lock:
                    closing = self.closing
                    if not closing:
                        self._switch_candidate = candidate
                if closing:
                    candidate.shutdown()
                    return
                try:
                    previous.shutdown()
                    # Teammates can finish emitting after their abort signal.
                    # Persist their final messages without painting the next conversation.
                    previous.ui = UI()
                    with self._lifecycle_lock:
                        if self.closing:
                            closing = True
                        else:
                            for unsubscribe in self._unsubscribers:
                                unsubscribe()
                            self.session = candidate
                            candidate.ui = self.ui
                            self.ui.set_translator(candidate.translator)
                            self._unsubscribers = [
                                candidate.bus.on("session_start", self._session_reset, priority=-100),
                                candidate.bus.on("agent_event", self._agent_event, priority=100),
                            ]
                            self.ui.push("session_changed", candidate)
                            self._session_reset(candidate)
                    if closing:
                        candidate.shutdown()
                        return
                except BaseException:
                    candidate.shutdown()
                    raise
                finally:
                    with self._lifecycle_lock:
                        self._switch_candidate = None
            if initial and not self.closing:
                self.session.prompt(initial)

        self._launch(open_saved)
        return True

    def new_session(self, *, initial: str | None = None) -> bool:
        if not self.can_switch:
            return False

        def start_new() -> None:
            if self.closing:
                return
            self.session.new_session()
            if initial and not self.closing:
                self.session.prompt(initial)

        self._launch(start_new)
        return True

    @property
    def pending(self) -> list[str]:
        unsaved = (
            [tr(self, "ui.controller.unprocessed", text=message.text()) for message in self.session.wakeups]
            if self.session.store.save_error is not None
            else []
        )
        return [
            *[tr(self, "ui.controller.steering", text=text) for text in self._steering],
            *self._deferred,
            *unsaved,
        ]

    def _session_reset(self, session: AgentSession) -> None:
        self.ui.bind_signal("main", session.main.agent.signal)
        self.ui.push("reset", copy.deepcopy(session.store.branch()))

    def _agent_event(self, event, runtime) -> None:
        if runtime.session is not self.session:
            return
        self.ui.bind_signal(event.agent, runtime.agent.signal)
        if event.agent == "main" and event.type == "agent_start" and (self.closing or self._interrupted):
            # Agent.prompt resets its own signal. An interrupt may have arrived
            # after scheduling but before that reset on the worker thread.
            runtime.agent.abort()

    def start(self, initial: str | None = None) -> None:
        if self._started or self.closing:
            return
        self._started = True
        self._initial_reset = Update("reset", copy.deepcopy(self.session.store.branch()))
        if initial:
            self.submit(initial)

    def _launch(
        self, fn: Callable[[], object], *, concurrent: bool = False, command: str | None = None
    ) -> None:
        if self.closing:
            return
        ticket = next(self._ids)
        if not concurrent:
            self.busy = True
            self._interrupted = False
            self.session.main.agent.signal.clear()
            self.ui.push("run_start", ticket)

        def run() -> None:
            failed = False
            try:
                fn()
            except Exception as error:  # noqa: BLE001 - errors become transcript entries
                failed = True
                self.session.report_error(
                    error,
                    phase="cli.command" if concurrent else "cli.tui_worker",
                    context={"ticket": ticket, **({"command": command} if command else {})},
                )
            finally:
                if concurrent:
                    self.ui.push("helper_done", ticket)
                else:
                    self.ui.push(
                        "done",
                        RunFinished(
                            ticket,
                            interrupted=self._interrupted or self.session.main.agent.signal.is_set(),
                            failed=failed,
                        ),
                    )

        worker = threading.Thread(target=run, daemon=True, name=f"virgo-{'command' if concurrent else 'run'}")
        if concurrent:
            self._helpers[ticket] = worker
        else:
            self._worker = worker
        worker.start()

    def submit(self, text: str) -> None:
        text = text.strip()
        if not text or self.closing:
            return
        if text in EXIT:
            self.request_close()
            return
        cmd = self.session.commands.get(text[1:].split(" ", 1)[0]) if text.startswith("/") else None
        if cmd is not None and cmd.concurrent:
            self._launch(
                lambda: cmd.fn(self.session, text.partition(" ")[2]), concurrent=True, command=cmd.name
            )
        elif self.busy:
            if text.startswith("/") and not text.startswith("//"):
                self._deferred.append(text)
            elif self.session.steer(text):
                self._steering.append(text)
            else:
                # The worker may be starting, executing a slash command, or
                # already finished with its done event still waiting for poll.
                # Queue a normal prompt in those windows instead of stranding
                # it in an agent queue that nobody will drain.
                self._deferred.append(text)
        else:
            self._launch(lambda: self.session.prompt(text))

    def poll(self) -> list[Update]:
        out = []
        if self._initial_reset is not None:
            out.append(self._initial_reset)
            self._initial_reset = None
        for update in self.ui.drain():
            if update.kind == "done":
                self.busy = False
                out.append(update)
            elif update.kind == "helper_done":
                self._helpers.pop(update.payload, None)
            elif update.kind == "wake":
                pass  # The scheduler below checks wakeups after all queued events.
            elif update.kind == "event":
                ev = update.payload
                if ev.type == "message_end" and ev.agent == "main" and isinstance(ev.message, UserMessage):
                    text = ev.message.text()
                    if text in self._steering:
                        self._steering.remove(text)
                out.append(update)
            elif update.kind == "confirmation":
                if self.ui.is_confirmation_pending(update.payload.id):
                    out.append(update)
            elif update.kind == "input":
                if self.ui.is_input_pending(update.payload.id):
                    out.append(update)
            else:
                if update.kind == "reset":
                    self._steering.clear()
                if update.kind == "closed":
                    self.closed = True
                    self.busy = False
                out.append(update)
        if not self.closing and not self.busy:
            if self._deferred:
                # Browsing must not strand work that was queued before it opened.
                text = self._deferred.popleft()
                self._launch(lambda: self.session.prompt(text))
            elif self.session.wakeups and not self.browsing and self.session.main.can_run():
                self._launch(self.session.run_pending)
        return out

    def abort(self) -> None:
        if self.closing:
            return
        self._interrupted = True
        self.session.abort()
        self.ui.cancel_confirmations("main")
        self.ui.cancel_inputs()
        self.ui.print(tr(self, "ui.event.interrupted"), "warn")

    def request_close(self) -> None:
        with self._lifecycle_lock:
            if self.closing:
                return
            self.closing = True
            sessions = [self.session]
            if self._switch_candidate is not None:
                sessions.append(self._switch_candidate)
        self._deferred.clear()
        self._steering.clear()
        self.ui.close()
        self.session.abort()
        threads = [self._worker, *self._helpers.values()]

        def shutdown() -> None:
            try:
                # Services must be stopped before waiting on workers: jobs or
                # MCP requests may be precisely what those workers await.
                for session in sessions:
                    session.shutdown()
                for worker in threads:
                    if worker is not None and worker is not threading.current_thread():
                        worker.join(timeout=5)
                        if worker.is_alive():
                            self.ui.print(tr(self, "ui.controller.worker_stopping"), "warn")
                # A command may finish its final write after shutdown hooks.
                # Recheck only the stores owned by the sessions we closed.
                for session in sessions:
                    session.cleanup_empty_stores()
            except Exception as error:  # noqa: BLE001
                session.report_error(error, phase="session.shutdown")
            finally:
                with self._lifecycle_lock:
                    unsubscribers = self._unsubscribers
                    self._unsubscribers = []
                for unsubscribe in unsubscribers:
                    unsubscribe()
                self.ui.push("closed")

        self._shutdown_worker = threading.Thread(target=shutdown, daemon=True, name="virgo-shutdown")
        self._shutdown_worker.start()

    def wait_closed(self, timeout: float = 5.0) -> bool:
        """Bounded cleanup after App.run has restored the terminal, never in a UI handler."""
        worker = self._shutdown_worker
        if worker is None:
            return False
        worker.join(timeout=timeout)
        return not worker.is_alive()
