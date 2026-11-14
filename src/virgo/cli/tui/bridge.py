"""Thread-safe adapter between the synchronous harness and the full-screen UI.

Workers never touch Textual widgets. Stream events are copied before returning to
providers, which commonly keep mutating the same partial message instance.
"""

from __future__ import annotations

import copy
import itertools
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ...agent.types import AgentEvent
from ...harness.preferences import EFFORT_LEVELS, PreferenceChoice, PreferenceRequest
from ...harness.ui import TIMED_OUT, UI, ScheduleChoice
from ..i18n import tr


@dataclass(frozen=True)
class Update:
    kind: str
    payload: Any = None
    created_at: float = field(default_factory=time.monotonic, compare=False)


@dataclass(frozen=True)
class RunFinished:
    ticket: int
    interrupted: bool = False
    failed: bool = False


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
    preferences: PreferenceRequest | None = None
    details: str | None = None
    multiline: bool = False
    schedule_workspace: bool = False
    schedules: tuple[ScheduleChoice, ...] | None = None
    selected_schedule: str | None = None


@dataclass
class _Waiting:
    request: Confirmation | InputRequest
    ready: threading.Event = field(default_factory=threading.Event)
    timeout_cancelled: threading.Event = field(default_factory=threading.Event)
    answer: str | PreferenceChoice | None = None


class FullscreenUI(UI):
    interactive = True
    supports_preferences = True

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[Update] = queue.SimpleQueue()
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._waiting: dict[int, _Waiting] = {}
        self._inputs: dict[int, _Waiting] = {}
        self._signals: dict[str, threading.Event] = {}
        self._closed = False
        self._local = threading.local()

    def push(self, kind: str, payload: Any = None) -> None:
        self._queue.put(Update(kind, payload))

    def language_changed(self) -> None:
        self.push("language_changed")

    def drain(self) -> list[Update]:
        updates = []
        while True:
            try:
                update = self._queue.get_nowait()
            except queue.Empty:
                return updates
            # Collapse token deltas within one content block only. Native tool
            # status and block boundaries can create timeline events, so their
            # positions must survive even when an entire run fits in one frame.
            if updates and self._stream_update(update) and self._stream_update(updates[-1]):
                previous, current = updates[-1].payload, update.payload
                if (
                    previous.agent == current.agent
                    and previous.stream_event.type == current.stream_event.type
                    and previous.stream_event.index == current.stream_event.index
                ):
                    updates[-1] = update
                    continue
            updates.append(update)

    @staticmethod
    def _stream_update(update: Update) -> bool:
        return (
            update.kind == "event"
            and update.payload.type == "message_update"
            and update.payload.stream_event is not None
            and update.payload.stream_event.type in ("text_delta", "thinking_delta", "toolcall_delta")
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

    @property
    def has_pending_requests(self) -> bool:
        with self._lock:
            return bool(self._waiting or self._inputs)

    def is_confirmation_pending(self, request_id: int) -> bool:
        with self._lock:
            return request_id in self._waiting

    def resolve_confirmation(self, request_id: int, answer: str | None) -> bool:
        if answer not in (None, "y", "n", "a"):
            raise ValueError(tr(self, "ui.bridge.invalid_confirmation"))
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

    def select(self, question: str, options: list[str], *, timeout: float | None = None) -> str | None:
        if not options:
            return None
        return self._ask_input(question, timeout=timeout, options=tuple(options))

    def input(
        self, question: str, *, password: bool = False, default: str = "", timeout: float | None = None
    ) -> str | None:
        return self._ask_input(question, timeout=timeout, password=password, default=default)

    def choose_preferences(self, request: PreferenceRequest) -> PreferenceChoice | None:
        return self._ask_input(
            tr(self, "ui.preference.select_model")
            if request.kind == "model"
            else tr(self, "ui.preference.effort"),
            preferences=request,
        )

    def select_details(self, question: str, options: list[str], details: str) -> str | None:
        return self._ask_input(question, options=tuple(options), details=details)

    def input_multiline(self, question: str, *, default: str = "") -> str | None:
        return self._ask_input(question, default=default, multiline=True)

    @contextmanager
    def schedule_workspace(self):
        nested = getattr(self._local, "schedule_workspace", False)
        self._local.schedule_workspace = True
        if not nested:
            self.push("schedule_workspace", True)
        try:
            yield
        finally:
            self._local.schedule_workspace = nested
            if not nested:
                self.push("schedule_workspace", False)

    def select_schedule(self, choices: list[ScheduleChoice], *, selected: str | None = None) -> str | None:
        return self._ask_input(
            tr(self, "ui.schedule.title"),
            schedules=tuple(choices),
            selected_schedule=selected,
            options=tuple(row.id for row in choices) + ("new",),
        )

    def _ask_input(
        self, question: str, timeout: float | None = None, **kwargs
    ) -> str | PreferenceChoice | None:
        with self._lock:
            signal = self._signals.get("main")
            if self._closed or (signal is not None and signal.is_set()):
                return None
            request = InputRequest(
                next(self._ids),
                question,
                schedule_workspace=getattr(self._local, "schedule_workspace", False),
                **kwargs,
            )
            waiting = _Waiting(request)
            self._inputs[request.id] = waiting
            self.push("input", request)
        deadline = None if timeout is None else time.monotonic() + timeout
        timed_out = False
        while not waiting.ready.wait(0.05):
            if signal is not None and signal.is_set():
                self.resolve_input(request.id, None)
            elif deadline is not None and time.monotonic() >= deadline:
                # Withdraws the form; an answer that won the race is kept instead.
                with self._lock:
                    if not waiting.timeout_cancelled.is_set():
                        timed_out = self.resolve_input(request.id, None)
        return TIMED_OUT if timed_out else waiting.answer

    def cancel_input_timeouts(self) -> None:
        with self._lock:
            for waiting in self._inputs.values():
                waiting.timeout_cancelled.set()

    def is_input_pending(self, request_id: int) -> bool:
        with self._lock:
            return request_id in self._inputs

    def resolve_input(self, request_id: int, answer: str | PreferenceChoice | None) -> bool:
        with self._lock:
            waiting = self._inputs.get(request_id)
            if waiting is None:
                return False
            options = waiting.request.options
            if answer is not None and options is not None and answer not in options:
                raise ValueError(tr(self, "ui.bridge.invalid_option"))
            preferences = waiting.request.preferences
            if answer is not None and preferences is not None:
                if (
                    not isinstance(answer, PreferenceChoice)
                    or answer.model not in {model.spec for model in preferences.models}
                    or answer.effort not in EFFORT_LEVELS
                ):
                    raise ValueError(tr(self, "ui.bridge.invalid_model"))
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


class StagedFullscreenUI(FullscreenUI):
    """Share a live terminal's transport while a candidate owns its language.

    Construction hooks may still print or ask questions through the active
    queue. A failed workspace switch cannot change the current UI's translator.
    Operational state, including cancellation and closure, belongs to the
    underlying bridge; only the candidate translator is held separately.
    """

    def __init__(self, bridge: FullscreenUI) -> None:
        object.__setattr__(self, "_bridge", bridge)
        object.__setattr__(self, "translator", getattr(bridge, "translator", None))

    def __getattr__(self, name):
        return getattr(self._bridge, name)

    def __setattr__(self, name, value) -> None:
        if name in {"_bridge", "translator"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._bridge, name, value)
