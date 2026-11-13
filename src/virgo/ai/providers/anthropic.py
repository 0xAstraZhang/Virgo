"""Anthropic Messages API adapter (official SDK, beta streaming endpoint).

Request shape follows current guidance for the Claude 4.6+/5.x families:
- adaptive thinking, depth controlled by ``output_config.effort`` (never ``budget_tokens``)
- top-level automatic prompt caching, append-only history (see harness docs)
- ``eager_input_streaming`` on tools; arguments are validated locally by the agent loop
- server-side refusal fallbacks and thinking ``drop_block`` binding as safety nets
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from dataclasses import replace

from ..search import is_search_unsupported, is_web_unsupported
from ..stream import EventStream, StreamEvent
from ..types import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    RawContent,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from ..validate import parse_tool_arguments
from ..web import declarations
from ._http import ProviderError, run_cancellable

BETA_FALLBACK = "server-side-fallback-2026-07-01"
BETA_BINDING = "thinking-binding-controls-2026-08-01"
BETA_COMPACTION = "compact-2026-09-04"
BETA_OAUTH = ("claude-code-20250219", "oauth-2025-04-20")
# models that accept server-side refusal fallbacks
FALLBACK_MODELS = {
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-fable-5",
}
_STOP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "pause_turn",
    "compaction": "compaction",
    "tool_use": "tool_use",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "refusal": "refusal",
}
_OVERFLOW = re.compile(r"prompt is too long|too many tokens|context (window|length)|exceed\w* the", re.I)

_clients: dict = {}
_clients_lock = threading.Lock()


def _api_key(model: Model) -> str | None:
    env = model.api_key_env or ("ANTHROPIC_API_KEY" if model.provider == "anthropic" else None)
    return model.options.get("apiKey") or (os.environ.get(env) if env else None) or None


def _is_oauth(model: Model) -> bool:
    return model.options.get("authType") == "oauth" or "sk-ant-oat" in (_api_key(model) or "")


def _client(model: Model):
    import anthropic

    key = _api_key(model)
    if not key and model.provider != "anthropic":
        # The SDK discovers Anthropic environment/profile credentials when no
        # explicit key is supplied. Those must never authenticate another host.
        raise ValueError(f"No API credential for provider {model.provider!r}; use /login api.")
    base = model.base_url or os.environ.get("ANTHROPIC_BASE_URL") or None
    oauth = _is_oauth(model)
    headers = {}
    if oauth:
        headers.update({"accept": "application/json", "user-agent": "claude-cli/2.1.280", "x-app": "cli"})
    headers.update(model.options.get("headers") or {})
    cache_key = (key, base, oauth, tuple(sorted(headers.items())))
    with _clients_lock:
        if cache_key not in _clients:
            kwargs = {"max_retries": 0}  # the harness owns retries
            if oauth:
                kwargs["auth_token"] = key
            elif key:
                kwargs["api_key"] = key
            if headers:
                kwargs["default_headers"] = headers
            if base:
                kwargs["base_url"] = base
            _clients[cache_key] = anthropic.Anthropic(**kwargs)
        return _clients[cache_key]


# ---------------------------------------------------------------------------
# request conversion
# ---------------------------------------------------------------------------


def _user_blocks(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out = []
    for b in content:
        if isinstance(b, TextContent):
            if b.text:
                out.append({"type": "text", "text": b.text})
        elif isinstance(b, ImageContent):
            out.append(
                {"type": "image", "source": {"type": "base64", "media_type": b.mime_type, "data": b.data}}
            )
    return out


def convert_messages(messages: list) -> list[dict]:
    out: list[dict] = []

    def add(role: str, blocks: list):
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)  # merge consecutive same-role turns deterministically
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        if isinstance(m, UserMessage):
            add("user", _user_blocks(m.content))
        elif isinstance(m, ToolResultMessage):
            content = _user_blocks(m.content) or [{"type": "text", "text": "(no output)"}]
            add(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id,
                        "content": content,
                        "is_error": m.is_error,
                    }
                ],
            )
        elif isinstance(m, AssistantMessage):
            blocks = []
            for b in m.content:
                if isinstance(b, TextContent):
                    if b.text:
                        block = {"type": "text", "text": b.text}
                        if m.api == "anthropic" and b.citations:
                            block["citations"] = copy.deepcopy(b.citations)
                        blocks.append(block)
                elif isinstance(b, ThinkingContent):
                    if b.redacted:
                        blocks.append({"type": "redacted_thinking", "data": b.signature})
                    elif b.signature:
                        blocks.append({"type": "thinking", "thinking": b.thinking, "signature": b.signature})
                elif isinstance(b, ToolCall):
                    blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.arguments})
                elif isinstance(b, RawContent) and b.api == "anthropic":
                    blocks.append(b.data)
            add("assistant", blocks)
    return out


def build_params(model: Model, ctx: Context, opts: StreamOptions) -> dict:
    o = model.options
    oauth = _is_oauth(model)
    params: dict = {
        "model": model.id,
        "max_tokens": opts.max_tokens or min(model.max_tokens, 64_000),
        "messages": convert_messages(ctx.messages),
        "cache_control": {"type": "ephemeral"},
    }
    if ctx.system:
        params["system"] = [{"type": "text", "text": ctx.system}]
    if oauth:
        params["system"] = [
            {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
            *params.get("system", []),
        ]
    if ctx.tools:
        params["tools"] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
                "eager_input_streaming": True,
            }
            for t in ctx.tools
        ]
    for declaration in declarations(ctx):
        params.setdefault("tools", []).append(declaration)
    betas: list[str] = list(BETA_OAUTH) if oauth else []
    if any(
        isinstance(b, RawContent) and b.api == "anthropic" and b.data.get("type") == "compaction"
        for m in ctx.messages if isinstance(m, AssistantMessage)
        for b in m.content
    ):
        betas.append(BETA_COMPACTION)
    if model.reasoning:
        thinking: dict = {"type": "adaptive", "display": o.get("thinkingDisplay", "summarized")}
        if o.get("thinkingBinding", True):
            thinking["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
            betas.append(BETA_BINDING)
        params["thinking"] = thinking
        if opts.effort:
            params["output_config"] = {"effort": opts.effort}
    if o.get("fallbacks", True) and model.id in FALLBACK_MODELS:
        params["fallbacks"] = "default"
        betas.append(BETA_FALLBACK)
    if betas:
        params["betas"] = betas
    return params


def compact(
    model: Model, ctx: Context, opts: StreamOptions, *, focus: str | None = None
) -> AssistantMessage:
    """Summarize the submitted history into an unmodified, signed API block."""
    out = AssistantMessage(api="anthropic", provider=model.provider, model=model.id)
    try:
        params = build_params(model, replace(ctx, web_search=None, web_tools=[], web_offers=[]), opts)
        params["max_tokens"] = opts.max_tokens or min(model.max_tokens, 8000)
        betas = params.setdefault("betas", [])
        if BETA_COMPACTION not in betas:
            betas.append(BETA_COMPACTION)
        params["compaction"] = {"type": "summarize"}
        if focus:
            params["compaction"]["instructions"] = (
                "Summarize this conversation so the assistant can continue the work. "
                "Preserve the user's latest request, constraints and preferences, completed and ongoing work, "
                "blockers, key decisions, next steps, and exact paths, identifiers, commands and errors needed "
                "to continue. Update any earlier summary and omit obsolete details. "
                "Do not continue the conversation, answer its questions, or call tools; return only the summary. "
                f"Pay special attention to: {focus}"
            )
        response = run_cancellable(
            lambda: _client(model).beta.messages.create(**params, stream=False), opts.signal
        )
        data = response.model_dump(mode="json", exclude_none=True)
        usage = data.get("usage") or {}
        iterations = usage.get("iterations") or [usage]
        out.usage = Usage(
            input=sum(u.get("input_tokens", 0) or 0 for u in iterations),
            output=sum(u.get("output_tokens", 0) or 0 for u in iterations),
            cache_read=sum(u.get("cache_read_input_tokens", 0) or 0 for u in iterations),
            cache_write=sum(u.get("cache_creation_input_tokens", 0) or 0 for u in iterations),
        )
        out.stop_reason = _STOP.get(data.get("stop_reason"), "error")
        blocks = data.get("content") or []
        if (
            out.stop_reason == "compaction"
            and len(blocks) == 1
            and blocks[0].get("type") == "compaction"
            and isinstance(blocks[0].get("content"), str)
            and blocks[0]["content"].strip()
            and isinstance(blocks[0].get("signature"), str)
            and blocks[0]["signature"]
        ):
            out.content = [RawContent("anthropic", blocks[0])]
        elif out.stop_reason == "refusal":
            out.error_message = "The model declined the compaction request."
        elif out.stop_reason == "length":
            out.error_message = "Compaction reached the output token limit without a complete summary."
        else:
            out.stop_reason, out.error_kind = "error", "fatal"
            out.error_message = "The provider did not return a complete signed compaction block."
    except Exception as error:  # noqa: BLE001 - leave the caller's history intact on failure
        out.error_kind, out.error_message = classify_error(error)
        out.stop_reason = "aborted" if opts.signal is not None and opts.signal.is_set() else "error"
        key = _api_key(model)
        if key and out.error_message:
            out.error_message = out.error_message.replace(key, "[redacted]")
    return out


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


def classify_error(e: Exception) -> tuple[str, str]:
    import anthropic
    import httpx2  # the SDK's HTTP library

    if isinstance(e, ProviderError):
        return e.kind, str(e)
    status = getattr(e, "status_code", None)
    text = getattr(e, "message", None) or str(e) or type(e).__name__
    if status == 413 or (status == 400 and _OVERFLOW.search(text)):
        return "overflow", text
    # The SDK wraps connection failures of the request itself, but a connection that drops or
    # times out while the response is streamed surfaces as the HTTP library's own exception.
    dropped = isinstance(e, (anthropic.APIConnectionError, httpx2.TransportError))
    if dropped or status in (408, 409, 429) or (status and status >= 500):
        return "retryable", text
    return "fatal", text


def stream(model: Model, ctx: Context, opts: StreamOptions) -> EventStream:
    out = AssistantMessage(api="anthropic", provider=model.provider, model=model.id)
    return EventStream(_events(model, ctx, opts, out), out)


def _events(model: Model, ctx: Context, opts: StreamOptions, out: AssistantMessage):
    params = build_params(model, ctx, opts)
    yield StreamEvent("start", out)
    json_bufs: dict[int, str] = {}
    index_map: dict[int, int] = {}  # api block index -> out.content index
    resp = None
    completed = False
    try:
        if opts.signal is not None and opts.signal.is_set():
            out.stop_reason = "aborted"
            out.error_message = "aborted by user"
            yield StreamEvent("error", out)
            return
        resp = _client(model).beta.messages.create(**params, stream=True)
        for ev in resp:
            if opts.signal is not None and opts.signal.is_set():
                out.stop_reason = "aborted"
                out.error_message = "aborted by user"
                yield StreamEvent("error", out)
                return
            t = ev.type
            if t == "message_start":
                u = ev.message.usage
                out.usage = Usage(
                    input=u.input_tokens or 0,
                    output=u.output_tokens or 0,
                    cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                    cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
                )
            elif t == "content_block_start":
                b = ev.content_block
                if b.type == "text":
                    citations = [c.model_dump(mode="json", exclude_none=True) if hasattr(c, "model_dump") else copy.deepcopy(c)
                                 for c in getattr(b, "citations", None) or []]
                    out.content.append(TextContent(b.text or "", citations=citations))
                    kind = "text_start"
                elif b.type == "thinking":
                    out.content.append(ThinkingContent(b.thinking or "", signature=b.signature or None))
                    kind = "thinking_start"
                elif b.type == "redacted_thinking":
                    out.content.append(ThinkingContent("", signature=b.data, redacted=True))
                    kind = "thinking_start"
                elif b.type == "tool_use":
                    out.content.append(ToolCall(b.id, b.name, b.input or {}))
                    json_bufs[ev.index] = ""
                    kind = "toolcall_start"
                else:  # fallback / server blocks: echo back verbatim
                    out.content.append(RawContent("anthropic", b.model_dump(mode="json", exclude_none=True)))
                    kind = None
                    if b.type == "server_tool_use":
                        json_bufs[ev.index] = ""
                        kind = "native_tool_start"
                        out.native_started = True
                        if b.name == "web_search":
                            out.search_started = True
                    elif b.type in {"web_search_tool_result", "web_fetch_tool_result"}:
                        out.native_started = True
                        out.search_started |= b.type == "web_search_tool_result"
                        kind = "native_tool_start"
                index_map[ev.index] = len(out.content) - 1
                if kind:
                    yield StreamEvent(kind, out, index_map[ev.index])
            elif t == "content_block_delta":
                i = index_map.get(ev.index)
                if i is None:
                    continue
                d = ev.delta
                blk = out.content[i]
                if d.type == "text_delta":
                    blk.text += d.text
                    yield StreamEvent("text_delta", out, i, d.text)
                elif d.type == "thinking_delta":
                    blk.thinking += d.thinking
                    yield StreamEvent("thinking_delta", out, i, d.thinking)
                elif d.type == "signature_delta":
                    blk.signature = (blk.signature or "") + d.signature
                elif d.type == "input_json_delta":
                    json_bufs[ev.index] = json_bufs.get(ev.index, "") + d.partial_json
                    if isinstance(blk, ToolCall):
                        yield StreamEvent("toolcall_delta", out, i, d.partial_json)
                    elif isinstance(blk, RawContent) and blk.data.get("type") == "server_tool_use":
                        yield StreamEvent("native_tool_delta", out, i, d.partial_json)
                elif d.type == "citations_delta" and isinstance(blk, TextContent):
                    citation = d.citation
                    blk.citations.append(citation.model_dump(mode="json", exclude_none=True)
                                         if hasattr(citation, "model_dump") else copy.deepcopy(citation))
            elif t == "content_block_stop":
                i = index_map.get(ev.index)
                if i is None:
                    continue
                blk = out.content[i]
                if isinstance(blk, ToolCall):
                    raw = json_bufs.pop(ev.index, "")
                    if raw.strip():
                        blk.arguments = parse_tool_arguments(raw)
                    yield StreamEvent("toolcall_end", out, i)
                elif isinstance(blk, RawContent) and blk.data.get("type") == "server_tool_use":
                    raw = json_bufs.pop(ev.index, "")
                    if raw.strip():
                        # Server calls belong to the API history; never turn them into client calls.
                        blk.data["input"] = json.loads(raw)
                    yield StreamEvent("native_tool_end", out, i)
                elif isinstance(blk, RawContent) and blk.data.get("type") in {"web_search_tool_result", "web_fetch_tool_result"}:
                    yield StreamEvent("native_tool_end", out, i)
                elif isinstance(blk, TextContent):
                    yield StreamEvent("text_end", out, i)
                elif isinstance(blk, ThinkingContent):
                    yield StreamEvent("thinking_end", out, i)
            elif t == "message_delta":
                if ev.delta.stop_reason:
                    out.stop_reason = _STOP.get(ev.delta.stop_reason, "stop")
                    if ev.delta.stop_reason == "refusal":
                        sd = getattr(ev.delta, "stop_details", None)
                        cat = getattr(sd, "category", None) if sd else None
                        expl = getattr(sd, "explanation", None) if sd else None
                        out.error_message = (
                            "The model declined this request"
                            + (f" ({cat})" if cat else "")
                            + (f": {expl}" if expl else ".")
                        )
                if ev.usage is not None:
                    out.usage.output = ev.usage.output_tokens or out.usage.output
                    if getattr(ev.usage, "input_tokens", None):
                        out.usage.input = ev.usage.input_tokens
            elif t == "message_stop":
                completed = True
        if declarations(ctx) and not completed:
            raise ProviderError("Anthropic web tool stream ended before message_stop.", "retryable")
        if out.stop_reason == "refusal":
            yield StreamEvent("error", out)
        else:
            yield StreamEvent("done", out)
    except Exception as e:  # noqa: BLE001
        kind, text = classify_error(e)
        if ctx.web_search and is_search_unsupported(e):
            kind = "search_unsupported"
        if any(d.get("name") == "web_fetch" for d in declarations(ctx)) and is_web_unsupported(e, "fetch"):
            kind = "fetch_unsupported"
        out.stop_reason = "error"
        out.error_kind = kind
        out.error_message = text
        yield StreamEvent("error", out)
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
