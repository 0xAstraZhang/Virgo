"""ChatGPT account transport, following Pi's OpenAI Codex SSE adapter."""

from __future__ import annotations

import copy
from dataclasses import replace

from ..search import is_search_unsupported
from ..stream import EventStream, StreamEvent
from ..types import AssistantMessage, Context, Model, RawContent, StreamOptions
from . import responses
from ._http import ProviderError, cancelled, classify_error, json_events
from .openai import _reasoning_effort


def build_params(model: Model, ctx: Context, opts: StreamOptions) -> dict:
    params = {
        "model": model.id,
        "instructions": ctx.system or "You are a helpful assistant.",
        "input": responses.convert_messages(ctx.messages, api=model.api),
        "store": False,
        "stream": True,
        "text": {"verbosity": "low"},
        "include": ["reasoning.encrypted_content"],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    if ctx.tools:
        params["tools"] = responses.convert_tools(ctx.tools)
    if ctx.web_search:
        params.setdefault("tools", []).append(copy.deepcopy(ctx.web_search))
    if model.reasoning and opts.effort:
        params["reasoning"] = {
            "effort": _reasoning_effort(model, opts.effort, "xhigh" if opts.effort == "max" else opts.effort),
            "summary": "auto",
        }
    return params


def _endpoint(model: Model) -> str:
    base = (model.base_url or "https://chatgpt.com/backend-api").rstrip("/")
    if base.endswith("/codex/responses"):
        return base
    if base.endswith("/codex"):
        return base + "/responses"
    return base + "/codex/responses"


def _normalize_events(events):
    for event in events:
        # Some Codex deployments use response.done for the same terminal event.
        if event.get("type") == "response.done":
            event = {**event, "type": "response.completed"}
        yield event


def stream(model: Model, ctx: Context, opts: StreamOptions) -> EventStream:
    out = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
    return EventStream(_events(model, ctx, opts, out), out)


def compact(model: Model, ctx: Context, opts: StreamOptions, *, focus: str | None = None) -> AssistantMessage:
    """Use Codex's Responses compaction trigger, preserving user inputs.

    The account API uses its ordinary SSE endpoint rather than the public
    standalone compact endpoint. Its trigger is transient request control.
    """
    out = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
    try:
        if cancelled(opts.signal):
            raise ProviderError("Aborted by user.", "aborted")
        # Compaction is maintenance, never a request to run hosted search.
        params = build_params(model, replace(ctx, web_search=None), opts)
        retained = [copy.deepcopy(item) for item in params["input"] if item.get("role") == "user"]
        params["input"].append({"type": "compaction_trigger"})
        out = EventStream(_events(model, ctx, opts, out, params=params), out).result()
        if out.stop_reason in ("error", "aborted", "refusal"):
            return out
        blocks = [b.data for b in out.content if isinstance(b, RawContent) and b.data.get("type") == "compaction"]
        if (
            out.stop_reason != "stop"
            or len(blocks) != 1
            or not isinstance(blocks[0].get("encrypted_content"), str)
            or not blocks[0]["encrypted_content"]
        ):
            raise ProviderError("The provider did not complete exactly one encrypted compaction item.")
        out.content = [RawContent(model.api, item) for item in [*retained, copy.deepcopy(blocks[0])]]
        out.stop_reason = "compaction"
    except Exception as error:
        kind, message = classify_error(error, model.options.get("apiKey", ""))
        out.stop_reason = "aborted" if kind == "aborted" or cancelled(opts.signal) else "error"
        out.error_kind, out.error_message = kind, message
    return out


def _events(model: Model, ctx: Context, opts: StreamOptions, out: AssistantMessage, *, params: dict | None = None):
    token = model.options.get("apiKey", "")
    events = None
    try:
        if cancelled(opts.signal):
            raise ProviderError("Aborted by user.", "aborted")
        if not token:
            raise ProviderError("Sign in with /login auth openai before using this model.")
        account_id = model.options.get("authMetadata", {}).get("accountId")
        if not account_id:
            raise ProviderError("The OpenAI login has no account ID. Sign in again with /login auth openai.")
        headers = {
            "originator": "virgo",
            "OpenAI-Beta": "responses=experimental",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            **model.options.get("headers", {}),
            "Authorization": f"Bearer {token}",
            "chatgpt-account-id": account_id,
        }
        events = json_events(
            _endpoint(model), headers, params if params is not None else build_params(model, ctx, opts), opts.signal
        )
        for event in responses.parse_events(
            _normalize_events(events), model, out, opts,
            web_search=bool(ctx.web_search) and params is None,
        ):
            if out.error_message:
                out.error_message = out.error_message.replace(token, "[redacted]")
            if event.type in ("done", "error"):
                events.close()
            yield event
    except Exception as error:
        if events is not None:
            events.close()
        kind, message = classify_error(error, token)
        if ctx.web_search and params is None and is_search_unsupported(error):
            kind = "search_unsupported"
        out.stop_reason = "aborted" if kind == "aborted" or cancelled(opts.signal) else "error"
        out.error_kind = kind
        out.error_message = message
        yield StreamEvent("error", out)
    finally:
        if events is not None:
            events.close()
