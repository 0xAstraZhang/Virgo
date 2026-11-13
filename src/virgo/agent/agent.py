"""Stateful wrapper around the loop: message state, steering/follow-up queues, abort, listeners."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable

from ..ai.types import UserMessage
from .loop import run_loop
from .types import AgentConfig, AgentEvent


class Agent:
    def __init__(self, cfg: AgentConfig, messages: list | None = None, signal: threading.Event | None = None):
        self.cfg = cfg
        self.messages: list = messages if messages is not None else []
        self.signal = signal or threading.Event()
        self._own_signal = signal is None
        self._listeners: list[Callable[[AgentEvent], None]] = []
        self._steer: deque = deque()
        self._follow: deque = deque()
        self._lock = threading.Lock()
        self._emit_lock = threading.RLock()
        self.running = False

    # -- events -------------------------------------------------------------
    def subscribe(self, fn: Callable[[AgentEvent], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    def _emit(self, ev: AgentEvent) -> None:
        with self._emit_lock:  # tools may emit from worker threads
            for fn in list(self._listeners):
                try:
                    fn(ev)
                except Exception:  # noqa: BLE001 - a broken listener must not kill the run
                    pass

    # -- queues -------------------------------------------------------------
    def steer(self, msg) -> None:
        """Deliver after the current tool batch, before the next model call."""
        with self._lock:
            self._steer.append(_as_message(msg))

    def follow_up(self, msg) -> None:
        """Deliver when the agent would otherwise stop."""
        with self._lock:
            self._follow.append(_as_message(msg))

    def _drain(self, q: deque) -> list:
        with self._lock:
            items = list(q)
            q.clear()
            return items

    def drain_queues(self) -> list:
        return self._drain(self._steer) + self._drain(self._follow)

    # -- running ------------------------------------------------------------
    def prompt(self, msgs) -> None:
        if isinstance(msgs, (str, UserMessage)):
            msgs = [msgs]
        prompts = [_as_message(m) for m in msgs]
        if self.cfg.can_run is not None and not self.cfg.can_run():
            with self._lock:
                self._steer.extend(prompts)
            return
        with self._lock:
            if self.running:
                raise RuntimeError("agent is already running; use steer() or follow_up()")
            self.running = True
        if self._own_signal:
            self.signal.clear()
        try:
            run_loop(
                self.messages,
                prompts,
                self.cfg,
                self._emit,
                self.signal,
                steering=lambda: self._drain(self._steer),
                follow_ups=lambda: self._drain(self._follow),
            )
        finally:
            with self._lock:
                self.running = False

    def continue_(self) -> None:
        self.prompt([])

    def abort(self) -> None:
        self.signal.set()

    @property
    def last_text(self) -> str:
        from ..ai.types import AssistantMessage

        for m in reversed(self.messages):
            if isinstance(m, AssistantMessage):
                return m.text()
        return ""


def _as_message(m):
    return UserMessage(m) if isinstance(m, str) else m
