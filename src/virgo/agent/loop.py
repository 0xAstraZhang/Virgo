"""The agent loop: stateless, no I/O. Mirrors Pi's runLoop.

while there are tool calls or pending messages:
    call the model, run its tool calls, pick up steering messages
when it would stop: deliver follow-ups, then ask stop hooks whether to continue
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..ai.types import AssistantMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
from ..ai.validate import INVALID_ARGUMENTS, validate
from ..diagnostics import capture_error, safe_text
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
                    # Preserve accepted prompts even if run-start persistence failed.
                    for m in pending:
                        push(m)
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
                if signal.is_set() or (cfg.can_run is not None and not cfg.can_run()):
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
                if signal.is_set():
                    return
                pending = steering()
            if signal.is_set():
                return
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
    started = False
    msg = AssistantMessage()
    try:
        stream = cfg.call_model(messages, signal)
        for ev in stream:
            msg = ev.partial
            if not started:
                emit(AgentEvent("message_start", cfg.name, message=ev.partial))
                started = True
            if ev.type not in ("start", "done", "error"):
                emit(AgentEvent("message_update", cfg.name, message=ev.partial, stream_event=ev))
        msg = stream.result()
    except Exception as error:  # noqa: BLE001 - custom model callbacks follow the stream contract
        if signal.is_set():
            msg.stop_reason = "aborted"
            msg.error_message = "aborted by user"
        else:
            msg.stop_reason = "error"
            msg.error_kind = "fatal"
            msg.error_details = capture_error(error, phase="model.call", context={"agent": cfg.name})
            msg.error_message = safe_text(f"{type(error).__name__}: {msg.error_details['message']}")
        if not started:
            emit(AgentEvent("message_start", cfg.name, message=msg))
    if signal.is_set() and msg.stop_reason != "error":
        msg.stop_reason = "aborted"
    return msg


def _tool_details(res: ToolResult) -> dict:
    """Keep extension-owned metadata when an error needs a diagnostic envelope."""
    if not isinstance(res.details, dict):
        res.details = {} if res.details is None else {"tool_details": res.details}
    return res.details


def _annotate_error(res: ToolResult, call: ToolCall, cfg: AgentConfig, *, phase: str,
                    error: Exception | str | None = None, kind: str = "fatal", code=None) -> ToolResult:
    if res.is_error and res.as_text().strip().lower() not in {
        "operation aborted.", "aborted by user", "aborted", "sub-agent was aborted.",
    }:
        details = _tool_details(res)
        if not details.get("error"):
            try:
                existing = getattr(error, "_virgo_error_details", None) if isinstance(error, Exception) else None
            except Exception:
                existing = None
            details["error"] = existing if isinstance(existing, dict) else capture_error(
                error if error is not None else res.as_text(), phase=phase, kind=kind, code=code,
                context={"agent": cfg.name, "tool_name": call.name, "tool_call_id": call.id},
            )
    return res


def _tool_error(call: ToolCall, cfg: AgentConfig, text: str, *, phase: str,
                error: Exception | str | None = None, details=None, kind="fatal", code=None) -> ToolResult:
    return _annotate_error(ToolResult.error(safe_text(text), details=details), call, cfg,
                           phase=phase, error=error, kind=kind, code=code)


def _preserve_errors(res: ToolResult, previous: list[dict]) -> None:
    """A result hook may recover or replace a failure without erasing its evidence."""
    if not previous:
        return
    details = _tool_details(res)
    current = details.get("error")
    related = list(details.get("related_errors") or [])
    for error in previous:
        if error != current and error not in related:
            related.append(error)
    if related:
        details["related_errors"] = related


def _execute(
    calls: list[ToolCall], msg: AssistantMessage, tools: dict, cfg: AgentConfig, emit: Emit, signal
) -> tuple[list, bool]:
    results: list[ToolResult | None] = [None] * len(calls)
    events: list[ToolCallEvent | None] = [None] * len(calls)

    def reject(i: int, reason: str, *, phase="tool.preflight", error=None, details=None, kind="fatal",
               code=None) -> None:
        results[i] = _tool_error(calls[i], cfg, reason, phase=phase, error=error, details=details,
                                 kind=kind, code=code)
        _end(cfg, emit, calls[i], results[i])

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
            reject(i, "Operation aborted.")
            continue
        if msg.stop_reason == "length":
            reject(
                i,
                "The response hit the output token limit, so these tool arguments may be truncated. "
                "Re-issue the call (split large content into smaller steps).",
            )
            continue
        tool = tools.get(call.name)
        if tool is None:
            reject(i, f"Unknown tool: {call.name}. Available: {', '.join(sorted(tools))}")
            continue
        if not isinstance(call.arguments, dict) or INVALID_ARGUMENTS in call.arguments:
            reject(i, "Tool arguments were not a valid JSON object. Re-issue the call.")
            continue
        args = dict(call.arguments)
        try:
            if tool.prepare_arguments:
                args = tool.prepare_arguments(args)
            args, errs = validate(args, tool.parameters)
        except Exception as e:  # noqa: BLE001
            reject(i, f"Invalid arguments:\n- {type(e).__name__}: {safe_text(e)}", phase="tool.prepare", error=e)
            continue
        if errs:
            reject(i, "Invalid arguments:\n" + "\n".join(f"- {e}" for e in errs), phase="tool.prepare")
            continue
        ev = ToolCallEvent(call.id, call.name, args, tool, cfg.name, cfg.cwd, signal=signal)
        if cfg.before_tool_call:
            try:
                block = cfg.before_tool_call(ev)
            except Exception as e:  # noqa: BLE001
                reject(i, f"Tool call hook failed: {safe_text(e)}", error=e)
                continue
            if block is not None:
                reject(i, block.reason, details=block.details, kind="blocked")
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
                results[i] = _tool_error(calls[i], cfg, f"Execution permission check failed: {safe_text(e)}",
                                         phase="tool.permission", error=e)
                return
            if block is not None:
                results[i] = _tool_error(calls[i], cfg, block.reason, phase="tool.permission",
                                         details=block.details, kind="blocked")
                return
            if signal.is_set():
                results[i] = ToolResult.error("Operation aborted.")
                return
        ctx = ToolContext(ev.call_id, ev.cwd, signal, cfg.name, review_source=ev.review_source)
        start = time.monotonic()
        try:
            out = ev.tool.execute(ev.args, ctx)
            res = ToolResult.text(out) if isinstance(out, str) else out
            if not isinstance(res, ToolResult):
                raise TypeError(f"Tool {ev.tool_name} returned {type(res).__name__}; expected ToolResult or str.")
        except Exception as e:  # noqa: BLE001
            res = _tool_error(calls[i], cfg, f"{type(e).__name__}: {safe_text(e)}", phase="tool.execute", error=e)
        if not signal.is_set():
            res = _annotate_error(res, calls[i], cfg, phase="tool.execute")
        if cfg.after_tool_call:
            previous = []
            if isinstance(res.details, dict):
                previous.extend(res.details.get("related_errors") or [])
                if res.details.get("error"):
                    previous.append(res.details["error"])
            try:
                res = cfg.after_tool_call(ev, res) or res
                if not isinstance(res, ToolResult):
                    raise TypeError(f"Tool result hook returned {type(res).__name__}; expected ToolResult or None.")
            except Exception as e:  # noqa: BLE001
                res = _tool_error(calls[i], cfg, f"Tool result hook failed: {safe_text(e)}",
                                  phase="tool.result_hook", error=e)
            if not signal.is_set():
                res = _annotate_error(res, calls[i], cfg, phase="tool.result_hook")
            _preserve_errors(res, previous)
        res.details = res.details if res.details is not None else {}
        if isinstance(res.details, dict):
            res.details.setdefault("duration", round(time.monotonic() - start, 3))
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
            for f in as_completed(futures):
                f.result()
                i = futures[f]
                _end(cfg, emit, calls[i], results[i])
    # Model history still pairs results in call order; UI events above follow completion order.
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
