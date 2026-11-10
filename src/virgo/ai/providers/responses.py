"""OpenAI Responses transport, including xAI account credentials.

The message and event conversion follows Pi's openai-responses-shared adapter.
Codex reuses it with its own request transport and parameters.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

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
from .openai import _client, classify_error


def convert_messages(messages: list, *, api: str = "openai-responses") -> list[dict]:
    result = []
    for message in messages:
        if isinstance(message, UserMessage):
            if isinstance(message.content, str):
                result.append({"role": "user", "content": message.content})
            else:
                content = []
                for block in message.content:
                    if isinstance(block, TextContent):
                        content.append({"type": "input_text", "text": block.text})
                    elif isinstance(block, ImageContent):
                        content.append(
                            {
                                "type": "input_image",
                                "image_url": f"data:{block.mime_type};base64,{block.data}",
                                "detail": "auto",
                            }
                        )
                if content:
                    result.append({"role": "user", "content": content})
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ThinkingContent) and message.api == api and block.signature:
                    try:
                        item = json.loads(block.signature)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(item, dict) and item.get("type") == "reasoning":
                        result.append(item)
                elif isinstance(block, TextContent) and block.text:
                    result.append({"role": "assistant", "content": block.text})
                elif isinstance(block, ToolCall):
                    call_id, _, item_id = block.id.partition("|")
                    item = {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": block.name,
                        "arguments": json.dumps(block.arguments),
                    }
                    if item_id and message.api == api:
                        item["id"] = item_id
                    result.append(item)
        elif isinstance(message, ToolResultMessage):
            output = message.text() or "(no output)"
            if any(isinstance(block, ImageContent) for block in message.content):
                output += "\n[image output omitted]"
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id.partition("|")[0],
                    "output": output,
                }
            )
    return result


def convert_tools(tools: list) -> list[dict]:
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "strict": False,
        }
        for tool in tools
    ]


def build_params(model: Model, ctx: Context, opts: StreamOptions) -> dict:
    params = {
        "model": model.id,
        "input": convert_messages(ctx.messages, api=model.api),
        "stream": True,
        "store": False,
        "max_output_tokens": max(16, opts.max_tokens or model.max_tokens),
    }
    if ctx.system:
        params["instructions"] = ctx.system
    if ctx.tools:
        params["tools"] = convert_tools(ctx.tools)
    if model.reasoning:
        params["include"] = ["reasoning.encrypted_content"]
        if opts.effort:
            params["reasoning"] = {
                "effort": "xhigh" if opts.effort == "max" else opts.effort,
                "summary": "auto",
            }
    return params


def _dict(value) -> dict:
    return value if isinstance(value, dict) else value.model_dump(mode="json", exclude_none=True)


def _arguments(raw: str) -> dict:
    try:
        parsed = json.loads(raw) if raw.strip() else {}
        return parsed if isinstance(parsed, dict) else {"__invalid_json__": raw}
    except (ValueError, TypeError):
        return {"__invalid_json__": raw}


def parse_events(events: Iterable, model: Model, out: AssistantMessage, opts: StreamOptions):
    """Convert SDK objects or decoded SSE dictionaries into Virgo stream events."""
    yield StreamEvent("start", out)
    slots: dict[int, int] = {}
    buffers: dict[int, str] = {}
    finished: set[int] = set()
    reasoning_by_id: dict[str, ThinkingContent] = {}
    refusal = False

    def start(index: int, item: dict):
        if index in slots:
            return
        kind = item.get("type")
        if kind == "message":
            block, event_kind = TextContent(""), "text_start"
        elif kind == "reasoning":
            block, event_kind = ThinkingContent(""), "thinking_start"
        elif kind == "function_call":
            call_id = item.get("call_id") or item.get("id") or f"call_{index}"
            item_id = item.get("id")
            block = ToolCall(f"{call_id}|{item_id}" if item_id else call_id, item.get("name", ""), {})
            buffers[index] = item.get("arguments") or ""
            event_kind = "toolcall_start"
        else:
            return
        slots[index] = len(out.content)
        out.content.append(block)
        yield StreamEvent(event_kind, out, slots[index])

    def finish(index: int, item: dict):
        nonlocal refusal
        yield from start(index, item)
        if index not in slots or index in finished:
            # Final response may contain the encryption omitted from item.done.
            if item.get("type") == "reasoning" and item.get("encrypted_content"):
                block = reasoning_by_id.get(item.get("id", ""))
                if block is not None:
                    block.signature = json.dumps(item)
            return
        ci = slots[index]
        block = out.content[ci]
        if isinstance(block, TextContent):
            parts = item.get("content")
            if parts:
                block.text = "".join(p.get("text") or p.get("refusal") or "" for p in parts)
                refusal = refusal or any(p.get("type") == "refusal" for p in parts)
            event_kind = "text_end"
        elif isinstance(block, ThinkingContent):
            parts = item.get("summary") or item.get("content") or []
            block.thinking = "\n\n".join(p.get("text", "") for p in parts) or block.thinking
            block.signature = json.dumps(item)
            if item.get("id"):
                reasoning_by_id[item["id"]] = block
            event_kind = "thinking_end"
        else:
            raw = item.get("arguments")
            block.arguments = _arguments(raw if isinstance(raw, str) else buffers.get(index, ""))
            block.name = item.get("name") or block.name
            event_kind = "toolcall_end"
        finished.add(index)
        yield StreamEvent(event_kind, out, ci)

    for raw in events:
        if opts.signal is not None and opts.signal.is_set():
            out.stop_reason, out.error_message = "aborted", "aborted by user"
            yield StreamEvent("error", out)
            return
        event = _dict(raw)
        kind = event.get("type", "")
        index = event.get("output_index", 0)
        if kind == "response.output_item.added":
            yield from start(index, event["item"])
        elif kind in ("response.output_text.delta", "response.refusal.delta"):
            yield from start(index, {"type": "message"})
            ci = slots[index]
            delta = event.get("delta", "")
            out.content[ci].text += delta
            refusal = refusal or kind == "response.refusal.delta"
            yield StreamEvent("text_delta", out, ci, delta)
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            yield from start(index, {"type": "reasoning"})
            ci = slots[index]
            delta = event.get("delta", "")
            out.content[ci].thinking += delta
            yield StreamEvent("thinking_delta", out, ci, delta)
        elif kind == "response.function_call_arguments.delta" and index in slots:
            delta = event.get("delta", "")
            buffers[index] = buffers.get(index, "") + delta
            yield StreamEvent("toolcall_delta", out, slots[index], delta)
        elif kind == "response.function_call_arguments.done" and index in slots:
            buffers[index] = event.get("arguments", buffers.get(index, ""))
        elif kind == "response.output_item.done":
            yield from finish(index, event["item"])
        elif kind in ("response.completed", "response.incomplete", "response.failed"):
            response = event.get("response") or {}
            for i, item in enumerate(response.get("output") or []):
                yield from finish(i, item)
            usage = response.get("usage") or {}
            details = usage.get("input_tokens_details") or {}
            cached, written = details.get("cached_tokens", 0) or 0, details.get("cache_write_tokens", 0) or 0
            out.usage = Usage(
                input=max(0, (usage.get("input_tokens", 0) or 0) - cached - written),
                output=usage.get("output_tokens", 0) or 0,
                cache_read=cached,
                cache_write=written,
            )
            status = response.get("status") or kind.removeprefix("response.")
            reason = (response.get("incomplete_details") or {}).get("reason")
            if status == "failed":
                out.stop_reason = "error"
                error = response.get("error") or {}
                out.error_message = error.get("message") or "The provider failed to complete the response."
                out.error_kind = (
                    "retryable" if error.get("code") in ("server_error", "rate_limit_exceeded") else "fatal"
                )
            elif refusal or reason == "content_filter":
                out.stop_reason = "refusal"
                out.error_message = "The provider declined this request."
            elif status == "incomplete":
                out.stop_reason = "length" if reason == "max_output_tokens" else "error"
                if out.stop_reason == "error":
                    out.error_kind = "fatal"
                    out.error_message = "The provider returned an incomplete response."
            else:
                unfinished = [
                    i for i in slots if i not in finished and isinstance(out.content[slots[i]], ToolCall)
                ]
                if unfinished:
                    out.stop_reason, out.error_kind = "error", "retryable"
                    out.error_message = "The provider ended before completing a tool call."
                else:
                    out.stop_reason = "tool_use" if out.tool_calls() else "stop"
            yield StreamEvent("error" if out.stop_reason in ("error", "refusal") else "done", out)
            return
        elif kind == "error":
            out.stop_reason = "error"
            out.error_kind = (
                "retryable" if event.get("code") in ("server_error", "rate_limit_exceeded") else "fatal"
            )
            out.error_message = event.get("message") or "The provider returned a stream error."
            yield StreamEvent("error", out)
            return
    if opts.signal is not None and opts.signal.is_set():
        out.stop_reason, out.error_message = "aborted", "aborted by user"
    else:
        out.stop_reason, out.error_kind = "error", "retryable"
        out.error_message = "Responses stream ended before the final response."
    yield StreamEvent("error", out)


def stream(model: Model, ctx: Context, opts: StreamOptions) -> EventStream:
    out = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
    return EventStream(_events(model, ctx, opts, out), out)


def _events(model: Model, ctx: Context, opts: StreamOptions, out: AssistantMessage):
    response = None
    try:
        if opts.signal is not None and opts.signal.is_set():
            out.stop_reason, out.error_message = "aborted", "aborted by user"
            yield StreamEvent("error", out)
            return
        response = _client(model).responses.create(**build_params(model, ctx, opts))
        for event in parse_events(response, model, out, opts):
            if out.error_message and model.options.get("apiKey"):
                out.error_message = out.error_message.replace(model.options["apiKey"], "[redacted]")
            yield event
    except Exception as exc:  # noqa: BLE001 - provider failures are data
        out.error_kind, out.error_message = classify_error(exc)
        if model.options.get("apiKey"):
            out.error_message = out.error_message.replace(model.options["apiKey"], "[redacted]")
        out.stop_reason = "aborted" if opts.signal is not None and opts.signal.is_set() else "error"
        yield StreamEvent("error", out)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - cleanup must not replace the result
                pass
