"""Anthropic Messages API adapter (official SDK, beta streaming endpoint).

Request shape follows current guidance for the Claude 4.6+/5.x families:
- adaptive thinking, depth controlled by ``output_config.effort`` (never ``budget_tokens``)
- top-level automatic prompt caching, append-only history (see harness docs)
- ``eager_input_streaming`` on tools; arguments are validated locally by the agent loop
- server-side refusal fallbacks and thinking ``drop_block`` binding as safety nets
"""

from __future__ import annotations

import json
import os
import re
import threading

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

BETA_FALLBACK = "server-side-fallback-2026-07-01"
BETA_BINDING = "thinking-binding-controls-2026-08-01"
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
    "pause_turn": "stop",
    "compaction": "stop",
    "tool_use": "tool_use",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "refusal": "refusal",
}
_OVERFLOW = re.compile(r"prompt is too long|too many tokens|context (window|length)|exceed\w* the", re.I)

_clients: dict = {}
_clients_lock = threading.Lock()


def _client(model: Model):
    import anthropic

    key = os.environ.get(model.api_key_env or "ANTHROPIC_API_KEY") or None
    base = model.base_url or os.environ.get("ANTHROPIC_BASE_URL") or None
    cache_key = (key, base)
    with _clients_lock:
        if cache_key not in _clients:
            kwargs = {"max_retries": 0}  # the harness owns retries
            if key:
                kwargs["api_key"] = key
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
                        blocks.append({"type": "text", "text": b.text})
                elif isinstance(b, ThinkingContent):
                    if b.redacted:
                        blocks.append({"type": "redacted_thinking", "data": b.signature})
                    elif b.signature:
                        blocks.append({"type": "thinking", "thinking": b.thinking, "signature": b.signature})
                elif isinstance(b, ToolCall):
                    blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.arguments})
                elif isinstance(b, RawContent):
                    blocks.append(b.data)
            add("assistant", blocks)
    return out


def build_params(model: Model, ctx: Context, opts: StreamOptions) -> dict:
    o = model.options
    params: dict = {
        "model": model.id,
        "max_tokens": opts.max_tokens or min(model.max_tokens, 64_000),
        "messages": convert_messages(ctx.messages),
        "cache_control": {"type": "ephemeral"},
    }
    if ctx.system:
        params["system"] = [{"type": "text", "text": ctx.system}]
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
    betas: list[str] = []
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


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


def classify_error(e: Exception) -> tuple[str, str]:
    import anthropic

    status = getattr(e, "status_code", None)
    text = getattr(e, "message", None) or str(e)
    if status == 413 or (status == 400 and _OVERFLOW.search(text)):
        return "overflow", text
    if isinstance(e, anthropic.APIConnectionError) or status in (408, 409, 429) or (status and status >= 500):
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
    try:
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
                    out.content.append(TextContent(b.text or ""))
                    kind = "text_start"
                elif b.type == "thinking":
                    out.content.append(ThinkingContent(b.thinking or "", signature=b.signature or None))
                    kind = "thinking_start"
                elif b.type == "redacted_thinking":
                    out.content.append(ThinkingContent("", signature=b.data, redacted=True))
                    kind = "thinking_start"
                elif b.type == "tool_use":
                    out.content.append(ToolCall(b.id, b.name, {}))
                    json_bufs[ev.index] = ""
                    kind = "toolcall_start"
                else:  # fallback / server blocks: echo back verbatim
                    out.content.append(RawContent("anthropic", b.model_dump(mode="json", exclude_none=True)))
                    kind = None
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
                    yield StreamEvent("toolcall_delta", out, i, d.partial_json)
            elif t == "content_block_stop":
                i = index_map.get(ev.index)
                if i is None:
                    continue
                blk = out.content[i]
                if isinstance(blk, ToolCall):
                    raw = json_bufs.pop(ev.index, "")
                    try:
                        blk.arguments = json.loads(raw) if raw.strip() else {}
                    except json.JSONDecodeError:
                        blk.arguments = {"__invalid_json__": raw}
                    yield StreamEvent("toolcall_end", out, i)
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
        if out.stop_reason == "refusal":
            yield StreamEvent("error", out)
        else:
            yield StreamEvent("done", out)
    except Exception as e:  # noqa: BLE001
        kind, text = classify_error(e)
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
