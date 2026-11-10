"""Unified LLM layer (≈ pi-ai): one streaming interface over several providers."""

from __future__ import annotations

from .models import resolve_model
from .stream import EventStream, StreamEvent, error_message
from .transform import normalize
from .types import (
    AssistantMessage,
    Context,
    ImageContent,
    Message,
    Model,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    Usage,
    UserMessage,
)


def stream(model: Model, context: Context, options: StreamOptions | None = None) -> EventStream:
    """Stream one assistant message. Never raises: failures arrive as an `error` event."""
    options = options or StreamOptions()
    try:
        from .auth import authenticated_model

        model = authenticated_model(model, options.signal)
        context = Context(system=context.system, messages=normalize(context.messages, model), tools=context.tools)
        if model.api == "anthropic":
            from .providers import anthropic as impl
        elif model.api == "openai":
            from .providers import openai as impl
        elif model.api == "openai-codex":
            from .providers import codex as impl
        elif model.api == "openai-responses":
            from .providers import responses as impl
        elif model.api == "google-gemini-cli":
            from .providers import google as impl
        elif model.api == "faux":
            from .providers import faux as impl
        else:
            raise ValueError(f"unsupported api {model.api!r}")
    except Exception as e:  # noqa: BLE001 - e.g. optional dependency missing
        msg = error_message(model.api, model.provider, model.id, f"{type(e).__name__}: {e}")
        if options.signal is not None and options.signal.is_set():
            msg.stop_reason = "aborted"
            msg.error_message = "aborted by user"
        return EventStream(iter([StreamEvent("error", msg)]), msg)
    return impl.stream(model, context, options)


def complete(
    model: Model,
    system: str,
    prompt: str,
    *,
    max_tokens: int | None = None,
    effort: str | None = "low",
    signal=None,
) -> AssistantMessage:
    """One-shot, tool-free completion (used for summaries, evaluators, workflows)."""
    ctx = Context(system=system, messages=[UserMessage(prompt)], tools=[])
    return stream(model, ctx, StreamOptions(signal=signal, max_tokens=max_tokens, effort=effort)).result()


__all__ = [
    "AssistantMessage",
    "Context",
    "EventStream",
    "ImageContent",
    "Message",
    "Model",
    "StreamEvent",
    "StreamOptions",
    "TextContent",
    "ThinkingContent",
    "ToolCall",
    "ToolResultMessage",
    "ToolSpec",
    "Usage",
    "UserMessage",
    "complete",
    "resolve_model",
    "stream",
]
