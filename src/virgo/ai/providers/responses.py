"""OpenAI Responses transport, including xAI account credentials.

The message and event conversion follows Pi's openai-responses-shared adapter.
Codex reuses it with its own request transport and parameters.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable

from ..search import is_search_unsupported
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
from ._errors import attach_error
from ._http import ProviderError, cancelled, run_cancellable
from .openai import _client, classify_error
from .reasoning import responses_effort


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


def compact(model: Model, ctx: Context, opts: StreamOptions, *, focus: str | None = None) -> AssistantMessage:
    """Return the standalone endpoint's entire replacement window verbatim.

    OpenAI's compaction contract omits instructions. Neither the system prompt
    nor a custom focus is injected into input, which would change its prefix.
    """
    out = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
    token = model.options.get("apiKey", "")
    try:
        if cancelled(opts.signal):
            raise ProviderError("Aborted by user.", "aborted")
        client = _client(model)
        token = token or getattr(client, "api_key", "")
        result = run_cancellable(
            lambda: client.responses.with_raw_response.compact(
                model=model.id,
                input=convert_messages(ctx.messages, api=model.api),
                timeout=600.0,
            ),
            opts.signal,
        )
        # The compacted window can contain input messages and newer item types
        # outside the SDK's output union. Read JSON to preserve their wire shape.
        data = json.loads(result.content)
        if not isinstance(data, dict):
            raise ProviderError("The provider returned an invalid compacted context.")
        items = data.get("output")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ProviderError("The provider returned an invalid compacted context.")
        if not any(
            item.get("type") == "compaction"
            and isinstance(item.get("encrypted_content"), str)
            and item["encrypted_content"]
            for item in items
        ):
            raise ProviderError("The provider returned no encrypted compaction item.")
        out.content = [RawContent(model.api, copy.deepcopy(item)) for item in items]
        out.usage = _usage(data.get("usage") or {})
        out.stop_reason = "compaction"
    except Exception as error:  # provider contract: failures are data
        if isinstance(error, ProviderError):
            out.error_kind, out.error_message = error.kind, str(error)
        else:
            out.error_kind, out.error_message = classify_error(error)
        out.stop_reason = "aborted" if cancelled(opts.signal) or out.error_kind == "aborted" else "error"
        attach_error(out, error, model, phase="provider.compact", token=token)
    return out


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
        out.stop_reason = "error"
        out.error_message = error.get("message") or default
        code = error.get("code")
        out.error_kind = "retryable" if code in ("server_error", "rate_limit_exceeded") else "fatal"
        if web_search and code in ("invalid_request_error", "unsupported_tool", "unsupported_value", "unknown_tool"):
            exception = ProviderError(out.error_message, body=error)
            if is_search_unsupported(exception):
                out.error_kind = "search_unsupported"
        attach_error(
            out, ProviderError(out.error_message, out.error_kind, body=error), model,
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
        elif kind in ("compaction", "web_search_call"):
            block, event_kind = RawContent(model.api, copy.deepcopy(item)), None
            if kind == "web_search_call":
                out.search_started = out.native_started = True
                event_kind = "native_tool_start"
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
            out.stop_reason, out.error_message = "aborted", "aborted by user"
            yield StreamEvent("error", out)
            return
        event = _dict(raw)
        kind = event.get("type", "")
        index = event.get("output_index", 0)
        if kind == "response.output_item.added":
            yield from start(index, event["item"])
        elif kind.startswith("response.web_search_call."):
            # A disconnect from this point must not silently repeat a potentially billed search.
            out.search_started = out.native_started = True
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
        out.stop_reason, out.error_message = "aborted", "aborted by user"
    else:
        out.stop_reason, out.error_kind = "error", "retryable"
        out.error_message = "Responses stream ended before the final response."
        attach_error(out, None, model)
    yield StreamEvent("error", out)


def stream(model: Model, ctx: Context, opts: StreamOptions) -> EventStream:
    out = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
    return EventStream(_events(model, ctx, opts, out), out)


def _events(model: Model, ctx: Context, opts: StreamOptions, out: AssistantMessage):
    response = None
    token = ""
    try:
        if opts.signal is not None and opts.signal.is_set():
            out.stop_reason, out.error_message = "aborted", "aborted by user"
            yield StreamEvent("error", out)
            return
        client = _client(model)
        token = getattr(client, "api_key", "")
        response = client.responses.create(**build_params(model, ctx, opts))
        for event in parse_events(response, model, out, opts, web_search=bool(ctx.web_search)):
            if out.error_message:
                attach_error(out, None, model, token=token, response=getattr(response, "response", None))
            yield event
    except Exception as exc:  # noqa: BLE001 - provider failures are data
        out.error_kind, out.error_message = classify_error(exc)
        if ctx.web_search and is_search_unsupported(exc):
            out.error_kind = "search_unsupported"
        out.stop_reason = "aborted" if opts.signal is not None and opts.signal.is_set() else "error"
        attach_error(out, exc, model, token=token, response=getattr(response, "response", None))
        yield StreamEvent("error", out)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - cleanup must not replace the result
                pass
