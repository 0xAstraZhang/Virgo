"""Normalize a transcript before it is converted for a provider (Pi's transform-messages)."""

from __future__ import annotations

import copy
import re

from .types import (
    AssistantMessage,
    ImageContent,
    Model,
    RawContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


def normalize_tool_id(tid: str) -> str:
    return _ID_RE.sub("_", tid)[:64] or "call"


def normalize(messages: list, model: Model) -> list:
    """Return a cleaned copy:

    - drop errored/aborted assistant messages that carry nothing usable
    - drop thinking / raw blocks produced by a different API
    - add an error tool result for any tool call that never got one
    - drop tool results whose call is missing
    - replace images with a placeholder for non-vision models
    """
    out: list = []
    open_calls: dict[str, ToolCall] = {}

    def close_open():
        for call in open_calls.values():
            out.append(
                ToolResultMessage(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content=[TextContent("No result provided (the call was interrupted).")],
                    is_error=True,
                )
            )
        open_calls.clear()

    for m in messages:
        if isinstance(m, AssistantMessage):
            close_open()
            if m.stop_reason in ("error", "aborted") and not m.tool_calls():
                if not m.text().strip():
                    continue
            same_api = m.api == model.api
            blocks = []
            for b in m.content:
                if isinstance(b, (ThinkingContent, RawContent)):
                    if isinstance(b, RawContent) and b.api != model.api:
                        continue
                    if isinstance(b, ThinkingContent) and not same_api:
                        continue
                if isinstance(b, TextContent) and not b.text:
                    continue
                blocks.append(b)
            if m.stop_reason in ("error", "aborted"):
                # never replay tool calls from a broken turn
                blocks = [b for b in blocks if not isinstance(b, ToolCall)]
            if not blocks:
                continue
            nm = copy.copy(m)
            nm.content = blocks
            out.append(nm)
            for b in blocks:
                if isinstance(b, ToolCall):
                    open_calls[b.id] = b
        elif isinstance(m, ToolResultMessage):
            if m.tool_call_id not in open_calls:
                continue
            open_calls.pop(m.tool_call_id)
            out.append(_strip_images(m, model) if not model.vision else m)
        else:
            close_open()
            out.append(_strip_images(m, model) if not model.vision else m)
    close_open()
    return out


def _strip_images(m, model: Model):
    if isinstance(m.content, str) or not any(isinstance(b, ImageContent) for b in m.content):
        return m
    nm = copy.copy(m)
    nm.content = [
        TextContent(f"[image omitted: {model.id} does not accept images]")
        if isinstance(b, ImageContent)
        else b
        for b in m.content
    ]
    return nm


def strip_thinking(messages: list) -> list:
    """Remove thinking blocks (used for turns retained across a compaction)."""
    out = []
    for m in messages:
        if isinstance(m, AssistantMessage) and any(isinstance(b, ThinkingContent) for b in m.content):
            nm = copy.copy(m)
            nm.content = [b for b in m.content if not isinstance(b, ThinkingContent)]
            if not nm.content:
                continue
            out.append(nm)
        else:
            out.append(m)
    return out


def is_user_like(m) -> bool:
    return isinstance(m, (UserMessage, ToolResultMessage))
