"""Provider-neutral message model (mirrors Pi's pi-ai types)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


@dataclass
class TextContent:
    text: str
    type: Literal["text"] = "text"
    citations: list[dict] = field(default_factory=list)
    native: dict | None = None  # original annotated message for same-provider replay


@dataclass
class ThinkingContent:
    thinking: str
    signature: str | None = None
    redacted: bool = False  # Anthropic redacted_thinking: payload lives in `signature`
    type: Literal["thinking"] = "thinking"


@dataclass
class ImageContent:
    data: str  # base64
    mime_type: str
    type: Literal["image"] = "image"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    type: Literal["tool_call"] = "tool_call"


@dataclass
class RawContent:
    """A provider-specific block that must be echoed back verbatim (e.g. Anthropic `fallback`)."""

    api: str
    data: dict
    type: Literal["raw"] = "raw"


Content = TextContent | ThinkingContent | ImageContent | ToolCall | RawContent

_BLOCKS = {
    "text": TextContent,
    "thinking": ThinkingContent,
    "image": ImageContent,
    "tool_call": ToolCall,
    "raw": RawContent,
}


def block_from_dict(d: dict) -> Content:
    cls = _BLOCKS[d["type"]]
    return cls(**{k: v for k, v in d.items() if k != "type"})


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


StopReason = Literal["stop", "length", "tool_use", "pause_turn", "refusal", "error", "aborted"]


@dataclass
class UserMessage:
    content: str | list
    kind: str | None = None  # internal tag: "summary", "notification", ... (never sent)
    timestamp: float = field(default_factory=time.time)
    role: Literal["user"] = "user"

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "\n".join(b.text for b in self.content if isinstance(b, TextContent))


@dataclass
class AssistantMessage:
    content: list = field(default_factory=list)
    api: str = ""
    provider: str = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "stop"
    error_message: str | None = None
    error_kind: str | None = None  # "retryable" | "overflow" | "search_unsupported" | "fatal"
    timestamp: float = field(default_factory=time.time)
    role: Literal["assistant"] = "assistant"
    search_started: bool = False  # server-side work may have incurred costs; do not blindly replay

    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextContent))

    def tool_calls(self) -> list[ToolCall]:
        return [b for b in self.content if isinstance(b, ToolCall)]


@dataclass
class ToolResultMessage:
    tool_call_id: str
    tool_name: str
    content: list
    details: Any = None
    is_error: bool = False
    timestamp: float = field(default_factory=time.time)
    role: Literal["toolResult"] = "toolResult"

    def text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextContent))


Message = UserMessage | AssistantMessage | ToolResultMessage


def message_to_dict(m: Message) -> dict:
    return asdict(m)


def message_from_dict(d: dict) -> Message:
    d = dict(d)
    role = d.get("role")
    content = d.get("content")
    if isinstance(content, list):
        d["content"] = [block_from_dict(b) for b in content]
    if role == "user":
        return UserMessage(**d)
    if role == "assistant":
        d["usage"] = Usage(**(d.get("usage") or {}))
        return AssistantMessage(**d)
    if role == "toolResult":
        return ToolResultMessage(**d)
    raise ValueError(f"unknown message role: {role!r}")


def text_of(content: str | list) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.text for b in content if isinstance(b, TextContent))


# ---------------------------------------------------------------------------
# Model / request context
# ---------------------------------------------------------------------------


@dataclass
class Model:
    provider: str  # settings key, e.g. "anthropic", "openai", "deepseek"
    id: str
    api: str  # "anthropic" | "openai" | "faux"
    base_url: str | None = None
    api_key_env: str | None = None
    context_window: int = 200_000
    max_tokens: int = 32_000
    reasoning: bool = False
    vision: bool = True
    options: dict = field(default_factory=dict)  # provider-specific knobs
    auth_path: str | None = field(default=None, repr=False)
    supported_reasoning_levels: tuple[str, ...] | None = None  # None: unknown; (): unsupported
    default_reasoning_level: str | None = None

    @property
    def spec(self) -> str:
        return f"{self.provider}/{self.id}"


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict


@dataclass
class Context:
    system: str
    messages: list
    tools: list[ToolSpec] = field(default_factory=list)
    web_search: dict | None = None  # provider-native declaration, separate from client functions


@dataclass
class StreamOptions:
    signal: Any = None  # threading.Event
    max_tokens: int | None = None
    effort: str | None = None
