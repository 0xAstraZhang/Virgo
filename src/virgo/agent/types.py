"""Agent-level types (≈ pi-agent-core): tools, tool results, events, loop configuration."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ai.stream import EventStream, StreamEvent
from ..ai.types import ImageContent, TextContent, ToolSpec


@dataclass
class ToolResult:
    content: list  # TextContent / ImageContent: what the model sees
    details: Any = None  # UI / state only, never sent to the model
    is_error: bool = False
    terminate: bool = False

    @staticmethod
    def text(text: str, details: Any = None, **kw) -> ToolResult:
        return ToolResult([TextContent(text)], details=details, **kw)

    @staticmethod
    def error(text: str, details: Any = None) -> ToolResult:
        return ToolResult([TextContent(text)], details=details, is_error=True)

    def as_text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextContent))


@dataclass
class ToolContext:
    call_id: str
    cwd: Path
    signal: threading.Event
    agent: str = "main"
    on_update: Callable[[str], None] | None = None
    review_source: Any = None  # Bound task/runtime source, never the currently selected chat.


@dataclass
class AgentTool:
    name: str
    description: str
    parameters: dict
    execute: Callable[[dict, ToolContext], ToolResult | str]
    label: str = ""
    read_only: bool = False  # does not modify the user's files or system
    sequential: bool = False  # force the whole batch to run sequentially
    prepare_arguments: Callable[[dict], dict] | None = None

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


@dataclass
class ToolCallEvent:
    call_id: str
    tool_name: str
    args: dict  # handlers may mutate in place
    tool: AgentTool | None
    agent: str
    cwd: Path
    signal: threading.Event | None = None
    review_source: Any = None


@dataclass
class Block:
    reason: str
    details: dict | None = None


@dataclass
class Continue:
    """Returned from a stop hook to keep the agent going with an extra user message."""

    text: str


@dataclass
class AgentEvent:
    type: str
    agent: str = "main"
    message: Any = None
    stream_event: StreamEvent | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    args: dict | None = None
    result: ToolResult | None = None
    is_error: bool = False
    tool_results: list = field(default_factory=list)


@dataclass
class AgentConfig:
    tools: list[AgentTool]
    call_model: Callable[[list, threading.Event], EventStream]
    name: str = "main"
    cwd: Path = field(default_factory=Path.cwd)
    before_tool_call: Callable[[ToolCallEvent], Block | None] | None = None
    after_tool_call: Callable[[ToolCallEvent, ToolResult], ToolResult | None] | None = None
    should_stop: Callable[[list], Continue | None] | None = None
    max_turns: int | None = None
    max_parallel: int = 8
    before_tool_execute: Callable[[ToolCallEvent], Block | None] | None = None
    can_run: Callable[[], bool] | None = None
    on_error: Callable[[dict], None] | None = None


def image_result(data: str, mime: str, note: str = "") -> ToolResult:
    content: list = [TextContent(note)] if note else []
    content.append(ImageContent(data, mime))
    return ToolResult(content)
