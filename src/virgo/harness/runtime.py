"""One conversation's model-call, ownership and cancellation policy."""

from __future__ import annotations

import math
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .. import ai
from ..agent import Agent, AgentConfig, AgentEvent, AgentTool, ToolCallEvent
from ..ai.compaction import compatible_context, context_binding, is_native_context
from ..ai.stream import EventStream, StreamEvent, aborted_event
from ..ai.transform import strip_thinking
from ..ai.types import (
    AssistantMessage,
    Context,
    StreamOptions,
    ToolResultMessage,
    message_to_dict,
)
from ..diagnostics import capture_error, safe_text
from ..execution_policy import normalize_approval_policy
from ..tools.output import MAX_BYTES, MAX_LINES
from ..tools.shell import ShellTool
from . import compaction
from .diagnostic_view import project_tool_result
from .origin_context import ExecutionOrigin
from .prompt import RuntimeContext, build_system
from .recovery import RuntimeDiagnostics, message_diagnostics
from .runtime_limits import RequestBudget
from .shell_log_compaction import ShellLogHistory, ShellLogSettings
from .store import SessionSaveError, SessionStore
from .telemetry import record_call

if TYPE_CHECKING:
    from .session import AgentSession

_TRANSCRIPT_STREAM_TYPES = {
    "text_start", "thinking_start", "toolcall_start",
    "native_tool_start", "native_tool_delta", "native_tool_end", "retry",
}


def retry_after_seconds(details: dict | None, *, now: float | None = None) -> float | None:
    context = details.get("context", {}) if isinstance(details, dict) else {}
    http = context.get("http", {}) if isinstance(context, dict) else {}
    value = http.get("retry_after") if isinstance(http, dict) else None
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        if not isinstance(value, str):
            return None
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            seconds = max(0.0, date.timestamp() - (datetime.now(UTC).timestamp() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


class Runtime:
    """One conversation and its model-call, ownership and cancellation policy."""

    def __init__(
        self,
        session: AgentSession,
        name: str,
        role: str,
        *,
        cwd: Path | None = None,
        store: SessionStore | None = None,
        messages: list | None = None,
        ids: list | None = None,
        tools: list[AgentTool] | None = None,
        system_extra: str | None = None,
        model: ai.Model | None = None,
        effort: str | None = None,
        max_turns: int | None = None,
        signal: threading.Event | None = None,
        compaction: bool = True,
        origin_parent: ExecutionOrigin | None = None,
        accounting_store=None,
        request_budget: RequestBudget | None = None,
        approval_policy: str | None = None,
        execution_context=None,
        parent_tool_call_id: str | None = None,
        description: str = "",
        job_id: str | None = None,
    ):
        self.session = session
        self.request_budget = request_budget or RequestBudget(session.settings.get("budget.maxModelCalls"))
        self.name = name
        self.role = role
        self.parent_tool_call_id = parent_tool_call_id
        self.description = description
        self.job_id = job_id
        self.cwd = Path(cwd or session.cwd)
        self.store = store
        self.accounting_store = accounting_store if accounting_store is not None else (store if role == "main" and store is not None else session.store)
        if role != "main" and origin_parent is None:
            origin_parent = getattr(getattr(session, "main", None), "origin", None)
        self.parent = origin_parent.runtime if origin_parent is not None else None
        self.approval_policy = normalize_approval_policy(approval_policy) if approval_policy is not None else None
        if origin_parent is not None:
            origin_parent.root.entries()  # Capture the task branch before a child can be queued or switched away.
            self.accounting_store = origin_parent.root
        self.construction_context = RuntimeContext(role, self.cwd, store, self.accounting_store, origin_parent)
        self.ids: list = list(ids or [])
        self._model = model or session.model
        self._effort = effort
        self._reset_auto_compaction()
        self.system_extra = system_extra
        self.tools = tools if tools is not None else session.tools_for(role)
        self._dynamic_tools = tools is None
        self._allowed_tool_names = {tool.name for tool in self.tools}
        self._tool_version, registered = session.tool_snapshot(role)
        self._registered_tools = {tool.name: tool for tool in registered}
        self.compaction = compaction
        self.compacted_at = 1 if messages and (
            getattr(messages[0], "kind", None) == "summary" or is_native_context(messages[0])
        ) else 0
        self._native_source: list | None = None
        self._call_started: float | None = None  # set per model call; None for loop-made messages
        self.run_id: str | None = None
        self._prepared_run = False
        self.execution_active = False
        self._model_call_id: str | None = None
        self._last_request_header: dict | None = None
        self._attempt_number = 0
        self.wakeups: deque = deque()
        self.diagnostic_id = uuid.uuid4().hex
        self.recovery = RuntimeDiagnostics(self, messages or ())
        self.diagnostic_access = self.recovery.access
        self.native_binding = (store.last_compaction() or {}).get("context_binding") if store else None
        self.shell_logs = ShellLogHistory(self)
        self._request_shell_context: dict | None = None
        self.system = ""
        if execution_context is not None:
            self.execution_context = execution_context
        session.services["execution"].capture_runtime(self)
        self.rebuild_system()
        cfg = AgentConfig(
            tools=self.tools,
            call_model=self.call_model,
            name=name,
            cwd=self.cwd,
            before_tool_call=self._before_tool,
            before_tool_execute=session._before_tool_execute,
            after_tool_call=session._after_tool,
            should_stop=lambda _msgs: session.bus.first("agent_stop", self),
            max_turns=max_turns,
            can_run=self.can_run,
            on_error=lambda details: self.report_error(details, phase="agent.listener"),
            project_tool_result=self._project_tool_result,
            translator=session.translator,
        )
        self.agent = Agent(cfg, messages=list(messages or []), signal=signal)
        self.origin = ExecutionOrigin(self, origin_parent)
        self.agent.subscribe(self._on_event)
        for bound in (self.store, self.accounting_store):
            if bound is not None:
                with bound._lock:
                    if self.agent.signal not in bound.failure_signals:
                        bound.failure_signals.append(self.agent.signal)
                    if session.report_save_error not in bound.failure_listeners:
                        bound.failure_listeners.append(session.report_save_error)
                    if bound.save_error is not None:
                        self.agent.signal.set()

    @property
    def messages(self) -> list:
        return self.agent.messages

    @property
    def model(self) -> ai.Model:
        return self._model

    @model.setter
    def model(self, model: ai.Model) -> None:
        self._reset_auto_compaction(unless_model=model)
        self._model = model

    def rebuild_system(self) -> None:
        from dataclasses import replace

        self.construction_context = replace(self.construction_context, execution_context=self.execution_context)
        self.system = build_system(self.session, self.role, self.system_extra, self.cwd,
                                   tools=self.tools, context=self.construction_context, runtime=self)

    def effort_for(self, model: ai.Model) -> str | None:
        if self._effort is not None:
            return self._effort
        return (self.parent or self.session).effort_for(model)

    def can_run(self) -> bool:
        errors = [bound.save_error for bound in (self.store, self.accounting_store)
                  if bound is not None and bound.save_error is not None]
        for error in errors:
            self.session.report_save_error(error)
        return not errors

    def _before_tool(self, ev: ToolCallEvent):
        ev.origin = self.origin
        return self.session._before_tool(ev)

    def _project_tool_result(self, call, result):
        content = project_tool_result(call, result,
                                      query_available=any(tool.name == "diagnostic_get" for tool in self.tools))
        count = len(result.content)
        if len(content) == count or not any(
            isinstance(tool, ShellTool) and tool.name == call.name for tool in self.tools
        ):
            return content
        # The preview already budgets its exit status and output ID. Diagnostics use the remaining space.
        preview = result.as_text()
        bytes_left = MAX_BYTES - len(preview.encode())
        lines_left = MAX_LINES - len(preview.splitlines())
        bounded = []
        for block in reversed(content[count:]):
            size = len(block.text.encode()) + 1
            lines = len(block.text.splitlines())
            if size <= bytes_left and lines <= lines_left:
                bounded.append(block)
                bytes_left -= size
                lines_left -= lines
        return [*result.content, *reversed(bounded)]

    @property
    def permission_snapshot(self):
        return getattr(self.execution_context, "permission_snapshot", None)

    def refresh_permission_snapshot(self) -> None:
        """Capture the current selection for a new task, not an older run."""
        self.session.services["execution"].capture_runtime(self)

    def prepare_run(self) -> None:
        """Bind startup hooks to this run before its first model request exists."""
        self.refresh_permission_snapshot()
        self._length_recovery = None
        self.run_id = uuid.uuid4().hex
        self._model_call_id = None
        self._attempt_number = 0
        self._prepared_run = True
        self.execution_active = True

    def _on_event(self, ev: AgentEvent) -> None:
        if (
            self._awaiting_compaction_usage and ev.type == "message_end"
            and isinstance(ev.message, AssistantMessage) and ev.message.usage.total > 0
            and ev.message.stop_reason not in ("error", "aborted", "compaction")
            and self._auto_compaction_model is not None
            and self._auto_compaction_model[0] == f"{ev.message.provider}/{ev.message.model}"
        ):
            # Input measures the replacement prompt. This reply's output already counts as growth.
            baseline = ev.message.usage.input_total
            window = self._auto_compaction_model[1]
            keep = int(self.session.settings.get("compaction.keepRecentTokens", 20000))
            if window > 0:
                keep = min(max(0, keep), window // 4)
            growth = max(1, keep, min(baseline // 4, 2048))
            self._auto_compaction_floor = self._cap_compaction_floor(baseline + growth, window)
            self._awaiting_compaction_usage = False
        if ev.type == "agent_end":
            self.execution_active = False
        if ev.type == "agent_start":
            if not self._prepared_run:
                self.prepare_run()  # Children can call agent.prompt() without AgentSession.run().
            self._prepared_run = False
        self._diagnose_event(ev)
        try:
            self._record_event(ev)
        except SessionSaveError as error:
            self.session.report_save_error(error)
        self.can_run()
        self.session.bus.emit("agent_event", ev, self)
        if ev.type == "turn_end":
            self.session.bus.emit("turn_end", ev, self)
        if self.role == "main" or self.accounting_store is None or self.accounting_store.id == self.session.store.id:
            self.session.ui.on_event(ev)

    def diagnostic_context(self, **extra) -> dict:
        owner = self.store if self.role == "main" and self.store is not None else self.accounting_store
        return {
            "agent": self.name,
            "role": self.role,
            "session_id": owner.id if owner is not None else None,
            "accounting_session_id": self.accounting_store.id if self.accounting_store is not None else None,
            "run_id": self.run_id,
            "runtime_id": self.diagnostic_id,
            "model_call_id": self._model_call_id,
            "model": self.model.spec,
            "api": self.model.api,
            **extra,
        }

    def report_error(self, error, *, phase: str, context=None, notify=True, kind="fatal", code=None) -> dict:
        owner = self.store if self.role == "main" and self.store is not None else self.accounting_store
        details = self.session.report_error(
            error, phase=phase, store=owner,
            context=self.diagnostic_context(**(context or {})), notify=notify, kind=kind, code=code,
        )
        self.recovery.safely("remember", details)
        if notify and details.get("kind") != "aborted":
            self.recovery.safely("queue", details)
        return details

    def record_model_error(self, message, *, purpose="agent", notify=False, context=None) -> dict | None:
        if message.stop_reason != "error":
            return None
        details = message.error_details or capture_error(
            message.error_message or "The model request failed without an error message.",
            phase="model.request", kind=message.error_kind or "fatal", code="MODEL_REQUEST_FAILED",
        )
        message.error_message = safe_text(message.error_message or details["message"])
        message.error_details = self.report_error(
            details, phase="model.request", notify=notify,
            context={
                "purpose": purpose,
                "model": f"{message.provider}/{message.model}" if message.provider and message.model else self.model.spec,
                "api": message.api or self.model.api,
                "attempt": self._attempt_number,
                **(context or {}),
            },
        )
        return message.error_details

    def _diagnose_event(self, ev: AgentEvent) -> None:
        if ev.type == "message_end" and isinstance(ev.message, AssistantMessage):
            self.record_model_error(ev.message, notify=True)
        elif ev.type == "tool_execution_end" and ev.result is not None and isinstance(ev.result.details, dict):
            details = ev.result.details
            if not isinstance(details.get("error"), dict) and not details.get("related_errors"):
                return
            context = {
                "tool_name": ev.tool_name, "tool_call_id": ev.tool_call_id,
                **{key: details[key] for key in (
                    "duration", "exit_code", "status", "timeout", "execution", "server", "stderr",
                ) if key in details},
            }
            if isinstance(details.get("error"), dict):
                details["error"] = self.report_error(
                    details["error"], phase="tool.execute", context=context,
                    notify=details["error"].get("kind") != "blocked",
                )
            if details.get("related_errors"):
                details["related_errors"] = [
                    self.report_error(error, phase="tool.execute", context=context, notify=False)
                    for error in details["related_errors"] if isinstance(error, dict)
                ]
                if isinstance(details.get("error"), dict):
                    details["error"] = {
                        **details["error"],
                        "related_error_ids": [error["error_id"] for error in details["related_errors"]],
                    }
                    self.recovery.safely("remember", details["error"])

    def _record_event(self, ev: AgentEvent) -> None:
        if ev.type in ("agent_start", "agent_end") and self.store is not None:
            self.store.append(
                "custom",
                kind="transcript_run_start" if ev.type == "agent_start" else "transcript_run_end",
                data={"agent": ev.agent},
            )
        elif (
            ev.type == "message_update" and self.store is not None
            and ev.stream_event is not None and ev.stream_event.type in _TRANSCRIPT_STREAM_TYPES
        ):
            stream = ev.stream_event
            block = (
                asdict(stream.partial.content[stream.index])
                if stream.type != "retry" and 0 <= stream.index < len(stream.partial.content)
                else None
            )
            self.store.append(
                "custom",
                kind="transcript_stream",
                data={"agent": ev.agent, "stream_type": stream.type, "index": stream.index, "block": block},
            )
        elif ev.type == "tool_execution_end" and self.store is not None:
            # Results enter model history in call order, after the whole batch.
            # Retain their arrival order separately for replaying the transcript.
            result = ev.result
            message = ToolResultMessage(
                ev.tool_call_id or "",
                ev.tool_name or "tool",
                result.content if result is not None else [],
                details=result.details if result is not None else None,
                is_error=ev.is_error,
            )
            self.store.append(
                "custom",
                kind="transcript_tool_result",
                data={
                    "agent": ev.agent,
                    "tool_call_id": message.tool_call_id,
                    "tool_name": message.tool_name,
                    "args": ev.args,
                    "message": message_to_dict(message),
                },
            )
        elif ev.type == "message_end":
            try:
                entry_id = None
                if self.store is not None:
                    if isinstance(ev.message, AssistantMessage) and self._request_shell_context is not None:
                        entry_id = self.store.append(
                            "message", message=message_to_dict(ev.message), shell_log_context=self._request_shell_context,
                        )["id"]
                    else:
                        entry_id = self.store.append_message(ev.message)
            except SessionSaveError as error:
                entry_id = error.entry["id"]
                self.session.report_save_error(error)
            self.ids.append(entry_id)
            if isinstance(ev.message, ToolResultMessage):
                self.recovery.safely("presented", message_diagnostics(ev.message))
            if isinstance(ev.message, AssistantMessage):
                self._request_shell_context = None
            if self._native_source is not None:
                self._native_source.append(ev.message)
            if self.role != "main" and isinstance(ev.message, AssistantMessage) and self._call_started is not None:
                # Loop notices (turn limit, paused search) are not model calls.
                record_call(
                    self.accounting_store, self.model, ev.message, purpose="agent",
                    started=self._call_started, effort=self.effort_for(self.model), agent=self.name,
                )
                self._call_started = None

    @staticmethod
    def _cap_compaction_floor(floor: int, window: int) -> int:
        """Cap the growth floor to reserve 10% of a known window, up to 4096 tokens."""
        if window <= 0:
            return floor
        reserve = min(max(1, window // 10), 4096)
        return min(floor, max(1, window - reserve))

    def _reset_auto_compaction(self, *, unless_model: ai.Model | None = None) -> None:
        if unless_model is not None and self._auto_compaction_model == (unless_model.spec, unless_model.context_window):
            return
        self._auto_compaction_model: tuple[str, int] | None = None
        self._auto_compaction_floor = 0
        self._awaiting_compaction_usage = False
        self._length_recovery: Literal["pending", "exhausted"] | None = None

    def compact(self, focus: str | None = None, reason: str = "manual", *, model=None) -> dict | None:
        outcome = self._compact(focus, reason, model=model)
        return outcome if isinstance(outcome, dict) else None

    def _compact(self, focus: str | None = None, reason: str = "manual", *, model=None,
                 estimate=None) -> compaction.CompactOutcome:
        if not self.can_run():
            return None
        target_model = model or self.model
        if reason == "manual" and not self.agent.running:
            self.agent.signal.clear()
        self.restore_native_context(target_model)
        automatic = reason in ("auto", "overflow")
        concise = automatic and self._length_recovery is not None
        if concise:
            if self._length_recovery == "exhausted" or (
                reason == "auto" and estimate is None and not self._compaction_due(target_model)
            ):
                return None
            self._length_recovery = "exhausted"
        if estimate is None:
            estimate = self._checkpoint_estimate(target_model) if self.shell_logs.has_records else self.context_tokens()
        result = compaction.compact_context(self, focus, reason, model=target_model, concise=concise, estimate=estimate)
        self._record_compaction_decision(reason, estimate, result, target_model)
        if result == "length" and automatic and not concise:
            self._auto_compaction_model = (target_model.spec, target_model.context_window)
            self._length_recovery = "pending"
        if isinstance(result, dict):
            self._reset_auto_compaction()
            self.recovery.after_compaction()
            if reason == "auto":
                # Use a local floor until the provider measures the replacement prompt.
                after = self._checkpoint_estimate(target_model)
                self._auto_compaction_model = (target_model.spec, target_model.context_window)
                floor = after + max(1, min(after // 10, 1024))
                self._auto_compaction_floor = self._cap_compaction_floor(floor, target_model.context_window)
                self._awaiting_compaction_usage = True
        return result

    def restore_native_context(self, model=None) -> bool:
        """Provider-owned state cannot replace portable history after a provider switch."""
        target_model = model or self.model
        if compatible_context(target_model, self.messages):
            self._reset_auto_compaction(unless_model=target_model)
            return False
        if self.store is not None:
            messages, ids = self.store.build_full_context()
        elif self._native_source is not None:
            messages, ids = list(self._native_source), [None] * len(self._native_source)
        else:
            raise ValueError("Cannot restore original history for this native compacted context.")
        self.messages[:] = messages
        self.ids[:] = ids
        self.compacted_at = len(messages)
        self._reset_auto_compaction()
        self._native_source = None
        self.native_binding = None
        return True

    def context_tokens(self, *, estimate_only: bool = False, model=None) -> int:
        # Status refreshes must not invoke extension hooks or search approvals.
        context = self._base_context(self.messages)
        target = model or self.model
        if estimate_only and self.shell_logs.has_records and compatible_context(target, context.messages):
            context.messages = self.shell_logs.project(context, target).request_messages()
        valid_from = len(context.messages) if estimate_only else self.compacted_at
        return compaction.estimate_tokens(context.messages, valid_from, system=context.system, tools=context.tools)

    def _checkpoint_estimate(self, model) -> int:
        if self.shell_logs.has_records:
            return self.context_tokens(estimate_only=True, model=model)
        return self.context_tokens(estimate_only=True)

    def _compaction_due(self, model=None) -> bool:
        threshold = self._auto_compaction_threshold(model)
        return threshold is not None and self.context_tokens() >= threshold

    def _auto_compaction_threshold(self, model=None) -> int | None:
        target_model = model or self.model
        self._reset_auto_compaction(unless_model=target_model)
        s = self.session.settings
        if not self.compaction or not s.get("compaction.enabled", True):
            return None
        window = target_model.context_window
        if self._length_recovery is not None:
            return self._cap_compaction_floor(window, window) if self._length_recovery == "pending" and window > 0 else None
        trigger = s.get("compaction.triggerTokens")
        if trigger is None:
            trigger = int(window * float(s.get("compaction.triggerRatio", 0.8)))
        floor = self._cap_compaction_floor(self._auto_compaction_floor, window)
        return max(trigger, floor)

    def _base_context(self, messages: list, *, report_errors: bool = False) -> Context:
        """Assemble instructions and schemas; only actual requests report formatting failures."""
        s = self.session
        system = self.system + "\n\n<language>\n" + s.t("harness.response_language") + "\n</language>"
        tools = []
        execution_provider = s.services["execution"]
        filter_tools = getattr(execution_provider, "filter_tools", None)
        visible_tools = filter_tools(self, self.tools) if callable(filter_tools) else self.tools
        for tool in visible_tools:
            try:
                tools.append(tool.spec(s.translator))
            except Exception as error:
                # Extension-provided translation values are presentation data;
                # a broken formatter must not disable an otherwise usable tool.
                if report_errors:
                    self.report_error(error, phase="i18n.tool", context={"tool_name": tool.name})
                tools.append(tool.spec())
        if instructions := execution_provider.instructions(self):
            system += "\n\n" + instructions
        for _order, _name, section in sorted(s.request_sections, key=lambda item: item[0]):
            if instructions := section(self):
                system += "\n\n" + instructions
        return Context(system, messages, tools)

    def request_context(self, messages: list, model=None) -> Context:
        """Use the same instructions, tools and extension view for generation and compaction."""
        s = self.session
        view = s.bus.chain("context", list(messages), self)
        context = self._base_context(view, report_errors=True)
        web = s.services.get("web")
        if web is not None:
            web.prepare(self, context, model or self.model)
        if any(is_native_context(message) for message in messages) and self.native_binding != context_binding(model or self.model, context):
            context.messages = strip_thinking(view)
        return context

    # -- model calls ---------------------------------------------------------
    def refresh_tools(self) -> None:
        """Adopt registered tools at a request boundary while retaining local restrictions."""
        version, registered = self.session.tool_snapshot(self.role)
        if version == self._tool_version:
            return
        local = [tool for tool in self.tools if self._registered_tools.get(tool.name) is not tool
                 or (self.role != "main" and isinstance(tool, ShellTool))]
        local_names = {tool.name for tool in local}
        allowed = [tool for tool in registered if tool.name not in local_names and (
            self._dynamic_tools or tool.name in self._allowed_tool_names
        )]
        self.tools[:] = [*allowed, *local]
        self.agent.cfg.tools = self.tools
        self._registered_tools = {tool.name: tool for tool in registered}
        self._tool_version = version
        self.rebuild_system()

    def _record_request_header(self, model: ai.Model, options: StreamOptions) -> None:
        resolution = ai.resolved_effort(model, options.effort)
        header = {
            "model": model.id, "provider": model.provider, "api": model.api,
            "requested_effort": options.effort, "wire_effort": resolution.wire,
            "effective_effort": resolution.effective,
            **ai.output_limit_metadata(model, options.max_tokens),
        }
        if header != self._last_request_header:
            try:
                self.store.append("custom", kind="request_header", data=header)
            except SessionSaveError as error:
                self.session.report_save_error(error)
            self._last_request_header = header

    def _record_shell_log_decision(self, data, request_type, failure=None) -> None:
        if self.store is not None:
            self.store.append("custom", kind="compaction_decision", data={
                **data, "request_type": request_type, "run_id": self.run_id,
                "model_call_id": self._model_call_id, "failure_reason": failure if isinstance(failure, str) else None,
            })

    def _record_compaction_decision(self, reason, estimate, outcome, model) -> None:
        before = outcome["tokens_before"] if isinstance(outcome, dict) else estimate
        self._record_shell_log_decision({
            "layer": 3, "reason": reason, "model": model.spec, "candidate_count": 0, "shortened_count": 0,
            "estimated_before": before,
            "estimated_after": self._checkpoint_estimate(model) if isinstance(outcome, dict) else before,
        }, "compaction" if outcome is not None else "none", outcome or "no_compactable_context")

    def _persist_shell_notice(self, notice) -> None:
        self.messages.append(notice)
        self.agent._emit(AgentEvent("message_start", self.name, message=notice))
        self.agent._emit(AgentEvent("message_end", self.name, message=notice))

    def call_model(self, messages: list, signal: threading.Event) -> EventStream:
        if self.can_run() and not signal.is_set():
            self.session.bus.emit("runtime_request", self)
            self.refresh_tools()
        self._call_started = time.monotonic()
        self._model_call_id = uuid.uuid4().hex
        self._attempt_number = 0
        self._request_shell_context = None
        fallback = AssistantMessage(api=self.model.api, provider=self.model.provider, model=self.model.id)
        return EventStream(self._attempts(messages, signal), fallback)

    def _attempts(self, messages: list, signal: threading.Event):
        s = self.session
        max_retries = int(s.settings.get("retry.maxRetries", 3))
        wait_budget = max(0.0, float(s.settings.get("retry.maxWaitSeconds", 120)))
        retries = overloaded = 0
        overflow_done = False
        web_fallbacks = 0
        model = self.model
        checked_model = None
        while True:
            if not self.can_run() or signal.is_set():
                yield aborted_event(AssistantMessage())
                return
            model_key = (model.spec, model.context_window)
            check = (self.restore_native_context(model) or model_key != checked_model) and not overflow_done
            checked_model = model_key
            threshold = self._auto_compaction_threshold(model) if check else None
            config = ShellLogSettings.parse(s.settings.get("compaction.shellLogs")) if threshold is not None else None
            if config is not None and config.start_tokens > threshold:
                raise ValueError("compaction.shellLogs.startTokens exceeds the active automatic compaction threshold")
            ctx = None
            if config is not None or self.shell_logs.has_records:
                ctx = self.request_context(messages, model)
                projection = self.shell_logs.project(ctx, model, settings=config, threshold=threshold, measure=True)
                ctx.messages = projection.request_messages()
                estimate = projection.estimate if projection.estimate is not None else self.context_tokens()
                decision = projection.decision
            else:
                estimate = self.context_tokens() if threshold is not None else None
                decision = None
            if threshold is not None and estimate is not None and estimate >= threshold:
                outcome = self._compact(reason="auto", model=model, estimate=estimate)
                if decision is not None:
                    self._record_shell_log_decision(decision, "compaction", outcome)
                    decision = None
                if outcome == "cancelled":
                    yield aborted_event(AssistantMessage())
                    return
                if isinstance(outcome, dict):
                    ctx = None
            prior_count = len(messages)
            self.recovery.safely("before_request")
            if not self.can_run() or signal.is_set():
                yield aborted_event(AssistantMessage())
                return
            if ctx is None or len(messages) != prior_count:
                ctx = self.request_context(messages, model)
                projection = self.shell_logs.project(ctx, model, measure=self.shell_logs.has_records or config is not None)
                ctx.messages = projection.request_messages()
            opts = StreamOptions(
                signal=signal, max_tokens=s.settings.get("maxTokens"), effort=self.effort_for(model),
                prompt_cache_key=self.diagnostic_id, io_bridge=s.io_bridge,
                before_dispatch=self._record_request_header if self.role == "main" and self.store is not None else None,
            )
            if not self.can_run() or signal.is_set():
                yield aborted_event(AssistantMessage())
                return
            if projection.unavailable:
                self._persist_shell_notice(ctx.messages[-1])
                if not self.can_run() or signal.is_set():
                    yield aborted_event(AssistantMessage())
                    return
            request_model = model
            self.request_budget.consume()
            self._attempt_number += 1
            self._call_started = time.monotonic()
            failed: AssistantMessage | None = None
            with ai.stream(model, ctx, opts) as events:
                for ev in events:
                    if decision is not None and ev.type in {"done", "error"}:
                        failure = (ev.partial.error_kind or ev.partial.stop_reason) if ev.type == "error" else None
                        self._record_shell_log_decision(decision, "ordinary", failure)
                        decision = None
                    if ev.type == "error" and ev.partial.stop_reason == "error":
                        failed = ev.partial
                        break
                    yield ev
            if failed is None:
                return
            duration = time.monotonic() - self._call_started
            self.record_model_error(failed, context={
                "model": request_model.spec, "api": failed.api or request_model.api,
                "max_retries": max_retries, "duration": round(duration, 6),
            })
            if failed.native_started or overflow_done:
                # Hosted work may have started. The one overflow replay may also have been used.
                yield StreamEvent("error", failed)
                return
            web = s.services.get("web")
            if (web_fallbacks < 2 and not signal.is_set() and web
                    and web.fallback(request_model, ctx, failed)):
                web_fallbacks += 1
                record_call(self.accounting_store, request_model, failed, purpose="fetch-fallback" if failed.error_kind == "fetch_unsupported" else "search-fallback",
                            started=self._call_started, duration=duration, effort=opts.effort, agent=self.name)
                yield StreamEvent("retry", failed)
                continue
            if (failed.error_kind == "overflow" and self.compaction
                    and self._length_recovery != "exhausted" and not signal.is_set()):
                overflow_done = True
                yield StreamEvent("retry", failed)
                outcome = self._compact(reason="overflow", model=model)
                if isinstance(outcome, dict) or outcome == "cancelled":
                    record_call(self.accounting_store, request_model, failed, purpose="retry",
                                started=self._call_started, duration=duration, effort=opts.effort, agent=self.name)
                    if outcome == "cancelled":
                        yield aborted_event(AssistantMessage())
                        return
                    continue
            elif failed.error_kind == "retryable" and retries < max_retries and not signal.is_set():
                retries += 1
                text = (failed.error_message or "").lower()
                if "overload" in text or "529" in text:
                    overloaded += 1
                    fb = s.settings.get("retry.fallbackModel")
                    if overloaded >= 2 and fb and model.spec != fb:
                        model = s.resolve(fb)
                        s.ui.print(s.t("harness.retry.fallback", model=model.spec), "warn")
                delay = min(32.0, 0.5 * 2**retries) * (1 + random.random() * 0.25)
                details = failed.error_details or {}
                minimum_delay = retry_after_seconds(details)
                if minimum_delay is not None:
                    delay = max(delay, minimum_delay)
                if delay > wait_budget:
                    s.ui.print(s.t("harness.retry.budget"), "warn")
                    yield StreamEvent("error", failed)
                    return
                wait_budget -= delay
                reference = s.t("harness.error.id", id=details.get("error_id", s.t("common.unavailable")))
                if details.get("log_path"):
                    reference += "\n" + s.t("harness.diagnostics.path", path=details["log_path"])
                elif details.get("logging_error"):
                    reference += "\n" + s.t("harness.diagnostics.unsaved")
                s.ui.print(
                    s.t("harness.retry.wait", error=failed.error_message, delay=delay, retries=retries, maximum=max_retries) + "\n"
                    + safe_text(reference), "warn",
                )
                yield StreamEvent("retry", failed)
                if signal.wait(delay):
                    failed.stop_reason = "aborted"
                    yield StreamEvent("error", failed)
                    return
                record_call(self.accounting_store, request_model, failed, purpose="retry",
                            started=self._call_started, duration=duration, effort=opts.effort, agent=self.name)
                continue
            yield StreamEvent("error", failed)
            return


