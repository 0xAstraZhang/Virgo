"""Shared animation clock and run summary; event rows own their active lifetimes."""

from __future__ import annotations

import time

from rich.text import Text

from ...agent.types import AgentEvent
from ...ai.types import AssistantMessage
from ..i18n import tr, translator_for
from .bridge import RunFinished

FRAMES = ("·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢")
FRAME_SECONDS = 0.12


def elapsed_text(seconds: float, owner=None) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return tr(owner, "ui.elapsed.hours", hours=hours, minutes=minutes, seconds=seconds)
    if minutes:
        return tr(owner, "ui.elapsed.minutes", minutes=minutes, seconds=seconds)
    return tr(owner, "ui.elapsed.seconds", seconds=seconds)


class RunActivity:
    def __init__(self, translator=None) -> None:
        self.translator = translator_for(translator)
        self.started_at: float | None = None
        self.phase = tr(self, "ui.activity.working")
        self.waiting: str | None = None
        self.effort = ""
        self.saw_agent = False
        self.stop_reason = "stop"
        self.running_tools: dict[str, str] = {}
        self.content = Text()
        self.display = False

    def reset(self) -> None:
        self.started_at = None
        self.waiting = None
        self.running_tools.clear()
        self.display = False

    def begin(self, started_at: float, effort: str) -> None:
        self.reset()
        self.started_at = started_at
        self.effort = effort
        self.phase = tr(self, "ui.activity.working")
        self.saw_agent = False
        self.stop_reason = "stop"
        self.display = True
        self.tick()

    def consume(self, event: AgentEvent) -> None:
        if self.started_at is None or event.agent != "main":
            return
        if event.type == "agent_start":
            self.saw_agent = True
            self.phase = tr(self, "ui.activity.thinking")
        elif event.type == "turn_start":
            self.phase = tr(self, "ui.activity.thinking")
        elif event.type == "message_update" and event.stream_event is not None:
            kind = event.stream_event.type
            if kind == "retry":
                self.phase = tr(self, "ui.activity.retrying")
            elif kind.startswith("thinking_"):
                self.phase = tr(self, "ui.activity.thinking")
            elif kind.startswith("text_"):
                self.phase = tr(self, "ui.activity.responding")
            elif kind.startswith("toolcall_"):
                self.phase = tr(self, "ui.activity.preparing")
        elif event.type == "message_end" and isinstance(event.message, AssistantMessage):
            self.stop_reason = event.message.stop_reason
            self.phase = tr(self, "ui.activity.working")
        elif event.type == "tool_execution_start":
            self.running_tools[event.tool_call_id or ""] = event.tool_name or "tool"
            self.phase = self._tool_phase()
        elif event.type == "tool_execution_end":
            self.running_tools.pop(event.tool_call_id or "", None)
            self.phase = self._tool_phase() if self.running_tools else tr(self, "ui.activity.working")
        self.tick()

    def _tool_phase(self) -> str:
        if len(self.running_tools) > 1:
            return tr(self, "ui.activity.running_tools", count=len(self.running_tools))
        return tr(self, "ui.activity.running_tool", name=next(iter(self.running_tools.values())))

    def tick(self) -> None:
        if self.started_at is None:
            return
        elapsed = max(0.0, time.monotonic() - self.started_at)
        frame = FRAMES[int(elapsed / FRAME_SECONDS) % len(FRAMES)]
        phase = self.waiting or self.phase
        label = Text(f"{frame} {phase}…", no_wrap=True, overflow="ellipsis")
        detail = elapsed_text(elapsed, self)
        if phase == tr(self, "ui.activity.thinking") and self.effort:
            detail += tr(self, "ui.activity.effort", effort=self.effort)
        label.append(f" ({detail})", style="dim")
        self.content = label

    def finish(self, result: RunFinished, finished_at: float) -> str | None:
        if self.started_at is None:
            return None
        elapsed = elapsed_text(finished_at - self.started_at, self)
        summary = None
        if self.saw_agent:
            if result.interrupted or self.stop_reason == "aborted":
                summary = tr(self, "ui.activity.interrupted", elapsed=elapsed)
            elif result.failed or self.stop_reason == "error":
                summary = tr(self, "ui.activity.failed", elapsed=elapsed)
            elif self.stop_reason in ("length", "refusal"):
                summary = tr(self, "ui.activity.stopped", elapsed=elapsed)
            else:
                summary = tr(self, "ui.activity.done", elapsed=elapsed, time=time.strftime("%H:%M"))
        self.reset()
        return summary
