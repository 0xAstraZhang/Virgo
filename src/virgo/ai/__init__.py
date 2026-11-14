"""Unified LLM layer (≈ pi-ai): one streaming interface over several providers."""

from __future__ import annotations

import os
from dataclasses import replace

from ..diagnostics import capture_error, safe_text
from .compaction import compact as native_compact
from .compaction import context_model
from .models import resolve_model
from .providers.reasoning import chat_effort, responses_effort
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
        if (context.web_tools or context.web_search) and model.api == "openai":
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
        elif model.api == "google-gemini-cli":
            from .providers import google as impl
        elif model.api == "faux":
            from .providers import faux as impl
        else:
            raise ValueError(f"unsupported api {model.api!r}")
        result = impl.stream(model, context, options)
    except Exception as e:  # noqa: BLE001 - e.g. optional dependency missing
        msg = AssistantMessage(api=model.api, provider=model.provider, model=model.id, stop_reason="error")
        if options.signal is not None and options.signal.is_set():
            msg.stop_reason = "aborted"
            msg.error_message = "aborted by user"
        else:
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


def completion_token_limit(model: Model, max_tokens: int | None, total_max_tokens: int | None) -> int | None:
    """Resolve a visible-answer allowance or an explicit total output ceiling."""
    if total_max_tokens is not None:
        if max_tokens is not None:
            raise ValueError("max_tokens and total_max_tokens are mutually exclusive")
        if type(total_max_tokens) is not int or total_max_tokens <= 0:
            raise ValueError("total_max_tokens must be a positive integer")
        return min(total_max_tokens, model.max_tokens)
    if max_tokens is not None:
        return max(max_tokens, min(max_tokens + REASONING_HEADROOM, model.max_tokens))
    return None


def completion_request_metadata(model: Model, effort: str | None, limit: int | None) -> dict:
    """Describe adapter request controls, without recording credentials or prompt contents."""
    effective = effort if model.reasoning else None
    if effective and model.api == "openai":
        effective = chat_effort(model, effective)
    elif effective and model.api in {"openai-responses", "openai-codex"}:
        effective = responses_effort(model, effective)
    elif effective and model.api == "google-gemini-cli":
        # The adapter sends a thinking level for Gemini 3, a token budget for older models.
        effective = "low" if effective == "low" else (
            "medium" if effective == "medium" and ("gemini-3" not in model.id or "pro" not in model.id) else "high"
        )
    supported = model.api != "openai-codex"
    actual_limit = limit or model.max_tokens
    if model.api == "anthropic" and limit is None:
        actual_limit = min(model.max_tokens, 64_000)
    elif model.api == "openai-responses":
        actual_limit = max(16, actual_limit)
    return {
        "effective_effort": effective,
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
    total_max_tokens: int | None = None,
    effort: str | None = "low",
    signal=None,
) -> AssistantMessage:
    """One-shot, tool-free completion (used for summaries, evaluators, workflows).

    ``max_tokens`` budgets the visible answer. Providers count hidden reasoning against the same
    limit, so the request allows ``REASONING_HEADROOM`` more (within the model's own maximum);
    otherwise a small budget can be spent entirely on thinking and return no answer.
    ``total_max_tokens`` instead supplies the total limit, with no added reasoning allowance.
    The two budget forms are mutually exclusive.
    """
    limit = completion_token_limit(model, max_tokens, total_max_tokens)
    ctx = Context(system=system, messages=[UserMessage(prompt)], tools=[])
    message = stream(model, ctx, StreamOptions(signal=signal, max_tokens=limit, effort=effort)).result()
    # Internal request metadata is deliberately not a dataclass field or model-visible content.
    message.request_metadata = completion_request_metadata(model, effort, limit)
    if total_max_tokens is not None:
        message.request_metadata["requested_max_tokens"] = total_max_tokens
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
    "native_compact",
    "resolve_model",
    "stream",
]
