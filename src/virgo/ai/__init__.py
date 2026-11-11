"""Unified LLM layer (≈ pi-ai): one streaming interface over several providers."""

from __future__ import annotations

from dataclasses import replace

from .compaction import compact as native_compact
from .compaction import context_model
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
        model = context_model(model, context.messages)
        if context.web_search and model.api == "openai":
            model = replace(model, api="openai-responses")
        context = Context(system=context.system, messages=normalize(context.messages, model),
                          tools=context.tools, web_search=context.web_search)
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
    result = impl.stream(model, context, options)

    def events():
        from .search import add_source_links

        for event in result:
            if event.type in ("done", "error"):
                count = len(event.partial.content)
                add_source_links(event.partial)
                for i in range(count, len(event.partial.content)):
                    block = event.partial.content[i]
                    yield StreamEvent("text_start", event.partial, i)
                    yield StreamEvent("text_delta", event.partial, i, block.text)
                    yield StreamEvent("text_end", event.partial, i)
            yield event

    return EventStream(events(), AssistantMessage(api=model.api, provider=model.provider, model=model.id))


REASONING_HEADROOM = 4096


def complete(
    model: Model,
    system: str,
    prompt: str,
    *,
    max_tokens: int | None = None,
    effort: str | None = "low",
    signal=None,
) -> AssistantMessage:
    """One-shot, tool-free completion (used for summaries, evaluators, workflows).

    ``max_tokens`` budgets the visible answer. Providers count hidden reasoning against the same
    limit, so the request allows ``REASONING_HEADROOM`` more (within the model's own maximum);
    otherwise a small budget can be spent entirely on thinking and return no answer.
    """
    if max_tokens is not None:
        max_tokens = max(max_tokens, min(max_tokens + REASONING_HEADROOM, model.max_tokens))
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
    "native_compact",
    "resolve_model",
    "stream",
]
