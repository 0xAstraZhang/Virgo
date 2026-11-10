"""Scripted provider for tests and offline demos (model spec: ``faux/<name>``).

    from virgo.ai.providers import faux
    faux.script("t", faux.reply(tools=[("read", {"path": "a.txt"})]), faux.reply("done"))

Every request is recorded in ``faux.CALLS[model_id]`` as plain dicts, so tests can assert on
exactly what was sent.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from ..stream import EventStream, StreamEvent
from ..types import (
    AssistantMessage,
    Context,
    Model,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)


@dataclass
class FauxReply:
    text: str = ""
    tool_calls: list = field(default_factory=list)  # [(name, args)] or [(name, args, id)]
    thinking: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    error_kind: str = "fatal"
    delay: float = 0.0


Responder = FauxReply | Callable[[Context], FauxReply]

_lock = threading.Lock()
_SCRIPTS: dict[str, deque] = defaultdict(deque)
_HANDLERS: dict[str, Callable[[Context], FauxReply]] = {}
CALLS: dict[str, list[dict]] = defaultdict(list)
_ids = itertools.count(1)


def reply(text: str = "", tools: list | None = None, **kw) -> FauxReply:
    return FauxReply(text=text, tool_calls=list(tools or []), **kw)


def error(message: str, kind: str = "fatal") -> FauxReply:
    return FauxReply(error=message, error_kind=kind)


def script(model_id: str, *responses: Responder) -> None:
    with _lock:
        _SCRIPTS[model_id] = deque(responses)
        CALLS[model_id] = []


def push(model_id: str, *responses: Responder) -> None:
    with _lock:
        _SCRIPTS[model_id].extend(responses)


def handler(model_id: str, fn: Callable[[Context], FauxReply]) -> None:
    """Respond to every request (once the script queue is empty) with fn(context)."""
    with _lock:
        _HANDLERS[model_id] = fn


def reset() -> None:
    with _lock:
        _SCRIPTS.clear()
        _HANDLERS.clear()
        CALLS.clear()


def _record(model: Model, ctx: Context) -> None:
    CALLS[model.id].append(
        {
            "system": ctx.system,
            "tools": [t.name for t in ctx.tools],
            "messages": [asdict(m) for m in ctx.messages],
        }
    )


def _demo(ctx: Context) -> FauxReply:
    """Default behaviour when no script is queued: a tiny deterministic 'agent'."""
    last = ctx.messages[-1] if ctx.messages else None
    if isinstance(last, ToolResultMessage):
        lines = last.text().splitlines()
        body = "\n".join(lines[:20]) + ("\n..." if len(lines) > 20 else "")
        return reply(f"Tool `{last.tool_name}` returned {len(lines)} line(s):\n{body}")
    text = last.text() if isinstance(last, UserMessage) else ""
    tool_names = {t.name for t in ctx.tools}
    if "python" in text.lower() and "glob" in tool_names:
        return reply("Let me look for Python files.", tools=[("glob", {"pattern": "**/*.py"})])
    return reply(f"(faux) You said: {text}")


def stream(model: Model, ctx: Context, opts: StreamOptions) -> EventStream:
    with _lock:
        _record(model, ctx)
        queue = _SCRIPTS.get(model.id)
        responder = queue.popleft() if queue else _HANDLERS.get(model.id, _demo)
    out = AssistantMessage(api="faux", provider=model.provider, model=model.id)
    return EventStream(_events(responder, ctx, out, opts), out)


def _events(responder: Responder, ctx: Context, out: AssistantMessage, opts: StreamOptions):
    r = responder(ctx) if callable(responder) else responder
    chars = len(ctx.system) + sum(len(json.dumps(asdict(m), default=str)) for m in ctx.messages)
    out.usage = Usage(input=chars // 4)
    yield StreamEvent("start", out)
    if r.delay:
        end = time.time() + r.delay
        while time.time() < end:
            if opts.signal is not None and opts.signal.is_set():
                out.stop_reason = "aborted"
                out.error_message = "aborted"
                yield StreamEvent("error", out)
                return
            time.sleep(0.01)
    if r.error:
        out.stop_reason = "error"
        out.error_message = r.error
        out.error_kind = r.error_kind
        yield StreamEvent("error", out)
        return
    if r.thinking:
        out.content.append(ThinkingContent(r.thinking, signature="faux-sig"))
        idx = len(out.content) - 1
        yield StreamEvent("thinking_start", out, idx)
        yield StreamEvent("thinking_delta", out, idx, r.thinking)
        yield StreamEvent("thinking_end", out, idx)
    if r.text:
        out.content.append(TextContent(""))
        idx = len(out.content) - 1
        yield StreamEvent("text_start", out, idx)
        for piece in _chunks(r.text):
            out.content[idx].text += piece
            yield StreamEvent("text_delta", out, idx, piece)
        yield StreamEvent("text_end", out, idx)
    for call in r.tool_calls:
        name, args = call[0], call[1]
        cid = call[2] if len(call) > 2 else f"faux_{next(_ids)}"
        out.content.append(ToolCall(cid, name, dict(args)))
        idx = len(out.content) - 1
        yield StreamEvent("toolcall_start", out, idx)
        yield StreamEvent("toolcall_end", out, idx)
    out.usage.output = max(1, len(r.text) // 4)
    out.stop_reason = r.stop_reason or ("tool_use" if r.tool_calls else "stop")
    yield StreamEvent("done", out)


def _chunks(text: str, n: int = 16):
    for i in range(0, len(text), n):
        yield text[i : i + n]
