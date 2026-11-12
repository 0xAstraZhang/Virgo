"""Doubles and helpers for driving a real AgentSession offline with the faux provider."""

from __future__ import annotations

import copy
import json
import queue
import threading
import time

from virgo import ai
from virgo.agent.types import ToolCallEvent
from virgo.ai.providers import faux
from virgo.ai.stream import EventStream, StreamEvent
from virgo.ai.types import ToolResultMessage
from virgo.harness.ui import UI


class RecordingUI(UI):
    """Collects output; answers permission questions from a list (default: deny)."""

    def __init__(self, answers=None):
        self.lines: list[str] = []
        self.events = []
        self.questions: list[str] = []
        self.answers = list(answers or [])
        self.woken = threading.Event()
        self._lock = threading.Lock()

    def on_event(self, ev):
        with self._lock:
            self.events.append(ev)

    def print(self, text, style=None):
        with self._lock:
            self.lines.append(text)

    def confirm(self, question, agent="main"):
        with self._lock:
            self.questions.append(question)
            return self.answers.pop(0) if self.answers else "n"

    def wake(self):
        self.woken.set()

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class ReplUI(RecordingUI):
    def prompt(self):
        pass


class ScriptedBroker:
    """Stands in for the terminal reader: the test decides when each line arrives."""

    def __init__(self):
        self.events: queue.Queue = queue.Queue()

    def push(self, *item):
        self.events.put(item)

    def start_reader(self):
        pass

    resume_reading = cancel_pending = close = start_reader


class PickerUI(UI):
    """Answers preference pickers with a fixed choice or a function of the request."""

    supports_preferences = True

    def __init__(self, answer=None):
        self.answer = answer
        self.requests = []
        self.lines = []

    def choose_preferences(self, request):
        self.requests.append(request)
        return self.answer(request) if callable(self.answer) else self.answer

    def print(self, text, style=None):
        self.lines.append(text)


def calls(model_id: str = "test") -> list[dict]:
    """Requests the faux provider received for ``faux/<model_id>``."""
    return faux.CALLS[model_id]


def last_message_text(request: dict) -> str:
    """The content of the newest message in a recorded faux request."""
    return str(request["messages"][-1]["content"])


def tool_results(session) -> list[ToolResultMessage]:
    return [m for m in session.main.messages if isinstance(m, ToolResultMessage)]


def tool_call_event(session, name, args, *, agent="main") -> ToolCallEvent:
    return ToolCallEvent("1", name, args, session.tool(name), agent, session.cwd)


def wait_until(predicate, timeout: float = 5.0) -> None:
    """Poll a condition owned by another thread; fail the test if it never holds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        time.sleep(0.005)


def response_stream(message) -> EventStream:
    kind = "error" if message.stop_reason in {"error", "aborted"} else "done"
    return EventStream(iter([StreamEvent("start", message), StreamEvent(kind, message)]), message)


def install_responses(monkeypatch, *messages) -> list[tuple]:
    """Answer each ``ai.stream`` call with the next message; return the (model, context) seen."""
    seen = []
    replies = iter(messages)

    def stream(model, context, opts):
        seen.append((model, copy.deepcopy(context)))
        return response_stream(next(replies))

    monkeypatch.setattr(ai, "stream", stream)
    return seen


def write_legacy_session(path, cwd, created, title="saved") -> None:
    """Write a version-1 session file, as saved before the global session catalog existed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "session", "version": 1, "id": path.stem, "cwd": str(cwd), "timestamp": created}
    entries = [{"type": "message", "id": "user", "parent_id": None, "message": {"role": "user", "content": title}}]
    path.write_text("\n".join(json.dumps(row) for row in [header, *entries]) + "\n")
