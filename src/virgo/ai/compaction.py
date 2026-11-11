"""Native compaction routing, separate from compatible third-party APIs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from urllib.parse import urlsplit

from .models import base_url
from .stream import error_message
from .transform import normalize
from .types import AssistantMessage, Context, Model, RawContent, StreamOptions


def native_api(model: Model) -> str | None:
    host = urlsplit(base_url(model)).hostname
    if model.api == "anthropic" and (model.provider == "anthropic" or host == "api.anthropic.com"):
        return "anthropic"
    if model.api == "openai-codex":
        return "openai-codex"
    if model.api in ("openai", "openai-responses") and (
        model.provider == "openai" or host == "api.openai.com"
    ):
        return "openai-responses"
    return None


def is_native_context(message) -> bool:
    return isinstance(message, AssistantMessage) and message.stop_reason == "compaction" and any(
        isinstance(block, RawContent) and block.data.get("type") == "compaction"
        for block in message.content
    )


def compatible_context(model: Model, messages: list) -> bool:
    return all(
        message.api == native_api(model) and message.provider == model.provider
        for message in messages if is_native_context(message)
    )


def context_model(model: Model, messages: list) -> Model:
    """Chat Completions cannot replay the state returned by Responses compaction."""
    if not compatible_context(model, messages):
        raise ValueError("Native compacted context belongs to a different provider; restore the original history first.")
    if model.api == "openai" and any(is_native_context(message) for message in messages):
        return replace(model, api="openai-responses")
    return model


def context_binding(model: Model, context: Context) -> str:
    data = {"model": model.spec, "system": context.system, "tools": [asdict(tool) for tool in context.tools]}
    if context.web_search:
        data["web_search"] = context.web_search
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def compact(model: Model, context: Context, options: StreamOptions | None = None, *, focus=None) -> AssistantMessage:
    options = options or StreamOptions()
    try:
        from .auth import authenticated_model

        api = native_api(model)
        if api is None:
            raise ValueError(f"No native compaction for provider {model.provider!r}.")
        model = replace(authenticated_model(model, options.signal), api=api)
        context_model(model, context.messages)
        context = Context(context.system, normalize(context.messages, model), context.tools)
        if api == "anthropic":
            from .providers import anthropic as impl
        elif api == "openai-codex":
            from .providers import codex as impl
        else:
            from .providers import responses as impl
        return impl.compact(model, context, options, focus=focus)
    except Exception as exc:  # provider setup failures follow the normal message contract
        text = str(exc)
        if key := model.options.get("apiKey"):
            text = text.replace(key, "[redacted]")
        result = error_message(model.api, model.provider, model.id, text)
        if options.signal is not None and options.signal.is_set():
            result.stop_reason = "aborted"
        return result
