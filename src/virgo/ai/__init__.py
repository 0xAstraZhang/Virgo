"""Unified LLM layer: one streaming interface over several providers."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import replace

from ..diagnostics import capture_error, safe_text
from .models import resolve_model
from .observed_usage import empty_usage
from .providers.reasoning import resolved_effort
from .stream import EventStream, StreamEvent, aborted_event, error_message
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
    if options.signal is not None and options.signal.is_set():
        fallback = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
        return EventStream(iter([aborted_event(fallback)]), fallback)
    try:
        from .auth import authenticated_model

        model = authenticated_model(model, options.signal)
        if context.web_tools and model.api == "openai":
            model = replace(model, api="openai-responses")
        context = replace(context, messages=normalize(context.messages, model))
        if model.api == "anthropic":
            from .providers import anthropic as impl
        elif model.api == "openai":
            from .providers import openai as impl
        elif model.api == "openai-codex":
            from .providers import codex as impl
        elif model.api == "openai-responses":
            from .providers import responses as impl
        elif model.api == "faux":
            from .providers import faux as impl
        else:
            raise ValueError(f"unsupported api {model.api!r}")
        if options.before_dispatch is not None:
            options.before_dispatch(model, options)
            if options.signal is not None and options.signal.is_set():
                fallback = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
                return EventStream(iter([aborted_event(fallback)]), fallback)
        observer = options.request_observer
        started = None
        request_id = uuid.uuid4().hex if observer is not None else None

        def dispatch():
            nonlocal started
            if started is not None:
                return
            started = time.monotonic()
            observer({
                "id": request_id, "phase": "start", "api": model.api,
                "provider": model.provider, "model": model.id,
                "effort": resolved_effort(model, options.effort).wire,
                "billing": "account" if model.api == "openai-codex" or
                model.options.get("authType") == "oauth" else "api",
                "usage_source": "synthetic" if model.api == "faux" else "provider",
            })

        if observer is not None:
            options = replace(options, on_dispatch=dispatch)
        result = impl.stream(model, context, options)
    except Exception as e:  # noqa: BLE001 - e.g. optional dependency missing
        msg = AssistantMessage(api=model.api, provider=model.provider, model=model.id, stop_reason="error")
        if options.signal is not None and options.signal.is_set():
            return EventStream(iter([aborted_event(msg)]), msg)
        secrets = (model.options.get("apiKey"), model.options.get("token"),
                   os.environ.get(model.api_key_env or ""))
        msg.error_kind = "fatal"
        msg.error_details = capture_error(
            e, phase="model.request", context={"api": model.api, "provider": model.provider, "model": model.id},
            secrets=secrets,
        )
        msg.error_message = safe_text(f"{type(e).__name__}: {msg.error_details['message']}", secrets=secrets)
        return EventStream(iter([StreamEvent("error", msg)]), msg)

    def events():
        from .web import add_source_links

        terminal = last = None
        try:
            for event in result:
                last = event.partial
                if event.type in ("done", "error"):
                    terminal = event.partial
                    count = len(event.partial.content)
                    add_source_links(event.partial)
                    for i in range(count, len(event.partial.content)):
                        block = event.partial.content[i]
                        yield StreamEvent("text_start", event.partial, i)
                        yield StreamEvent("text_delta", event.partial, i, block.text)
                        yield StreamEvent("text_end", event.partial, i)
                    if options.signal is not None and options.signal.is_set():
                        yield aborted_event(event.partial)
                        return
                yield event
        finally:
            try:
                result.close()
            finally:
                if observer is not None and started is not None:
                    observed = terminal if terminal is not None else last
                    observer({
                        "id": request_id, "phase": "end",
                        "duration_s": round(time.monotonic() - started, 6),
                        "stop_reason": terminal.stop_reason if terminal is not None else "incomplete",
                        "usage": getattr(observed, "reported_usage", None) or empty_usage(),
                        "provider_reported_cost_usd": None,
                    })

    return EventStream(events(), AssistantMessage(api=model.api, provider=model.provider, model=model.id))


REASONING_HEADROOM = 4096


def completion_token_limit(model: Model, max_tokens: int | None) -> int | None:
    """Resolve a visible-answer allowance with reasoning headroom."""
    if max_tokens is not None:
        return max(max_tokens, min(max_tokens + REASONING_HEADROOM, model.max_tokens))
    return None


def completion_request_metadata(model: Model, effort: str | None, limit: int | None) -> dict:
    """Describe adapter request controls, without recording credentials or prompt contents."""
    return {"effective_effort": resolved_effort(model, effort).wire, **output_limit_metadata(model, limit)}


def output_limit_metadata(model: Model, limit: int | None) -> dict:
    supported = model.api != "openai-codex"
    actual_limit = limit or model.max_tokens
    if model.api == "anthropic" and limit is None:
        actual_limit = min(model.max_tokens, 64_000)
    elif model.api == "openai-responses":
        actual_limit = max(16, actual_limit)
    return {
        "requested_max_tokens": limit,
        "max_tokens": actual_limit if supported else None,
        "token_limit_supported": supported,
        "token_limit_omission_reason": None if supported else "account adapter does not send an output token limit",
    }


def complete(
    model: Model,
    system: str,
    prompt: str,
    *,
    max_tokens: int | None = None,
    effort: str | None = "low",
    signal=None,
    request_observer=None,
) -> AssistantMessage:
    """One-shot, tool-free completion (used for compaction summaries).

    ``max_tokens`` budgets the visible answer. Providers count hidden reasoning against the same
    limit, so the request allows ``REASONING_HEADROOM`` more (within the model's own maximum);
    otherwise a small budget can be spent entirely on thinking and return no answer.
    """
    limit = completion_token_limit(model, max_tokens)
    ctx = Context(system=system, messages=[UserMessage(prompt)], tools=[])
    message = stream(model, ctx, StreamOptions(signal=signal, max_tokens=limit, effort=effort,
                                               request_observer=request_observer)).result()
    # Internal request metadata is deliberately not a dataclass field or model-visible content.
    message.request_metadata = completion_request_metadata(model, effort, limit)
    return message


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
    "error_message",
    "resolve_model",
    "resolved_effort",
    "stream",
]
