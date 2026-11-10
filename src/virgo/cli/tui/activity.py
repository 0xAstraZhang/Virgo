"""Live run feedback, kept outside the scrollable conversation."""

from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import Static

from ...agent.types import AgentEvent
from ...ai.types import AssistantMessage
from .bridge import RunFinished

FRAMES = ("·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢")
FRAME_SECONDS = 0.12


def elapsed_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


class RunActivity(Static):
    DEFAULT_CSS = """
    RunActivity { height: 1; color: $accent; background: $background; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", markup=False, **kwargs)
        self.started_at: float | None = None
        self.phase = "Working"
        self.waiting: str | None = None
        self.effort = ""
        self.saw_agent = False
        self.stop_reason = "stop"
        self.running_tools: dict[str, str] = {}
        self.display = False
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(FRAME_SECONDS, self.tick, pause=True)

    def reset(self) -> None:
        self.started_at = None
        self.waiting = None
        self.running_tools.clear()
        self.display = False
        if self._timer is not None:
            self._timer.pause()

    def begin(self, started_at: float, effort: str) -> None:
        self.reset()
        self.started_at = started_at
        self.effort = effort
        self.phase = "Working"
        self.saw_agent = False
        self.stop_reason = "stop"
        self.display = True
        if self._timer is not None:
            self._timer.resume()
        self.tick()

    def consume(self, event: AgentEvent) -> None:
        if self.started_at is None or event.agent != "main":
            return
        if event.type == "agent_start":
            self.saw_agent = True
            self.phase = "Thinking"
        elif event.type == "turn_start":
            self.phase = "Thinking"
        elif event.type == "message_update" and event.stream_event is not None:
            kind = event.stream_event.type
            if kind == "retry":
                self.phase = "Retrying"
            elif kind.startswith("thinking_"):
                self.phase = "Thinking"
            elif kind.startswith("text_"):
                self.phase = "Responding"
            elif kind.startswith("toolcall_"):
                self.phase = "Preparing tools"
        elif event.type == "message_end" and isinstance(event.message, AssistantMessage):
            self.stop_reason = event.message.stop_reason
            self.phase = "Working"
        elif event.type == "tool_execution_start":
            self.running_tools[event.tool_call_id or ""] = event.tool_name or "tool"
            self.phase = self._tool_phase()
        elif event.type == "tool_execution_end":
            self.running_tools.pop(event.tool_call_id or "", None)
            self.phase = self._tool_phase() if self.running_tools else "Working"
        self.tick()

    def _tool_phase(self) -> str:
        if len(self.running_tools) > 1:
            return f"Running {len(self.running_tools)} tools"
        return f"Running {next(iter(self.running_tools.values()))}"

    def tick(self) -> None:
        if self.started_at is None:
            return
        elapsed = max(0.0, time.monotonic() - self.started_at)
        frame = FRAMES[int(elapsed / FRAME_SECONDS) % len(FRAMES)]
        phase = self.waiting or self.phase
        label = Text(f"{frame} {phase}…", no_wrap=True, overflow="ellipsis")
        detail = elapsed_text(elapsed)
        if phase == "Thinking" and self.effort:
            detail += f" · thinking with {self.effort} effort"
        label.append(f" ({detail})", style="dim")
        self.update(label)

    def finish(self, result: RunFinished, finished_at: float) -> str | None:
        if self.started_at is None:
            return None
        elapsed = elapsed_text(finished_at - self.started_at)
        summary = None
        if self.saw_agent:
            if result.interrupted or self.stop_reason == "aborted":
                summary = f"✻ Interrupted after {elapsed}"
            elif result.failed or self.stop_reason == "error":
                summary = f"✻ Failed after {elapsed}"
            elif self.stop_reason in ("length", "refusal"):
                summary = f"✻ Stopped after {elapsed}"
            else:
                summary = f"✻ Worked for {elapsed} · done {time.strftime('%H:%M')}"
        self.reset()
        return summary
