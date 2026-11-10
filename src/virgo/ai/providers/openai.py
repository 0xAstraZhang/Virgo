"""OpenAI Chat Completions adapter (works with any OpenAI-compatible endpoint).

Requires the optional dependency: ``uv sync --extra openai``.
Provider options (settings.providers.<name>): ``sendReasoning`` (echo reasoning_content back),
``maxTokensField`` ("max_tokens" | "max_completion_tokens").
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
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

_STOP = {"stop": "stop", "tool_calls": "tool_use", "function_call": "tool_use", "length": "length"}
_OVERFLOW = re.compile(
    r"context.length|maximum context|too many tokens|context_length_exceeded|prompt is too long", re.I
)
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

_clients: dict = {}
_clients_lock = threading.Lock()


def _client(model: Model):
    import openai

    env = model.api_key_env or ("OPENAI_API_KEY" if model.provider == "openai" else None)
    key = model.options.get("apiKey") or (os.environ.get(env) if env else None) or "not-set"
    base = model.base_url or (os.environ.get("OPENAI_BASE_URL") if model.provider == "openai" else None)
    headers = dict(model.options.get("headers") or {})
    ck = (key, base, tuple(sorted(headers.items())))
    with _clients_lock:
        if ck not in _clients:
            kwargs = {"api_key": key, "max_retries": 0}
            if headers:
                kwargs["default_headers"] = headers
            if base:
                kwargs["base_url"] = base
            _clients[ck] = openai.OpenAI(**kwargs)
        return _clients[ck]


def convert_messages(system: str, messages: list, send_reasoning: bool = False) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if isinstance(m, UserMessage):
            if isinstance(m.content, str):
                out.append({"role": "user", "content": m.content})
            else:
                parts = []
                for b in m.content:
                    if isinstance(b, TextContent):
                        parts.append({"type": "text", "text": b.text})
                    elif isinstance(b, ImageContent):
                        parts.append(
                            {"type": "image_url", "image_url": {"url": f"data:{b.mime_type};base64,{b.data}"}}
                        )
                out.append({"role": "user", "content": parts})
        elif isinstance(m, AssistantMessage):
            text, calls = m.text(), m.tool_calls()
            if not text and not calls:
                # Chat Completions rejects an assistant message with neither content nor tool calls,
                # e.g. a turn whose output budget ran out while the model was still reasoning.
                continue
            msg: dict = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in calls
                ]
            if send_reasoning:
                reasoning = "".join(b.thinking for b in m.content if isinstance(b, ThinkingContent))
                # Thinking-mode endpoints require the field on every tool-call turn, including turns
                # whose reasoning is unavailable (taken over from another provider, or never produced).
                if reasoning or calls:
                    msg["reasoning_content"] = reasoning
            out.append(msg)
        elif isinstance(m, ToolResultMessage):
            text = m.text()
            if any(isinstance(b, ImageContent) for b in m.content):
                text += "\n[image output omitted]"
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": text or "(no output)"})
    return out


def _reasoning_effort(model: Model, effort: str, fallback: str | None) -> str | None:
    if effort in (model.supported_reasoning_levels or ()) or effort == model.default_reasoning_level:
        return effort
    return fallback


def build_params(model: Model, ctx: Context, opts: StreamOptions) -> dict:
    o = model.options
    params: dict = {
        "model": model.id,
        "messages": convert_messages(ctx.system, ctx.messages, o.get("sendReasoning", False)),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    field = o.get("maxTokensField") or (
        "max_completion_tokens" if model.provider == "openai" else "max_tokens"
    )
    params[field] = opts.max_tokens or model.max_tokens
    if ctx.tools:
        params["tools"] = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in ctx.tools
        ]
    if model.reasoning and opts.effort:
        if "reasoningEffortMap" in o:
            effort = o["reasoningEffortMap"].get(opts.effort)
        else:
            effort = _reasoning_effort(model, opts.effort, _EFFORT.get(opts.effort))
        if effort is not None:
            params["reasoning_effort"] = effort
    return params


def classify_error(e: Exception) -> tuple[str, str]:
    import openai

    status = getattr(e, "status_code", None)
    text = getattr(e, "message", None) or str(e)
    if status in (400, 413) and _OVERFLOW.search(text):
        return "overflow", text
    if isinstance(e, openai.APIConnectionError) or status in (408, 409, 429) or (status and status >= 500):
        return "retryable", text
    return "fatal", text


def stream(model: Model, ctx: Context, opts: StreamOptions) -> EventStream:
    out = AssistantMessage(api="openai", provider=model.provider, model=model.id)
    return EventStream(_events(model, ctx, opts, out), out)


def _events(model: Model, ctx: Context, opts: StreamOptions, out: AssistantMessage):
    yield StreamEvent("start", out)
    text_i: int | None = None
    think_i: int | None = None
    calls: dict[int, tuple[int, str]] = {}  # openai index -> (content index, json buffer)
    finish = None
    resp = None
    try:
        if opts.signal is not None and opts.signal.is_set():
            out.stop_reason = "aborted"
            out.error_message = "aborted by user"
            yield StreamEvent("error", out)
            return
        resp = _client(model).chat.completions.create(**build_params(model, ctx, opts))
        for chunk in resp:
            if opts.signal is not None and opts.signal.is_set():
                out.stop_reason = "aborted"
                out.error_message = "aborted by user"
                yield StreamEvent("error", out)
                return
            if getattr(chunk, "usage", None):
                u = chunk.usage
                cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
                out.usage = Usage(
                    input=(u.prompt_tokens or 0) - cached, output=u.completion_tokens or 0, cache_read=cached
                )
            if not chunk.choices:
                continue
            ch = chunk.choices[0]
            d = ch.delta
            reasoning = getattr(d, "reasoning_content", None) or getattr(d, "reasoning", None)
            if reasoning:
                if think_i is None:
                    out.content.append(ThinkingContent(""))
                    think_i = len(out.content) - 1
                    yield StreamEvent("thinking_start", out, think_i)
                out.content[think_i].thinking += reasoning
                yield StreamEvent("thinking_delta", out, think_i, reasoning)
            if d.content:
                if text_i is None:
                    out.content.append(TextContent(""))
                    text_i = len(out.content) - 1
                    yield StreamEvent("text_start", out, text_i)
                out.content[text_i].text += d.content
                yield StreamEvent("text_delta", out, text_i, d.content)
            for tc in d.tool_calls or []:
                if tc.index not in calls:
                    out.content.append(
                        ToolCall(tc.id or f"call_{tc.index}", (tc.function and tc.function.name) or "", {})
                    )
                    calls[tc.index] = (len(out.content) - 1, "")
                    yield StreamEvent("toolcall_start", out, calls[tc.index][0])
                ci, buf = calls[tc.index]
                if tc.function and tc.function.name and not out.content[ci].name:
                    out.content[ci].name = tc.function.name
                piece = (tc.function and tc.function.arguments) or ""
                calls[tc.index] = (ci, buf + piece)
                if piece:
                    yield StreamEvent("toolcall_delta", out, ci, piece)
            if ch.finish_reason:
                finish = ch.finish_reason
        for ci, buf in calls.values():
            try:
                out.content[ci].arguments = json.loads(buf) if buf.strip() else {}
            except json.JSONDecodeError:
                out.content[ci].arguments = {"__invalid_json__": buf}
            yield StreamEvent("toolcall_end", out, ci)
        if text_i is not None:
            yield StreamEvent("text_end", out, text_i)
        if finish == "content_filter":
            out.stop_reason = "refusal"
            out.error_message = "The response was blocked by the provider's content filter."
            yield StreamEvent("error", out)
            return
        out.stop_reason = _STOP.get(finish or "stop", "stop")
        if calls and out.stop_reason == "stop":
            out.stop_reason = "tool_use"
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
