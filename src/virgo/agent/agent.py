"""Stateful wrapper around the loop: message state, steering/follow-up queues, abort, listeners."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Sequence

from ..ai.types import AssistantMessage, Message, UserMessage
from ..diagnostics import capture_error, emergency_write
from .loop import run_loop
from .types import AgentConfig, AgentEvent


class Agent:
    def __init__(self, cfg: AgentConfig, messages: list | None = None, signal: threading.Event | None = None):
        self.cfg = cfg
        self.messages: list = messages if messages is not None else []
        self._last_answer: tuple[AssistantMessage, str] | None = None
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

    def append_message(self, msg) -> None:
        """Append a message directly to history and emit start/end lifecycle events."""
        message = _as_message(msg)
        self._emit(AgentEvent("message_start", self.cfg.name, message=message))
        self.messages.append(message)
        self._emit(AgentEvent("message_end", self.cfg.name, message=message))

    def _emit(self, ev: AgentEvent) -> None:
        with self._emit_lock:  # tools may emit from worker threads
            for fn in list(self._listeners):
                try:
                    fn(ev)
                except Exception as error:  # noqa: BLE001 - a broken listener must not kill the run
                    details = capture_error(error, phase="agent.listener", context={
                        "agent": ev.agent,
                        "event": ev.type,
                        "handler": getattr(fn, "__qualname__", type(fn).__qualname__),
                        "tool_name": ev.tool_name,
                        "tool_call_id": ev.tool_call_id,
                    })
                    if self.cfg.on_error is None:
                        emergency_write(details)
                    else:
                        try:
                            self.cfg.on_error(details)
                        except Exception as reporting_error:  # noqa: BLE001
                            emergency_write(details, reporting_error=reporting_error)

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

    def discard_steering(self, predicate: Callable) -> int:
        """Remove only matching messages that the loop has not consumed yet."""
        with self._lock:
            kept = deque(message for message in self._steer if not predicate(message))
            removed = len(self._steer) - len(kept)
            self._steer.clear()
            self._steer.extend(kept)
            return removed

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
                on_answer=self._record_answer,
            )
        finally:
            with self._lock:
                self.running = False

    def abort(self) -> None:
        self.signal.set()

    def _record_answer(self, msg: AssistantMessage, text: str) -> None:
        self._last_answer = (msg, text)

    @property
    def last_text(self) -> str:
        last = next((m for m in reversed(self.messages) if isinstance(m, AssistantMessage)), None)
        if last is None:
            return ""
        answer = self._last_answer
        if answer is not None and answer[0] is last:
            return answer[1]
        return last_assistant_text(self.messages)


def last_assistant_text(messages: Sequence[Message]) -> str:
    """Return the latest assistant text, including contiguous length continuations still in history.

    Stop hooks and compaction also use "continue". The preceding length stop
    identifies which prompts belong to the same answer.
    """
    history = reversed(messages)
    last = next((message for message in history if isinstance(message, AssistantMessage)), None)
    if last is None:
        return ""
    if last.stop_reason not in ("stop", "length") or last.tool_calls():
        return last.text()

    fragments = [last.text()]
    for bridge in history:
        if not isinstance(bridge, UserMessage) or bridge.kind != "continue":
            break
        previous = next(history, None)
        if (
            not isinstance(previous, AssistantMessage)
            or previous.stop_reason != "length"
            or previous.tool_calls()
        ):
            break
        fragments.append(previous.text())
    return "".join(reversed(fragments))


def _as_message(m):
    return UserMessage(m) if isinstance(m, str) else m
