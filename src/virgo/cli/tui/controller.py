"""Scheduling and lifecycle for the fullscreen client, independent of Textual."""

from __future__ import annotations

import copy
import itertools
import threading
from collections import deque
from collections.abc import Callable

from ...ai.types import UserMessage
from ...harness.session import AgentSession
from ..repl import EXIT
from .bridge import FullscreenUI, Update


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
    def pending(self) -> list[str]:
        return [*[f"steering: {text}" for text in self._steering], *self._deferred]

    def _session_reset(self, session: AgentSession) -> None:
        self.ui.bind_signal("main", session.main.agent.signal)
        self.ui.push("reset", copy.deepcopy(session.store.branch()))

    def _agent_event(self, event, runtime) -> None:
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

    def _launch(self, fn: Callable[[], object], *, concurrent: bool = False) -> None:
        if self.closing:
            return
        ticket = next(self._ids)
        if not concurrent:
            self.busy = True
            self._interrupted = False
            self.session.main.agent.signal.clear()

        def run() -> None:
            try:
                fn()
            except Exception as error:  # noqa: BLE001 - errors become transcript entries
                self.ui.print(f"{type(error).__name__}: {error}", "error")
            finally:
                self.ui.push("helper_done" if concurrent else "done", ticket)

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
            self._launch(lambda: cmd.fn(self.session, text.partition(" ")[2]), concurrent=True)
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
                text = self._deferred.popleft()
                self._launch(lambda: self.session.prompt(text))
            elif self.session.wakeups:
                self._launch(self.session.run_pending)
        return out

    def abort(self) -> None:
        if self.closing:
            return
        self._interrupted = True
        self.session.abort()
        self.ui.cancel_confirmations("main")
        self.ui.cancel_inputs()
        self.ui.print("Interrupted.", "warn")

    def request_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self._deferred.clear()
        self._steering.clear()
        self.ui.close()
        self.session.abort()
        threads = [self._worker, *self._helpers.values()]

        def shutdown() -> None:
            try:
                # Services must be stopped before waiting on workers: jobs or
                # MCP requests may be precisely what those workers await.
                self.session.shutdown()
                for worker in threads:
                    if worker is not None and worker is not threading.current_thread():
                        worker.join(timeout=5)
                        if worker.is_alive():
                            self.ui.print("A worker is still stopping; its cancellation signal is set.", "warn")
            except Exception as error:  # noqa: BLE001
                self.ui.print(f"Shutdown failed: {error}", "error")
            finally:
                for unsubscribe in self._unsubscribers:
                    unsubscribe()
                self._unsubscribers.clear()
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
