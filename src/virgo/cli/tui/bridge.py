"""Thread-safe adapter between the synchronous harness and the full-screen UI.

Workers never touch Textual widgets. Stream events are copied before returning to
providers, which commonly keep mutating the same partial message instance.
"""

from __future__ import annotations

import copy
import itertools
import queue
import threading
from dataclasses import dataclass, field
from typing import Any

from ...agent.types import AgentEvent
from ...harness.ui import UI


@dataclass(frozen=True)
class Update:
    kind: str
    payload: Any = None


@dataclass(frozen=True)
class Confirmation:
    id: int
    question: str
    agent: str


@dataclass(frozen=True)
class InputRequest:
    id: int
    question: str
    options: tuple[str, ...] | None = None
    password: bool = False
    default: str = ""


@dataclass
class _Waiting:
    request: Confirmation | InputRequest
    ready: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None


class FullscreenUI(UI):
    interactive = True

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[Update] = queue.SimpleQueue()
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._waiting: dict[int, _Waiting] = {}
        self._inputs: dict[int, _Waiting] = {}
        self._signals: dict[str, threading.Event] = {}
        self._closed = False

    def push(self, kind: str, payload: Any = None) -> None:
        self._queue.put(Update(kind, payload))

    def drain(self) -> list[Update]:
        updates = []
        while True:
            try:
                update = self._queue.get_nowait()
            except queue.Empty:
                return updates
            # Each partial is a complete detached snapshot. Within a frame,
            # render only the latest adjacent update for the same agent. Retry
            # and lifecycle boundaries stay ordered and are never discarded.
            if updates and self._stream_update(update) and self._stream_update(updates[-1]):
                if updates[-1].payload.agent == update.payload.agent:
                    updates[-1] = update
                    continue
            updates.append(update)

    @staticmethod
    def _stream_update(update: Update) -> bool:
        return (
            update.kind == "event"
            and update.payload.type == "message_update"
            and update.payload.stream_event is not None
            and update.payload.stream_event.type != "retry"
        )

    def on_event(self, ev: AgentEvent) -> None:
        self.push("event", copy.deepcopy(ev))

    def print(self, text: str, style: str | None = None) -> None:
        self.push("print", (str(text), style))

    def wake(self) -> None:
        self.push("wake")

    def bind_signal(self, agent: str, signal: threading.Event) -> None:
        with self._lock:
            self._signals[agent] = signal

    def confirm(self, question: str, agent: str = "main") -> str | None:
        with self._lock:
            signal = self._signals.get(agent)
            if self._closed or (signal is not None and signal.is_set()):
                return None
            request = Confirmation(next(self._ids), question, agent)
            waiting = _Waiting(request)
            self._waiting[request.id] = waiting
            self.push("confirmation", request)
        # No queue or UI lock is held while waiting. Shared cancellation signals
        # also release foreground sub-agent confirmations when the lead aborts.
        while not waiting.ready.wait(0.05):
            if signal is not None and signal.is_set():
                self.resolve_confirmation(request.id, None)
        return waiting.answer

    def is_confirmation_pending(self, request_id: int) -> bool:
        with self._lock:
            return request_id in self._waiting

    def resolve_confirmation(self, request_id: int, answer: str | None) -> bool:
        if answer not in (None, "y", "n", "a"):
            raise ValueError("confirmation answer must be y, n, a or None")
        with self._lock:
            waiting = self._waiting.pop(request_id, None)
            if waiting is None:
                return False
            signal = self._signals.get(waiting.request.agent)
            if self._closed or (signal is not None and signal.is_set()):
                answer = None
            waiting.answer = answer
            waiting.ready.set()
            if answer is None:
                self.push("confirmation_cancelled", request_id)
            return True

    def cancel_confirmations(self, agent: str | None = None) -> None:
        with self._lock:
            ids = [i for i, w in self._waiting.items() if agent is None or w.request.agent == agent]
            for request_id in ids:
                self.resolve_confirmation(request_id, None)

    def select(self, question: str, options: list[str]) -> str | None:
        if not options:
            return None
        return self._ask_input(question, options=tuple(options))

    def input(self, question: str, *, password: bool = False, default: str = "") -> str | None:
        return self._ask_input(question, password=password, default=default)

    def _ask_input(self, question: str, **kwargs) -> str | None:
        with self._lock:
            signal = self._signals.get("main")
            if self._closed or (signal is not None and signal.is_set()):
                return None
            request = InputRequest(next(self._ids), question, **kwargs)
            waiting = _Waiting(request)
            self._inputs[request.id] = waiting
            self.push("input", request)
        while not waiting.ready.wait(0.05):
            if signal is not None and signal.is_set():
                self.resolve_input(request.id, None)
        return waiting.answer

    def is_input_pending(self, request_id: int) -> bool:
        with self._lock:
            return request_id in self._inputs

    def resolve_input(self, request_id: int, answer: str | None) -> bool:
        with self._lock:
            waiting = self._inputs.get(request_id)
            if waiting is None:
                return False
            options = waiting.request.options
            if answer is not None and options is not None and answer not in options:
                raise ValueError("answer must be one of the offered options")
            self._inputs.pop(request_id)
            signal = self._signals.get("main")
            if self._closed or (signal is not None and signal.is_set()):
                answer = None
            waiting.answer = answer
            waiting.ready.set()
            if answer is None:
                self.push("input_cancelled", request_id)
            return True

    def cancel_inputs(self) -> None:
        with self._lock:
            for request_id in list(self._inputs):
                self.resolve_input(request_id, None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.cancel_confirmations()
            self.cancel_inputs()
