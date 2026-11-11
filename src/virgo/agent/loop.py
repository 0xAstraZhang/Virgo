"""The agent loop: stateless, no I/O. Mirrors Pi's runLoop.

while there are tool calls or pending messages:
    call the model, run its tool calls, pick up steering messages
when it would stop: deliver follow-ups, then ask stop hooks whether to continue
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from ..ai.types import AssistantMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
from ..ai.validate import INVALID_ARGUMENTS, validate
from .types import AgentConfig, AgentEvent, ToolCallEvent, ToolContext, ToolResult

Emit = Callable[[AgentEvent], None]
Queue = Callable[[], list]


def run_loop(
    messages: list,
    prompts: list,
    cfg: AgentConfig,
    emit: Emit,
    signal: threading.Event,
    steering: Queue = lambda: [],
    follow_ups: Queue = lambda: [],
) -> None:
    """Append to `messages` in place, emitting events as it goes."""
    tools = {t.name: t for t in cfg.tools}

    def push(m) -> None:
        emit(AgentEvent("message_start", cfg.name, message=m))
        messages.append(m)
        emit(AgentEvent("message_end", cfg.name, message=m))

    emit(AgentEvent("agent_start", cfg.name))
    pending = list(prompts)
    need_call = True
    turns = 0
    native_continues = 0
    try:
        while True:
            while need_call or pending:
                if signal.is_set():
                    return
                emit(AgentEvent("turn_start", cfg.name))
                for m in pending:
                    push(m)
                pending = []
                if cfg.max_turns is not None and turns >= cfg.max_turns:
                    push(
                        AssistantMessage(
                            content=[TextContent(f"[Stopped: reached the {cfg.max_turns}-turn limit.]")],
                            stop_reason="stop",
                        )
                    )
                    emit(AgentEvent("turn_end", cfg.name))
                    return
                turns += 1
                msg = _stream_assistant(messages, cfg, emit, signal)
                messages.append(msg)
                emit(AgentEvent("message_end", cfg.name, message=msg))
                if msg.stop_reason in ("error", "aborted", "refusal"):
                    emit(AgentEvent("turn_end", cfg.name, message=msg))
                    return
                calls = msg.tool_calls()
                results, terminate = _execute(calls, msg, tools, cfg, emit, signal) if calls else ([], False)
                for r in results:
                    push(r)
                emit(AgentEvent("turn_end", cfg.name, message=msg, tool_results=results))
                if terminate:
                    return
                if msg.stop_reason == "pause_turn":
                    native_continues += 1
                    if native_continues > 3:
                        push(AssistantMessage(
                            content=[TextContent("[Search paused after three continuations. Send another message to resume.]")],
                            stop_reason="stop",
                        ))
                        emit(AgentEvent("turn_end", cfg.name))
                        return
                else:
                    native_continues = 0
                need_call = bool(calls) or msg.stop_reason == "pause_turn"
                pending = steering()
            pending = follow_ups()
            if pending:
                continue
            cont = cfg.should_stop(messages) if cfg.should_stop else None
            if cont is not None:
                pending = [UserMessage(cont.text, kind="continue")]
                continue
            return
    finally:
        emit(AgentEvent("agent_end", cfg.name, message=messages[-1] if messages else None))


def _stream_assistant(
    messages: list, cfg: AgentConfig, emit: Emit, signal: threading.Event
) -> AssistantMessage:
    stream = cfg.call_model(messages, signal)
    started = False
    for ev in stream:
        if not started:
            emit(AgentEvent("message_start", cfg.name, message=ev.partial))
            started = True
        if ev.type not in ("start", "done", "error"):
            emit(AgentEvent("message_update", cfg.name, message=ev.partial, stream_event=ev))
    msg = stream.result()
    if signal.is_set() and msg.stop_reason not in ("error",):
        msg.stop_reason = "aborted"
    return msg


def _execute(
    calls: list[ToolCall], msg: AssistantMessage, tools: dict, cfg: AgentConfig, emit: Emit, signal
) -> tuple[list, bool]:
    results: list[ToolResult | None] = [None] * len(calls)
    events: list[ToolCallEvent | None] = [None] * len(calls)

    # 1. preflight, in source order
    for i, call in enumerate(calls):
        emit(
            AgentEvent(
                "tool_execution_start",
                cfg.name,
                tool_call_id=call.id,
                tool_name=call.name,
                args=call.arguments,
            )
        )
        if signal.is_set():
            results[i] = ToolResult.error("Operation aborted.")
            continue
        if msg.stop_reason == "length":
            results[i] = ToolResult.error(
                "The response hit the output token limit, so these tool arguments may be truncated. "
                "Re-issue the call (split large content into smaller steps)."
            )
            continue
        tool = tools.get(call.name)
        if tool is None:
            results[i] = ToolResult.error(f"Unknown tool: {call.name}. Available: {', '.join(sorted(tools))}")
            continue
        args = dict(call.arguments)
        if INVALID_ARGUMENTS in args:
            results[i] = ToolResult.error("Tool arguments were not a valid JSON object. Re-issue the call.")
            continue
        try:
            if tool.prepare_arguments:
                args = tool.prepare_arguments(args)
            args, errs = validate(args, tool.parameters)
        except Exception as e:  # noqa: BLE001
            errs = [str(e)]
        if errs:
            results[i] = ToolResult.error("Invalid arguments:\n" + "\n".join(f"- {e}" for e in errs))
            continue
        ev = ToolCallEvent(call.id, call.name, args, tool, cfg.name, cfg.cwd, signal=signal)
        if cfg.before_tool_call:
            try:
                block = cfg.before_tool_call(ev)
            except Exception as e:  # noqa: BLE001
                block = None
                results[i] = ToolResult.error(f"Tool call hook failed: {e}")
                continue
            if block is not None:
                results[i] = ToolResult.error(block.reason)
                continue
        events[i] = ev

    # 2. execute
    def run(i: int) -> None:
        ev = events[i]
        if signal.is_set():
            results[i] = ToolResult.error("Operation aborted.")
            return
        if cfg.before_tool_execute:
            try:
                block = cfg.before_tool_execute(ev)
            except Exception as e:  # noqa: BLE001
                results[i] = ToolResult.error(f"Execution permission check failed: {e}")
                return
            if block is not None:
                results[i] = ToolResult.error(block.reason)
                return
            if signal.is_set():
                results[i] = ToolResult.error("Operation aborted.")
                return
        ctx = ToolContext(ev.call_id, ev.cwd, signal, cfg.name)
        start = time.time()
        try:
            out = ev.tool.execute(ev.args, ctx)
            res = ToolResult.text(out) if isinstance(out, str) else out
        except Exception as e:  # noqa: BLE001
            res = ToolResult.error(f"{type(e).__name__}: {e}")
        if cfg.after_tool_call:
            try:
                res = cfg.after_tool_call(ev, res) or res
            except Exception as e:  # noqa: BLE001
                res = ToolResult.error(f"Tool result hook failed: {e}")
        res.details = res.details if res.details is not None else {}
        if isinstance(res.details, dict):
            res.details.setdefault("duration", round(time.time() - start, 3))
        results[i] = res

    todo = [i for i, ev in enumerate(events) if ev is not None]
    sequential = any(events[i].tool.sequential for i in todo) or len(todo) <= 1
    if sequential:
        for i in todo:
            run(i)
            _end(cfg, emit, calls[i], results[i])
    else:
        with ThreadPoolExecutor(max_workers=min(cfg.max_parallel, len(todo))) as pool:
            futures = {pool.submit(run, i): i for i in todo}
            for f in futures:
                f.result()
                i = futures[f]
                _end(cfg, emit, calls[i], results[i])
    for i, call in enumerate(calls):
        if events[i] is None:
            _end(cfg, emit, call, results[i])

    out = [
        ToolResultMessage(call.id, call.name, res.content, details=res.details, is_error=res.is_error)
        for call, res in zip(calls, results, strict=True)
    ]
    return out, all(r.terminate for r in results)


def _end(cfg: AgentConfig, emit: Emit, call: ToolCall, res: ToolResult) -> None:
    emit(
        AgentEvent(
            "tool_execution_end",
            cfg.name,
            tool_call_id=call.id,
            tool_name=call.name,
            args=call.arguments,
            result=res,
            is_error=res.is_error,
        )
    )
