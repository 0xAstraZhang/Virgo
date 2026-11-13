"""Google account requests through Cloud Code Assist, following Pi's Gemini CLI adapter."""

from __future__ import annotations

import copy
import json
import uuid

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
from ._errors import attach_error
from ._http import ProviderError, cancelled, classify_error, json_events

_API = "google-gemini-cli"
_ENDPOINT = "https://cloudcode-pa.googleapis.com"
_REFUSALS = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION", "IMAGE_SAFETY"}


def _parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content}]
    parts = []
    for block in content:
        if isinstance(block, TextContent):
            parts.append({"text": block.text})
        elif isinstance(block, ImageContent):
            parts.append({"inlineData": {"mimeType": block.mime_type, "data": block.data}})
    return parts


def convert_messages(model: Model, messages: list) -> list[dict]:
    contents = []
    historical_calls = set()
    response_ids = {}
    for message in messages:
        if isinstance(message, UserMessage):
            parts = _parts(message.content)
            if parts:
                contents.append({"role": "user", "parts": parts})
        elif isinstance(message, AssistantMessage):
            same_model = message.provider == model.provider and message.model == model.id
            raw = next(
                (b for b in message.content if isinstance(b, RawContent) and b.api == _API), None
            )
            if raw is not None and same_model:
                # Preserve signature-bearing parts exactly where Google supplied them.
                parts = copy.deepcopy(raw.data["parts"])
                raw_calls = [part["functionCall"] for part in parts if "functionCall" in part]
                for call, raw_call in zip(message.tool_calls(), raw_calls, strict=False):
                    if raw_call.get("id"):
                        response_ids[call.id] = raw_call["id"]
            else:
                parts = []
                for block in message.content:
                    if isinstance(block, TextContent) and block.text:
                        parts.append({"text": block.text})
                    elif isinstance(block, ThinkingContent) and block.thinking:
                        part = {"text": block.thinking}
                        if same_model:
                            part["thought"] = True
                            if block.signature:
                                part["thoughtSignature"] = block.signature
                        parts.append(part)
                    elif isinstance(block, ToolCall):
                        if "gemini-3" in model.id:
                            # Gemini 3 rejects unsigned function calls in imported history.
                            historical_calls.add(block.id)
                            parts.append({"text": f"Previous tool {block.name}: {json.dumps(block.arguments)}"})
                        else:
                            parts.append({"functionCall": {"name": block.name, "args": block.arguments}})
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif isinstance(message, ToolResultMessage):
            if message.tool_call_id in historical_calls:
                parts = [{"text": f"Previous result from {message.tool_name}: {message.text()}"}]
            else:
                result = {"error" if message.is_error else "output": message.text()}
                response = {"name": message.tool_name, "response": result}
                if message.tool_call_id in response_ids:
                    response["id"] = response_ids[message.tool_call_id]
                parts = [{"functionResponse": response}]
                images = _parts([b for b in message.content if isinstance(b, ImageContent)])
                if images and model.vision:
                    if "gemini-3" in model.id:
                        response["parts"] = images
                    else:
                        parts.extend(images)
            # Parallel function responses must be in the same user turn.
            if contents and contents[-1]["role"] == "user":
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": "user", "parts": parts})
    return contents


def build_params(model: Model, ctx: Context, opts: StreamOptions) -> dict:
    project = model.options.get("authMetadata", {}).get("projectId")
    if not project:
        raise ProviderError("Google credentials have no project ID. Use /login auth google again.")
    limit = opts.max_tokens or model.max_tokens
    generation = {"maxOutputTokens": limit}
    if model.reasoning and opts.effort:
        thinking = {"includeThoughts": True}
        if "gemini-3" in model.id:
            if "pro" in model.id:
                thinking["thinkingLevel"] = "LOW" if opts.effort == "low" else "HIGH"
            else:
                thinking["thinkingLevel"] = {"low": "LOW", "medium": "MEDIUM"}.get(opts.effort, "HIGH")
        else:
            budget = {"low": 2048, "medium": 8192}.get(opts.effort, 16384)
            thinking["thinkingBudget"] = min(budget, max(0, limit - 1024))
        generation["thinkingConfig"] = thinking
    request = {"contents": convert_messages(model, ctx.messages), "generationConfig": generation}
    if ctx.system:
        request["systemInstruction"] = {"parts": [{"text": ctx.system}]}
    if ctx.tools:
        request["tools"] = [{"functionDeclarations": [
            {"name": tool.name, "description": tool.description, "parametersJsonSchema": tool.parameters}
            for tool in ctx.tools
        ]}]
    return {"model": model.id, "project": project, "user_prompt_id": str(uuid.uuid4()), "request": request}


def _url(base_url: str | None) -> str:
    base = (base_url or _ENDPOINT).rstrip("/")
    if ":streamGenerateContent" in base:
        return base if "?" in base else base + "?alt=sse"
    if not base.endswith("/v1internal"):
        base += "/v1internal"
    return base + ":streamGenerateContent?alt=sse"


def stream(model: Model, ctx: Context, opts: StreamOptions) -> EventStream:
    out = AssistantMessage(api=_API, provider=model.provider, model=model.id)
    return EventStream(_events(model, ctx, opts, out), out)


def _events(model: Model, ctx: Context, opts: StreamOptions, out: AssistantMessage):
    yield StreamEvent("start", out)
    token = model.options.get("apiKey", "")
    network = None
    try:
        if cancelled(opts.signal):
            raise ProviderError("aborted by user")
        if not token:
            raise ProviderError("Google requires account authentication. Use /login auth google.")
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "google-cloud-sdk vscode_cloudshelleditor/0.1",
            "X-Goog-Api-Client": "gl-python/3",
            "Client-Metadata": json.dumps({
                "ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"
            }),
            **model.options.get("headers", {}),
            "Authorization": f"Bearer {token}",
        }
        current = None
        finish = None
        raw_parts = []
        call_ids = set()
        network = json_events(_url(model.base_url), headers, build_params(model, ctx, opts), opts.signal)
        for chunk in network:
            if cancelled(opts.signal):
                raise ProviderError("aborted by user")
            if chunk.get("error"):
                error = chunk["error"]
                message = error.get("message", "Google request failed") if isinstance(error, dict) else str(error)
                code = error.get("code") if isinstance(error, dict) else None
                raise ProviderError(
                    message, "retryable" if code in (429, 500, 502, 503, 504) else "fatal",
                    status_code=code, body=error,
                )
            response = chunk.get("response", {})
            usage = response.get("usageMetadata")
            if usage:
                cache = usage.get("cachedContentTokenCount", 0) or 0
                out.usage = Usage(
                    input=max(0, (usage.get("promptTokenCount", 0) or 0) - cache),
                    output=(usage.get("candidatesTokenCount", 0) or 0) + (usage.get("thoughtsTokenCount", 0) or 0),
                    cache_read=cache,
                )
            feedback = response.get("promptFeedback", {})
            if feedback.get("blockReason"):
                finish = feedback["blockReason"]
            candidates = response.get("candidates") or []
            if not candidates:
                continue
            candidate = candidates[0]
            for part in candidate.get("content", {}).get("parts", []):
                raw_parts.append(copy.deepcopy(part))
                if "text" in part:
                    kind = "thinking" if part.get("thought") is True else "text"
                    if current is None or current[0] != kind:
                        if current is not None:
                            yield StreamEvent(current[0] + "_end", out, current[1])
                        out.content.append(ThinkingContent("") if kind == "thinking" else TextContent(""))
                        current = (kind, len(out.content) - 1)
                        yield StreamEvent(kind + "_start", out, current[1])
                    block = out.content[current[1]]
                    delta = part["text"]
                    if kind == "thinking":
                        block.thinking += delta
                        block.signature = part.get("thoughtSignature") or block.signature
                    else:
                        block.text += delta
                    yield StreamEvent(kind + "_delta", out, current[1], delta)
                if "functionCall" in part:
                    if current is not None:
                        yield StreamEvent(current[0] + "_end", out, current[1])
                        current = None
                    call = part["functionCall"]
                    arguments = call.get("args") or {}
                    if not isinstance(arguments, dict) or not call.get("name"):
                        raise ProviderError("Google returned an invalid function call.")
                    call_id = call.get("id")
                    if not call_id or call_id in call_ids:
                        call_id = "call_" + uuid.uuid4().hex
                    call_ids.add(call_id)
                    out.content.append(ToolCall(call_id, call["name"], arguments))
                    index = len(out.content) - 1
                    yield StreamEvent("toolcall_start", out, index)
                    yield StreamEvent("toolcall_delta", out, index, json.dumps(arguments))
                    yield StreamEvent("toolcall_end", out, index)
            finish = candidate.get("finishReason") or finish
        if cancelled(opts.signal):
            raise ProviderError("aborted by user")
        if not finish:
            raise ProviderError("Google stream ended before a finish reason.", "retryable")
        if current is not None:
            yield StreamEvent(current[0] + "_end", out, current[1])
        if raw_parts:
            out.content.append(RawContent(_API, {"parts": raw_parts}))
        if finish == "STOP":
            out.stop_reason = "tool_use" if out.tool_calls() else "stop"
        elif finish == "MAX_TOKENS":
            out.stop_reason = "length"
        elif finish in _REFUSALS:
            out.stop_reason = "refusal"
            out.error_message = f"Google blocked the response ({finish})."
        else:
            raise ProviderError(f"Google could not complete the response ({finish}).")
        yield StreamEvent("done", out)
    except Exception as error:  # noqa: BLE001 - provider errors follow the EventStream contract
        out.stop_reason = "aborted" if cancelled(opts.signal) else "error"
        out.error_kind, out.error_message = classify_error(error, token)
        attach_error(out, error, model, token=token)
        if network is not None:
            close = getattr(network, "close", None)
            if close is not None:
                close()
            network = None
        yield StreamEvent("error", out)
    finally:
        if network is not None:
            close = getattr(network, "close", None)
            if close is not None:
                close()
