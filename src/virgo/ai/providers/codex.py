"""ChatGPT account transport using the OpenAI Codex SSE protocol."""

from __future__ import annotations

from ..search import is_search_unsupported
from ..stream import EventStream, StreamEvent
from ..types import AssistantMessage, Context, Model, StreamOptions
from ..web import declarations
from . import responses
from ._errors import attach_error
from ._http import ProviderError, cancelled, classify_error, json_events
from .reasoning import responses_effort


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
    if opts.prompt_cache_key:
        params["prompt_cache_key"] = opts.prompt_cache_key
    if ctx.tools:
        params["tools"] = responses.convert_tools(ctx.tools)
    for tool in declarations(ctx):
        params.setdefault("tools", []).append(tool)
    if model.reasoning and opts.effort:
        params["reasoning"] = {
            "effort": responses_effort(model, opts.effort),
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
        if opts.prompt_cache_key:
            headers.setdefault("session-id", opts.prompt_cache_key)
            headers.setdefault("thread-id", opts.prompt_cache_key)
        events = json_events(
            _endpoint(model), headers, params if params is not None else build_params(model, ctx, opts), opts.signal,
            io_bridge=opts.io_bridge, codex_session=opts.prompt_cache_key or "",
        )
        for event in responses.parse_events(
            _normalize_events(events), model, out, opts,
            web_search=bool(ctx.web_tools) and params is None,
        ):
            if out.error_message:
                attach_error(out, None, model, token=token)
            if event.type in ("done", "error"):
                events.close()
            yield event
    except Exception as error:
        if events is not None:
            events.close()
        kind, message = classify_error(error, token)
        if ctx.web_tools and params is None and is_search_unsupported(error):
            kind = "search_unsupported"
        out.stop_reason = "aborted" if kind == "aborted" or cancelled(opts.signal) else "error"
        out.error_kind = kind
        out.error_message = message
        attach_error(out, error, model, token=token)
        yield StreamEvent("error", out)
    finally:
        if events is not None:
            events.close()
