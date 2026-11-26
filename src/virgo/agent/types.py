"""Agent-level types: tools, tool results, events, loop configuration."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ai.stream import EventStream, StreamEvent
from ..ai.types import ImageContent, TextContent, ToolCall, ToolSpec
from ..i18n import Translator, localize_tool


@dataclass
class ToolResult:
    content: list  # TextContent / ImageContent: what the model sees
    details: Any = None  # UI / state only, never sent to the model
    is_error: bool = False
    terminate: bool = False
    error_kind: str | None = field(default=None, kw_only=True)  # "fatal" | "blocked" | None

    @staticmethod
    def text(text: str, details: Any = None, **kw) -> ToolResult:
        return ToolResult([TextContent(text)], details=details, **kw)

    @staticmethod
    def error(text: str, details: Any = None, *, error_kind: str | None = None) -> ToolResult:
        return ToolResult([TextContent(text)], details=details, is_error=True, error_kind=error_kind)

    def as_text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextContent))


@dataclass
class ToolContext:
    call_id: str
    cwd: Path
    signal: threading.Event
    agent: str = "main"
    on_update: Callable[[str], None] | None = None
    origin: Any = None  # Bound task/runtime source, never the currently selected chat.
    translator: Translator | None = field(default=None, kw_only=True)
    dispatch_event: ToolCallEvent | None = field(default=None, kw_only=True, repr=False, compare=False)

    def t(self, key: str, /, **values: object) -> str:
        return (self.translator or Translator())(key, **values)


EXECUTION_SCOPES = frozenset({"file", "process", "web", "broker", "delegate", "external"})


@dataclass(init=False)
class AgentTool:
    name: str
    description: str
    parameters: dict
    execute: Callable[[dict, ToolContext], ToolResult | str]
    label: str = ""
    side_effect: str = "write"  # "read" or "write"; separate from the run permission
    execution_scope: str = "external"  # trusted adapter contract, never inferred from command text
    sequential: bool = False  # force the whole batch to run sequentially
    prepare_arguments: Callable[[dict], dict] | None = None
    i18n_key: str = field(default="", kw_only=True)
    i18n_values: dict | Callable[[Translator], dict] = field(default_factory=dict, kw_only=True)
    config_fingerprint: str | None = field(default=None, kw_only=True)

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict,
        execute: Callable[[dict, ToolContext], ToolResult | str],
        label: str = "",
        side_effect: str = "write",
        sequential: bool = False,
        prepare_arguments: Callable[[dict], dict] | None = None,
        *,
        read_only: bool | None = None,
        execution_scope: str = "external",
        i18n_key: str = "",
        i18n_values: dict | Callable[[Translator], dict] | None = None,
        config_fingerprint: str | None = None,
    ):
        if read_only is not None:
            # Compatibility for extensions written before the 0.3 rename.
            side_effect = "read" if read_only else "write"
        if side_effect not in {"read", "write"}:
            raise ValueError("AgentTool.side_effect must be 'read' or 'write'")
        if execution_scope not in EXECUTION_SCOPES:
            raise ValueError(
                f"AgentTool.execution_scope must be one of {sorted(EXECUTION_SCOPES)}"
            )
        self.name = name
        self.description = description
        self.parameters = parameters
        self.execute = execute
        self.label = label
        self.execution_scope = execution_scope
        self.side_effect = side_effect
        self.sequential = sequential
        self.prepare_arguments = prepare_arguments
        self.i18n_key = i18n_key
        self.i18n_values = {} if i18n_values is None else i18n_values
        self.config_fingerprint = config_fingerprint

    @property
    def read_only(self) -> bool:
        return self.side_effect == "read"

    def spec(self, translator: Translator | None = None) -> ToolSpec:
        tool = localize_tool(self, translator) if translator is not None else self
        return ToolSpec(tool.name, tool.description, tool.parameters)


@dataclass
class ToolCallEvent:
    call_id: str
    tool_name: str
    args: dict  # handlers may mutate in place
    tool: AgentTool | None
    agent: str
    cwd: Path
    signal: threading.Event | None = None
    origin: Any = None


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
    # Pure, optional view applied once before a result enters model history.
    project_tool_result: Callable[[ToolCall, ToolResult], list] | None = None
    # Optional upper-layer classifier mapping a returned error ToolResult to a diagnostic kind ("fatal" | "blocked" | None).
    classify_tool_error: Callable[[ToolResult, str | None], str | None] | None = None
    translator: Translator | None = field(default=None, kw_only=True)

    def t(self, key: str, /, **values: object) -> str:
        return (self.translator or Translator())(key, **values)


def image_result(data: str, mime: str, note: str = "") -> ToolResult:
    content: list = [TextContent(note)] if note else []
    content.append(ImageContent(data, mime))
    return ToolResult(content)
