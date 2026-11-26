"""OpenAI Responses transport, including xAI account credentials.

Codex reuses its message and event conversion with its own request transport and parameters.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable

from ..search import is_search_unsupported
from ..stream import EventStream, StreamEvent, aborted_event
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
from ._errors import attach_error
from ._http import ProviderError, sdk_stream
from .openai import _client, classify_error
from .reasoning import responses_effort


def convert_messages(messages: list, *, api: str = "openai-responses") -> list[dict]:
    def input_parts(blocks: list) -> list[dict]:
        parts = []
        for block in blocks:
            if isinstance(block, TextContent):
                parts.append({"type": "input_text", "text": block.text})
            elif isinstance(block, ImageContent):
                parts.append({
                    "type": "input_image",
                    "image_url": f"data:{block.mime_type};base64,{block.data}",
                    "detail": "auto",
                })
        return parts

    result = []
    for message in messages:
        if isinstance(message, UserMessage):
            if isinstance(message.content, str):
                result.append({"role": "user", "content": message.content})
            elif content := input_parts(message.content):
                result.append({"role": "user", "content": content})
        elif isinstance(message, AssistantMessage):
            items = []
            for block in message.content:
                if isinstance(block, ThinkingContent) and message.api == api and block.signature:
                    try:
                        item = json.loads(block.signature)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(item, dict) and item.get("type") == "reasoning":
                        items.append(item)
                elif isinstance(block, TextContent) and block.text:
                    if block.native and message.api == api and message.stop_reason not in ("error", "aborted"):
                        # Annotated output messages carry IDs/status and sometimes new provider fields.
                        # Replaying the actual item avoids inventing an incomplete output-message shape.
                        items.append(copy.deepcopy(block.native))
                    else:
                        items.append({"role": "assistant", "content": block.text})
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
                    items.append(item)
                elif isinstance(block, RawContent) and block.api == api:
                    items.append(copy.deepcopy(block.data))
            # A reasoning item is only accepted together with the output item that follows it. A turn
            # cut off while reasoning, or whose tool calls were dropped after an abort, leaves it dangling.
            while items and items[-1].get("type") == "reasoning":
                items.pop()
            result.extend(items)
        elif isinstance(message, ToolResultMessage):
            if any(isinstance(block, ImageContent) for block in message.content):
                output = input_parts(message.content)
            else:
                output = message.text() or "(no output)"
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
    for declaration in declarations(ctx):
        params.setdefault("tools", []).append(declaration)
    if model.reasoning:
        params["include"] = ["reasoning.encrypted_content"]
        if opts.effort:
            params["reasoning"] = {
                "effort": responses_effort(model, opts.effort),
                "summary": "auto",
            }
    return params


def _dict(value) -> dict:
    return value if isinstance(value, dict) else value.model_dump(mode="json", exclude_none=True)


def _usage(value: dict) -> Usage:
    details = value.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", 0) or 0
    written = details.get("cache_write_tokens", 0) or 0
    return Usage(
        input=max(0, (value.get("input_tokens", 0) or 0) - cached - written),
        output=value.get("output_tokens", 0) or 0,
        cache_read=cached,
        cache_write=written,
    )


_RETRYABLE_ERROR_CODES = frozenset({
    "server_error",
    "rate_limit_exceeded",
    "server_is_overloaded",
    "overloaded",
    "service_unavailable",
    "slow_down",
    "too_many_requests",
})
_RETRYABLE_ERROR_TYPES = frozenset({
    "server_error",
    "service_unavailable_error",
    "rate_limit_error",
    "overloaded_error",
})


def parse_events(events: Iterable, model: Model, out: AssistantMessage, opts: StreamOptions, *, web_search: bool = False):
    """Convert SDK objects or decoded SSE dictionaries into Virgo stream events."""
    yield StreamEvent("start", out)
    slots: dict[int, int] = {}
    buffers: dict[int, str] = {}
    finished: set[int] = set()
    reasoning_by_id: dict[str, ThinkingContent] = {}
    text_offsets: dict[tuple[int, int], int] = {}
    refusal = False

    def update_message(block: TextContent, item: dict):
        nonlocal refusal
        parts = item.get("content")
        if not parts:
            return
        text, citations = "", []
        for part in parts:
            for annotation in part.get("annotations") or []:
                citation = copy.deepcopy(annotation)
                for key in ("start_index", "end_index"):
                    if isinstance(citation.get(key), int):
                        citation[key] += len(text)
                citations.append(citation)
            text += part.get("text") or part.get("refusal") or ""
        block.text = text
        if any("annotations" in part for part in parts):
            block.citations = citations
        block.native = copy.deepcopy(item) if block.citations else None
        refusal = refusal or any(part.get("type") == "refusal" for part in parts)

    def fail(error: dict, default: str):
        payload = error.get("error") if isinstance(error.get("error"), dict) else error
        out.stop_reason = "error"
        out.error_message = payload.get("message") or default
        code = payload.get("code")
        err_type = payload.get("type")
        lower = out.error_message.lower()
        retryable = (
            code in _RETRYABLE_ERROR_CODES
            or err_type in _RETRYABLE_ERROR_TYPES
            or "overload" in lower
            or "try again later" in lower
        )
        out.error_kind = "retryable" if retryable else "fatal"
        if web_search and code in ("invalid_request_error", "unsupported_tool", "unsupported_value", "unknown_tool"):
            exception = ProviderError(out.error_message, body=payload)
            if is_search_unsupported(exception):
                out.error_kind = "search_unsupported"
        attach_error(
            out, ProviderError(out.error_message, out.error_kind, body=payload), model,
            phase="provider.response",
        )

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
        elif kind == "web_search_call":
            block, event_kind = RawContent(model.api, copy.deepcopy(item)), "native_tool_start"
            out.native_started = True
        else:
            return
        slots[index] = len(out.content)
        out.content.append(block)
        if event_kind:
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
            elif index in slots and isinstance(out.content[slots[index]], RawContent):
                out.content[slots[index]].data = copy.deepcopy(item)
            elif index in slots and isinstance(out.content[slots[index]], TextContent):
                update_message(out.content[slots[index]], item)
            return
        ci = slots[index]
        block = out.content[ci]
        if isinstance(block, TextContent):
            update_message(block, item)
            event_kind = "text_end"
        elif isinstance(block, ThinkingContent):
            parts = item.get("summary") or item.get("content") or []
            block.thinking = "\n\n".join(p.get("text", "") for p in parts) or block.thinking
            block.signature = json.dumps(item)
            if item.get("id"):
                reasoning_by_id[item["id"]] = block
            event_kind = "thinking_end"
        elif isinstance(block, RawContent):
            block.data = copy.deepcopy(item)
            event_kind = "native_tool_end" if item.get("type") == "web_search_call" else None
        else:
            raw = item.get("arguments")
            block.arguments = parse_tool_arguments(raw if isinstance(raw, str) else buffers.get(index, ""))
            block.name = item.get("name") or block.name
            event_kind = "toolcall_end"
        finished.add(index)
        if event_kind:
            yield StreamEvent(event_kind, out, ci)

    for raw in events:
        if opts.signal is not None and opts.signal.is_set():
            yield aborted_event(out)
            return
        event = _dict(raw)
        kind = event.get("type", "")
        index = event.get("output_index", 0)
        if kind == "response.output_item.added":
            yield from start(index, event["item"])
        elif kind.startswith("response.web_search_call."):
            # A disconnect from this point must not silently repeat a potentially billed search.
            out.native_started = True
            status = kind.removeprefix("response.web_search_call.")
            yield from start(index, {"type": "web_search_call", "id": event.get("item_id"), "status": status})
            ci = slots.get(index)
            if ci is not None and isinstance(out.content[ci], RawContent):
                out.content[ci].data["status"] = status
                yield StreamEvent("native_tool_delta", out, ci)
        elif kind in ("response.output_text.delta", "response.refusal.delta"):
            yield from start(index, {"type": "message"})
            ci = slots[index]
            delta = event.get("delta", "")
            text_offsets.setdefault((index, event.get("content_index", 0)), len(out.content[ci].text))
            out.content[ci].text += delta
            refusal = refusal or kind == "response.refusal.delta"
            yield StreamEvent("text_delta", out, ci, delta)
        elif kind == "response.output_text.annotation.added":
            yield from start(index, {"type": "message"})
            annotation = event.get("annotation")
            if isinstance(annotation, dict):
                citation = copy.deepcopy(annotation)
                offset = text_offsets.get((index, event.get("content_index", 0)), 0)
                for key in ("start_index", "end_index"):
                    if isinstance(citation.get(key), int):
                        citation[key] += offset
                out.content[slots[index]].citations.append(citation)
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
            out.usage = _usage(response.get("usage") or {})
            status = response.get("status") or kind.removeprefix("response.")
            reason = (response.get("incomplete_details") or {}).get("reason")
            if status == "failed":
                fail(response.get("error") or {}, "The provider failed to complete the response.")
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
            if out.stop_reason == "error" and out.error_details is None:
                attach_error(out, None, model, phase="provider.response")
            yield StreamEvent("error" if out.stop_reason in ("error", "refusal") else "done", out)
            return
        elif kind == "error":
            fail(event, "The provider returned a stream error.")
            yield StreamEvent("error", out)
            return
    if opts.signal is not None and opts.signal.is_set():
        yield aborted_event(out)
        return
    out.stop_reason, out.error_kind = "error", "retryable"
    out.error_message = "Responses stream ended before the final response."
    attach_error(out, None, model)
    yield StreamEvent("error", out)


def stream(model: Model, ctx: Context, opts: StreamOptions) -> EventStream:
    out = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
    def parse(items, request):
        for event in parse_events(items, model, out, opts, web_search=bool(ctx.web_tools)):
            if out.error_message and out.stop_reason != "aborted":
                attach_error(out, None, model, token=getattr(request.client, "api_key", ""), response=request.response)
            yield event

    def on_error(exc, request):
        out.error_kind, out.error_message = classify_error(exc)
        if ctx.web_tools and is_search_unsupported(exc):
            out.error_kind = "search_unsupported"
        out.stop_reason = "error"
        attach_error(out, exc, model, token=getattr(request.client, "api_key", ""), response=request.response)

    return sdk_stream(
        out, opts, lambda bridge: _client(model, bridge),
        lambda client: client.responses.create(**build_params(model, ctx, opts)),
        parse, on_error,
    )
