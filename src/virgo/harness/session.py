"""AgentSession: wires the LLM layer, the agent loop, the session store and extensions together.

This is the "integrated harness": every feature plugs into one loop through extension events.
Per model call, in order:
  1. steering messages (notifications, teammate messages) are appended by the loop
  2. auto-compaction check (at a turn boundary)
  3. `context` handlers build the request view; plan mode restricts instructions and tools
  4. stream with retries (backoff, optional fallback model) and one reactive compaction on overflow
Per tool call: ordered extensions and explicit permission preflight -> execution recheck -> tool -> results.
When the agent would stop: follow-ups (schedules), then `agent_stop` handlers (todo, goal, hooks).
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from collections import deque
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .. import ai
from ..agent import Agent, AgentConfig, AgentEvent, AgentTool, Block, ToolCallEvent, ToolContext, ToolResult
from ..ai.auth import AuthStore
from ..ai.compaction import compatible_context, context_binding, is_native_context
from ..ai.model_metadata import cache_path, clean_metadata, read_catalog, write_metadata
from ..ai.models import base_url
from ..ai.stream import EventStream, StreamEvent
from ..ai.transform import strip_thinking
from ..ai.types import (
    AssistantMessage,
    Context,
    StreamOptions,
    TextContent,
    ToolResultMessage,
    UserMessage,
    message_to_dict,
)
from ..diagnostics import capture_error, emergency_write, error_summary, safe_text
from ..features.permission_context import ReviewSource
from ..i18n import Translator, normalize_language
from ..tools import BASE_TOOLS
from ..tools.shell import Shell
from ..tools.truncate import MAX_BYTES, budget
from . import compaction
from .commands import Commands, expand_template, load_templates, register_builtins
from .diagnostic_view import project_tool_result
from .diagnostics import DiagnosticLog
from .extensions import (
    ROLES_ALL,
    EventBus,
    Handled,
    install_command_hooks,
    load_builtin_features,
    load_user_extensions,
)
from .permissions import NoPermissions, PermissionPolicy, normalize_permission_mode, plan_tool_allowed
from .prompt import RuntimeContext, build_system
from .recovery import RuntimeDiagnostics, message_diagnostics
from .retry import retry_after_seconds
from .settings import Paths, Settings
from .shell_commands import ShellSelection
from .store import SessionSaveError, SessionStore
from .telemetry import record_call
from .ui import UI

_DEFAULT_EFFORT = object()
_TRANSCRIPT_STREAM_TYPES = {
    "text_start", "thinking_start", "toolcall_start",
    "native_tool_start", "native_tool_delta", "native_tool_end", "retry",
}


class Runtime:
    """One conversation (the lead agent, a teammate, or a sub-agent) and its model-call policy."""

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
        max_turns: int | None = None,
        signal: threading.Event | None = None,
        compaction: bool = True,
        review_parent: ReviewSource | None = None,
        accounting_store=None,
    ):
        self.session = session
        self.name = name
        self.role = role
        self.cwd = Path(cwd or session.cwd)
        self.store = store
        self.accounting_store = accounting_store if accounting_store is not None else (store if role == "main" and store is not None else session.store)
        if role != "main" and review_parent is None:
            review_parent = getattr(getattr(session, "main", None), "review_source", None)
        if review_parent is not None:
            review_parent.root.entries()  # Capture the task branch before a child can be queued or switched away.
            self.accounting_store = review_parent.root
        self.construction_context = RuntimeContext(role, self.cwd, store, self.accounting_store, review_parent)
        self.ids: list = list(ids or [])
        self.model = model or session.model
        self.system_extra = system_extra
        self.tools = tools if tools is not None else session.tools_for(role)
        self._dynamic_tools = tools is None
        self._allowed_tool_names = {tool.name for tool in self.tools}
        self.compaction = compaction
        self.compacted_at = 1 if messages and (
            getattr(messages[0], "kind", None) == "summary" or is_native_context(messages[0])
        ) else 0
        self._native_source: list | None = None
        self._call_started: float | None = None  # set per model call; None for loop-made messages
        self.run_id: str | None = None
        self._prepared_run = False
        self._model_call_id: str | None = None
        self._attempt_number = 0
        self.wakeups: deque = deque()
        self.diagnostic_id = uuid.uuid4().hex
        self.recovery = RuntimeDiagnostics(self, messages or ())
        self.diagnostic_access = self.recovery.access
        self.native_binding = (store.last_compaction() or {}).get("context_binding") if store else None
        self.system = ""
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
            project_tool_result=self._project_tool_result if session.settings.feature_enabled("diagnostics") else None,
            translator=session.translator,
        )
        self.agent = Agent(cfg, messages=list(messages or []), signal=signal)
        self.review_source = ReviewSource(self, review_parent)
        for message in self.agent.messages:
            self.review_source.remember(message)
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

    def rebuild_system(self) -> None:
        self.system = build_system(self.session, self.role, self.system_extra, self.cwd,
                                   tools=self.tools, context=self.construction_context)

    def can_run(self) -> bool:
        errors = [bound.save_error for bound in (self.store, self.accounting_store)
                  if bound is not None and bound.save_error is not None]
        for error in errors:
            self.session.report_save_error(error)
        return not errors

    def _before_tool(self, ev: ToolCallEvent):
        ev.review_source = self.review_source
        return self.session._before_tool(ev)

    def _project_tool_result(self, call, result):
        return project_tool_result(call, result,
                                   query_available=any(tool.name == "diagnostic_get" for tool in self.tools))

    def prepare_run(self) -> None:
        """Bind startup hooks to this run before its first model request exists."""
        self.run_id = uuid.uuid4().hex
        self._model_call_id = None
        self._attempt_number = 0
        self._prepared_run = True

    def _on_event(self, ev: AgentEvent) -> None:
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
        self.session.ui.on_event(ev)

    def diagnostic_context(self, **extra) -> dict:
        owner = self.store if self.store is not None else self.accounting_store
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
        details = self.session.report_error(
            error, phase=phase, store=self.store if self.store is not None else self.accounting_store,
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
            context = {
                "tool_name": ev.tool_name, "tool_call_id": ev.tool_call_id,
                **{key: details[key] for key in (
                    "duration", "exit_code", "status", "timeout", "execution", "transport",
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
        if ev.type in ("agent_start", "agent_end") and self.role == "main" and self.store is not None:
            self.store.append(
                "custom",
                kind="transcript_run_start" if ev.type == "agent_start" else "transcript_run_end",
                data={"agent": ev.agent},
            )
        elif (
            ev.type == "message_update" and self.role == "main" and self.store is not None
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
                entry_id = self.store.append_message(ev.message) if self.store else None
            except SessionSaveError as error:
                entry_id = error.entry["id"]
                self.session.report_save_error(error)
            self.ids.append(entry_id)
            self.review_source.remember(ev.message, self.ids[-1])
            if isinstance(ev.message, ToolResultMessage):
                self.recovery.safely("presented", message_diagnostics(ev.message))
            if self._native_source is not None:
                self._native_source.append(ev.message)
            if self.role != "main" and isinstance(ev.message, AssistantMessage) and self._call_started is not None:
                # Loop notices (turn limit, paused search) are not model calls.
                record_call(
                    self.accounting_store, self.model, ev.message, purpose="agent",
                    started=self._call_started, effort=self.session.effort, agent=self.name,
                )
                self._call_started = None

    def compact(self, focus: str | None = None, reason: str = "manual", *, model=None) -> dict | None:
        if not self.can_run():
            return None
        result = compaction.compact(self, focus=focus, reason=reason, model=model)
        if result is not None:
            self.recovery.after_compaction()
        return result

    def restore_native_context(self, model=None) -> bool:
        """Provider-owned state cannot replace portable history after a provider switch."""
        if compatible_context(model or self.model, self.messages):
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
        self._native_source = None
        self.native_binding = None
        return True

    def context_tokens(self) -> int:
        # Status refreshes must not invoke extension hooks or search approvals.
        context = self._base_context(self.messages)
        return compaction.estimate_tokens(
            context.messages, self.compacted_at, system=context.system, tools=context.tools,
        )

    def _compaction_due(self, model=None) -> bool:
        s = self.session.settings
        if not self.compaction or not s.get("compaction.enabled", True):
            return False
        trigger = s.get("compaction.triggerTokens")
        if trigger is None:
            trigger = int((model or self.model).context_window * float(s.get("compaction.triggerRatio", 0.8)))
        return self.context_tokens() >= trigger

    def _base_context(self, messages: list, *, report_errors: bool = False) -> Context:
        """Assemble instructions and schemas; only actual requests report formatting failures."""
        s = self.session
        permissions = s.services.get("permissions")
        mode = permissions.mode_for(self.name) if hasattr(permissions, "mode_for") else s.permission_mode
        system = self.system + "\n\n<language>\n" + s.t("harness.response_language") + "\n</language>"
        tools = []
        for tool in self.tools:
            if mode == "plan" and not plan_tool_allowed(tool):
                continue
            try:
                tools.append(tool.spec(s.translator))
            except Exception as error:
                # Extension-provided translation values are presentation data;
                # a broken formatter must not disable an otherwise usable tool.
                if report_errors:
                    self.report_error(error, phase="i18n.tool", context={"tool_name": tool.name})
                tools.append(tool.spec())
        if mode == "plan":
            system += (
                "\n\n<permission_mode>\n"
                "Plan mode is active: investigate and plan; file and system changes are not permitted. "
                "Planning task records are allowed.\n</permission_mode>"
            )
            for tool in tools:
                if tool.name == "bash":
                    tool.description = s.t("harness.plan_shell_description")
        return Context(system, messages, tools)

    def request_context(self, messages: list, model=None) -> Context:
        """Use the same instructions, tools and extension view for generation and compaction."""
        s = self.session
        view = s.bus.chain("context", list(messages), self)
        context = self._base_context(view, report_errors=True)
        from ..features.web import web_service

        web_service(s).prepare(self, context, model or self.model)
        if any(is_native_context(message) for message in messages) and self.native_binding != context_binding(model or self.model, context):
            context.messages = strip_thinking(view)
        return context

    # -- model calls ---------------------------------------------------------
    def call_model(self, messages: list, signal: threading.Event) -> EventStream:
        refresh = self.session.services.get("mcp_refresh")
        if refresh is not None and self.can_run() and not signal.is_set() and refresh(self):
            # Only MCP wrappers changed. Preserve runtime-local extension tools
            # and explicitly restricted child tool sets, including their objects.
            tools = [tool for tool in self.session.tools_for(self.role) if tool.name.startswith("mcp__")]
            if not self._dynamic_tools:
                tools = [tool for tool in tools if tool.name in self._allowed_tool_names]
            self.tools[:] = [tool for tool in self.tools if not tool.name.startswith("mcp__")] + tools
            self.rebuild_system()
        self._call_started = time.monotonic()
        self._model_call_id = uuid.uuid4().hex
        self._attempt_number = 0
        fallback = AssistantMessage(api=self.model.api, provider=self.model.provider, model=self.model.id)
        return EventStream(self._attempts(messages, signal), fallback)

    def _attempts(self, messages: list, signal: threading.Event):
        s = self.session
        if not self.can_run() or signal.is_set():
            yield StreamEvent("error", AssistantMessage(stop_reason="aborted"))
            return
        self.restore_native_context()
        if self._compaction_due():
            self.compact(reason="auto")
        max_retries = int(s.settings.get("retry.maxRetries", 3))
        wait_budget = max(0.0, float(s.settings.get("retry.maxWaitSeconds", 120)))
        retries = overloaded = 0
        overflow_done = False
        web_fallbacks = 0
        model = self.model
        while True:
            if not self.can_run() or signal.is_set():
                stopped = AssistantMessage(stop_reason="aborted")
                yield StreamEvent("error", stopped)
                return
            if self.restore_native_context(model) and self._compaction_due(model):
                self.compact(reason="auto", model=model)
            self.recovery.safely("before_request")
            if not self.can_run() or signal.is_set():
                yield StreamEvent("error", AssistantMessage(stop_reason="aborted"))
                return
            ctx = self.request_context(messages, model)
            opts = StreamOptions(signal=signal, max_tokens=s.settings.get("maxTokens"), effort=s.effort)
            if not self.can_run() or signal.is_set():
                stopped = AssistantMessage(stop_reason="aborted")
                yield StreamEvent("error", stopped)
                return
            request_model = model
            self._attempt_number += 1
            self._call_started = time.monotonic()
            failed: AssistantMessage | None = None
            for ev in ai.stream(model, ctx, opts):
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
            web = s.services.get("web")
            if (web_fallbacks < 2 and not signal.is_set() and web
                    and web.fallback(request_model, ctx, failed)):
                web_fallbacks += 1
                record_call(self.accounting_store, request_model, failed, purpose="fetch-fallback" if failed.error_kind == "fetch_unsupported" else "search-fallback",
                            started=self._call_started, duration=duration, effort=s.effort, agent=self.name)
                yield StreamEvent("retry", failed)
                continue
            if failed.search_started or failed.native_started:
                # Hosted work may already have been billed. Preserve its evidence
                # and let the user resume rather than silently replaying the turn.
                yield StreamEvent("error", failed)
                return
            if failed.error_kind == "overflow" and not overflow_done and self.compaction:
                overflow_done = True
                yield StreamEvent("retry", failed)
                if self.compact(reason="overflow", model=model) is not None:
                    record_call(self.accounting_store, request_model, failed, purpose="retry",
                                started=self._call_started, duration=duration, effort=s.effort, agent=self.name)
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
                            started=self._call_started, duration=duration, effort=s.effort, agent=self.name)
                continue
            yield StreamEvent("error", failed)
            return


class AgentSession:
    def __init__(
        self,
        cwd: str | Path = ".",
        *,
        ui: UI | None = None,
        model: str | None = None,
        session: str | Path | None = "new",  # "new" | "continue" | path | None (in memory)
        permission_mode: str | None = None,
        settings: dict | None = None,
        extensions: bool = True,
        home: str | Path | None = None,
        refresh_models: bool = True,
        shell_catalog: tuple[Shell, ...] | None = None,
    ):
        self.session_options = {
            "model": model,
            "permission_mode": permission_mode,
            "settings": settings,
            "extensions": extensions,
            "home": home,
            "refresh_models": False,
        }
        self.paths = Paths(Path(cwd), Path(home) if home else None)
        self.cwd = self.paths.cwd
        self.ui = ui or UI()
        self.diagnostics = DiagnosticLog(self.paths.user / "logs")
        self._diagnostic_lock = threading.RLock()
        self._notified_errors: set[str] = set()
        self.settings = Settings.load(self.paths, settings)
        self.translator = Translator(self.settings.get("language", "en"))
        self.ui.set_translator(self.translator)
        self.auth = AuthStore(self.paths.user / "auth.json")
        self.bus = EventBus(on_error=lambda m: self.ui.print(m, "error"), on_diagnostic=self._extension_error)
        self.commands = Commands(self.translator)
        self.services: dict = {}
        self.sections: list = []
        self.runtime_sections: list = []
        self.permission_policy: PermissionPolicy | None = None
        self._permission_boundary = None
        self._reported_save_errors: set[int] = set()
        self._failed_stores: list = []
        self._retained_sessions: dict[Path, tuple[Runtime, deque]] = {}
        self._main_runtimes: dict[str, Runtime] = {}
        self._tools: list[tuple[AgentTool, tuple]] = []
        self.templates: dict = {}
        self.permission_mode = permission_mode or self.settings.get("permissions.mode", "auto-review")
        self.effort = self.settings.get("effort", "medium")
        self._lock = threading.RLock()
        self._shutdown_started = False
        self.busy = False
        self.wakeups: deque = deque()
        self.discovered_models: dict[str, dict] = {}
        self._metadata_paths: dict[str, Path] = {}
        self._model_catalogs: dict[Path, list] = {}
        self._catalog_stop = threading.Event()
        self._catalog_refresh_done = threading.Event()
        self._metadata_pending = False
        self.model = self.resolve(model or self.settings.get("model"))
        self.small_model = self._small_model(self.model)

        for t in BASE_TOOLS:
            if t.name != "bash":
                self.register_tool(t, ROLES_ALL)
        self.services["shell"] = ShellSelection(self, shell_catalog)
        self.session_options["shell_catalog"] = self.services["shell"].catalog
        self.services["shell"].install_tools()
        register_builtins(self)
        self.store = self._open_store(session)
        self._visited_stores = [self.store]
        load_builtin_features(self)
        if extensions:
            load_user_extensions(self)
        if self.settings.feature_enabled("permission") and self.permission_policy is None:
            raise RuntimeError("Permission feature enabled but no policy was registered")
        if self.permission_policy is None:
            self.permission_policy = NoPermissions()
        install_command_hooks(self)
        self.templates = load_templates(self.paths.both("prompts"))
        self.main = self._new_main(self.store)
        self.main.wakeups = self.wakeups
        for record in self.diagnostics.recent():
            context = record.get("context", {})
            if context.get("session_id") == self.store.id and context.get("agent") in (None, "main"):
                self.main.recovery.safely("queue", record)
        self.bus.emit("session_start", self)
        if refresh_models:
            from .preferences import start_model_refresh

            start_model_refresh(self)
        else:
            self._catalog_refresh_done.set()

    # -- diagnostics -----------------------------------------------------------
    def report_error(self, error, *, phase: str, context=None, store=None,
                     notify=True, kind="fatal", code=None) -> dict:
        """Record a failure independently of session writes, then show its reference.

        Re-reporting an exception at a CLI boundary keeps its original ID and
        stack. Explicit owners prevent background work following a session switch.
        """
        details = None
        try:
            if isinstance(error, dict):
                details = error
            else:
                try:
                    details = getattr(error, "_virgo_error_details", None)
                except Exception:
                    pass
            details = dict(details) if isinstance(details, dict) else capture_error(
                error, phase=phase, kind=kind, code=code,
            )
            owner = store if store is not None else getattr(self, "store", None)
            base = {"cwd": str(self.cwd)}
            if owner is not None:
                base.update(session_id=owner.id, session_file=str(owner.path) if owner.path else None)
            details["context"] = {**base, **(details.get("context") or {}), **(context or {})}
            details = self.diagnostics.record(details)
            if isinstance(error, BaseException):
                try:
                    error._virgo_error_details = details
                except Exception:
                    pass
            if notify:
                with self._diagnostic_lock:
                    fresh = details["error_id"] not in self._notified_errors
                    self._notified_errors.add(details["error_id"])
                if fresh:
                    try:
                        self.ui.print(error_summary(details, t=self.t), "error")
                    except Exception as reporting_error:
                        emergency_write(details, reporting_error)
            return details
        except Exception as reporting_error:
            # Do not rely on the failing session store, UI or logger to report
            # their own failures. Preserve an already captured original error.
            if not isinstance(details, dict):
                details = capture_error(error, phase=phase, kind=kind, context=context, code=code)
            details = {**details, "logging_error": safe_text(reporting_error)}
            emergency_write(details, reporting_error)
            return details

    def _extension_error(self, details: dict, args: tuple) -> None:
        runtime = None
        for value in args:
            if isinstance(value, Runtime):
                runtime = value
                break
            if isinstance(value, ToolCallEvent) and value.review_source is not None:
                runtime = value.review_source.runtime
                break
        if runtime is not None:
            runtime.report_error(details, phase="extension.handler")
        elif any(value is self for value in args) and getattr(self, "main", None) is not None:
            # Session lifecycle events carry their owning session, not a child
            # accounting store. The currently active main owns this dispatch.
            self.main.report_error(details, phase="extension.handler")
        else:
            self.report_error(details, phase="extension.handler")

    # -- setup helpers ---------------------------------------------------------
    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @permission_mode.setter
    def permission_mode(self, mode: str) -> None:
        self._permission_mode = normalize_permission_mode(mode)

    def resolve(self, spec: str) -> ai.Model:
        with self._lock:
            return self._resolve(spec)

    def _resolve(self, spec: str) -> ai.Model:
        providers = self.settings.get("providers", {})
        candidate = ai.resolve_model(spec, providers)
        path = cache_path(self.paths.user, candidate)
        if self._metadata_paths.get(candidate.provider) != path:
            self._replace_metadata(candidate.provider, {item.id: item.metadata for item in self.cached_model_catalog(candidate)})
            self._metadata_paths[candidate.provider] = path
        if metadata := self.discovered_models.get(candidate.spec):
            provider, model_id = candidate.provider, candidate.id
            config = providers.get(provider, {})
            models = config.get("models") or {}
            names = {
                "context_window": "contextWindow", "max_tokens": "maxTokens", "reasoning": "reasoning", "vision": "vision",
                "supported_reasoning_levels": "supportedReasoningLevels",
                "default_reasoning_level": "defaultReasoningLevel",
            }
            live = {names[key]: value for key, value in metadata.items() if key in names}
            providers = {**providers, provider: {**config, "models": {**models, model_id: {**live, **models.get(model_id, {})}}}}
        m = ai.resolve_model(spec, providers)
        m.auth_path = str(self.auth.path)
        if m.api == "anthropic":
            for k in ("fallbacks", "thinkingDisplay", "thinkingBinding"):
                m.options.setdefault(k, self.settings.get(f"anthropic.{k}"))
        return m

    def _replace_metadata(self, provider: str, models: dict) -> None:
        self.discovered_models = {
            spec: meta for spec, meta in self.discovered_models.items() if not spec.startswith(provider + "/")
        }
        self.discovered_models.update({f"{provider}/{key}": clean_metadata(value) for key, value in models.items()})

    def cached_model_catalog(self, connection: ai.Model) -> list:
        with self._lock:
            path = cache_path(self.paths.user, connection)
            if path not in self._model_catalogs:
                self._model_catalogs[path] = read_catalog(path)
            return list(self._model_catalogs[path])

    def remember_model_metadata(self, connection: ai.Model, models: list, *, diagnostic_store=None) -> None:
        with self._lock:
            path = cache_path(self.paths.user, connection)
            self._model_catalogs[path] = list(models)
            self._replace_metadata(connection.provider, {item.id: item.metadata for item in models})
            self._metadata_paths[connection.provider] = path
            try:
                write_metadata(path, models)
            except OSError as error:
                self.report_error(error, phase="catalog.save", store=diagnostic_store, context={"path": str(path)})
                self.ui.print(self.t("harness.models.save_failed"), "warn")
            self._metadata_pending = True
            if not self.busy:
                self._apply_model_metadata()

    def _apply_model_metadata(self) -> None:
        if not self._metadata_pending:
            return
        model = self.resolve(self.model.spec)
        # An unrelated catalog refresh must not invalidate prepared request
        # bindings by replacing an otherwise unchanged runtime model object.
        if model != self.model:
            self.model = self.main.model = model
        self.small_model = self.model if self.small_model.spec == self.model.spec else self.resolve(self.small_model.spec)
        self._metadata_pending = False

    def _small_model(self, model: ai.Model) -> ai.Model:
        host = urlsplit(base_url(model)).hostname
        if model.api == "anthropic" and (
            model.provider == "anthropic" or host == "api.anthropic.com"
        ):
            return self.resolve(f"{model.provider}/claude-haiku-5-5")
        is_openai = model.provider in {"openai", "openai-codex"} or model.api == "openai-codex"
        if model.api in {"openai", "openai-responses", "openai-codex"} and (
            is_openai or host == "api.openai.com"
        ):
            return self.resolve(f"{model.provider}/gpt-6-luna")
        return model

    @property
    def small_model_effort(self) -> str | None:
        model = self.small_model
        if not model.reasoning:
            return None
        if "low" in (model.supported_reasoning_levels or ()):
            return "low"
        default = model.default_reasoning_level
        if model.supported_reasoning_levels is None or default in model.supported_reasoning_levels:
            return default
        return None

    @property
    def small_model_effort_fallback_reason(self) -> str | None:
        model = self.small_model
        if not model.reasoning:
            return "model does not support reasoning controls"
        selected = self.small_model_effort
        if selected == "low":
            return None
        if selected is not None:
            return "low is unavailable; using the declared model default"
        if model.supported_reasoning_levels is None:
            return "reasoning capabilities are unknown; using the server default"
        return "low is unavailable and no valid model default is declared; using the server default"

    def complete(self, purpose: str, system: str, prompt: str, *, model=None,
                 effort=_DEFAULT_EFFORT, max_tokens=None, total_max_tokens=None, signal=None, agent="main", tool_call_id=None,
                 accounting_store=None, call_metadata=None):
        """Account for a tool-free request separately from the conversation context."""
        if effort is _DEFAULT_EFFORT:
            effort = self.small_model_effort if model is None else None
        uses_small_default = model is None and effort == self.small_model_effort
        requested_effort = "low" if uses_small_default else effort
        fallback_reason = self.small_model_effort_fallback_reason if uses_small_default else None
        model = model or self.small_model
        # A caller may bind a background request to its original conversation before a session switch.
        store = accounting_store if accounting_store is not None else self.store
        if store.save_error is not None:
            self.report_save_error(store.save_error)
            raise store.save_error
        started = time.monotonic()
        diagnostic_context = {
            "agent": agent, "purpose": purpose, "tool_call_id": tool_call_id,
            "model": model.spec, "api": model.api, "model_call_id": uuid.uuid4().hex, "attempt": 1,
        }
        try:
            message = ai.complete(model, system, prompt, max_tokens=max_tokens, total_max_tokens=total_max_tokens,
                                  effort=effort, signal=signal)
        except Exception as error:
            self.report_error(error, phase="model.auxiliary", store=store, context=diagnostic_context)
            raise
        if message.stop_reason == "error":
            details = message.error_details or capture_error(
                message.error_message or "The auxiliary model request failed.", phase="model.auxiliary",
                kind=message.error_kind or "fatal", code="MODEL_REQUEST_FAILED",
            )
            message.error_message = safe_text(message.error_message or details["message"])
            message.error_details = self.report_error(
                details, phase="model.auxiliary", store=store,
                context={**diagnostic_context, "duration": round(time.monotonic() - started, 6)},
            )
        metadata = getattr(message, "request_metadata", None) or ai.completion_request_metadata(
            model, effort, ai.completion_token_limit(model, max_tokens, total_max_tokens)
        )
        metadata = {**metadata, "requested_effort": requested_effort, "effort_fallback_reason": fallback_reason}
        if total_max_tokens is not None:
            metadata["requested_max_tokens"] = total_max_tokens
        if effort is not None and metadata["effective_effort"] != effort:
            metadata["effort_fallback_reason"] = (
                f"adapter maps {effort!r} to {metadata['effective_effort']!r}"
            )
        record_call(store, model, message, purpose=purpose, started=started, effort=effort,
                    agent=agent, tool_call_id=tool_call_id, request_metadata=metadata,
                    call_metadata=call_metadata)
        return message

    def _open_store(self, session) -> SessionStore:
        if session is None:
            return SessionStore(None, self.cwd)
        if session == "continue":
            items = SessionStore.list(self.paths.sessions) if self.paths.sessions.is_dir() else []
            return (
                SessionStore.load(items[0]["path"])
                if items
                else SessionStore.new(self.paths.sessions, self.cwd)
            )
        if session == "new":
            return SessionStore.new(self.paths.sessions, self.cwd)
        return SessionStore.load(Path(session))

    def create_runtime(self, name: str, role: str, **kwargs) -> Runtime:
        with self._lock:
            parent = kwargs.get("review_parent")
            if role != "main" and parent is None:
                parent = self.main.review_source
                kwargs["review_parent"] = parent
            if parent is not None:
                parent.root.entries()
            kwargs.setdefault("accounting_store", kwargs.get("store") if role == "main" else parent.root)
            try:
                runtime = Runtime(self, name, role, **kwargs)
                if role == "main" and runtime.store is not None:
                    self._main_runtimes[runtime.store.id] = runtime
                return runtime
            except Exception as error:
                owner = kwargs.get("store") or kwargs.get("accounting_store")
                self.report_error(error, phase="runtime.create", store=owner,
                                  context={"agent": name, "role": role})
                raise

    def runtime_for_context(self, ctx: ToolContext) -> Runtime | None:
        """Resolve a trusted caller without borrowing the currently selected runtime."""
        source = ctx.review_source
        runtime = source.runtime if isinstance(source, ReviewSource) else None
        if (not isinstance(runtime, Runtime) or runtime.session is not self
                or source is not runtime.review_source or ctx.agent != runtime.name):
            return None
        return runtime

    def _new_main(self, store: SessionStore) -> Runtime:
        messages, ids = store.build_context()
        return self.create_runtime("main", "main", store=store, messages=messages, ids=ids)

    def _prepare_main(self, store: SessionStore) -> Runtime:
        return self._new_main(store)

    def live_store(self, path: str | Path) -> SessionStore | None:
        """Resolve an owned conversation before consulting its possibly damaged file."""
        path = Path(path).absolute()
        with self._lock:
            if self.store.path == path:
                return self.store
            retained = self._retained_sessions.get(path)
            return retained[0].store if retained is not None else None

    def retained_stores(self) -> tuple[SessionStore, ...]:
        with self._lock:
            return tuple(runtime.store for runtime, pending in self._retained_sessions.values()
                         if runtime.store.entries or pending)

    def list_sessions(self) -> list[dict]:
        """Merge this workspace's disk catalog with recoverable in-process history."""
        items = {item["path"]: item for item in SessionStore.list(self.paths.sessions)}
        with self._lock:
            stores = self.retained_stores()
            if self.store.save_error is not None:
                stores = (*stores, self.store)
            for store in stores:
                items[store.path] = store.describe()
        return sorted(items.values(), key=lambda item: (-item["mtime"], str(item["path"])))

    def open_session(self, path: str | Path) -> None:
        path = Path(path).absolute()
        with self._lock:
            if self.store.path == path:
                return
            retained = self._retained_sessions.get(path)
            if retained is not None:
                runtime, pending = retained
                self._activate_store(runtime.store, runtime=runtime, pending=pending)
                return
            self._activate_store(SessionStore.load(path))

    def new_session(self) -> None:
        self._activate_store(SessionStore.new(self.paths.sessions if self.store.path else None, self.cwd))

    def trash_session(self, path: str | Path) -> None:
        """Trash a conversation, replacing the active one with a new saved session."""
        path = Path(path).absolute()
        with self._lock:
            if self._shutdown_started:
                return
            stores = [store for store in self._visited_stores if store.path == path]
            if not stores:
                stores = [SessionStore.load(path)]
            current = self.store in stores
            if current:
                replacement = SessionStore.new(self.paths.sessions, self.cwd)
                runtime = self._prepare_main(replacement)
            # A conversation may have been reopened more than once. Stop all
            # retained stores from recreating it, including late extension writes.
            try:
                with ExitStack() as locks:
                    for store in stores:
                        locks.enter_context(store._lock)
                    stores[0].trash()
                    for store in stores[1:]:
                        store.path = None
            except BaseException:
                if current:
                    # The prepared replacement was never activated. Do not
                    # retain a notification destination for a failed deletion.
                    self._main_runtimes.pop(replacement.id, None)
                raise
            self._retained_sessions.pop(path, None)
            for store in stores:
                self._main_runtimes.pop(store.id, None)
            if current:
                self._activate_store(replacement, runtime=runtime)

    def _activate_store(self, store: SessionStore, *, runtime: Runtime | None = None,
                        pending: deque | None = None) -> None:
        with self._lock:
            if self._shutdown_started:
                return
            runtime = runtime if runtime is not None else self._prepare_main(store)
            if self._shutdown_started:
                return
            old = self.main
            # Background accounting can fail after we leave, even when there
            # were no queued wakeups. Keep the original owner, not a fresh disk copy.
            if old.store.path is not None:
                self._retained_sessions[old.store.path] = (old, self.wakeups)
            self._retained_sessions.pop(store.path, None)
            self.wakeups = pending if pending is not None else deque()
            runtime.wakeups = self.wakeups
            runtime.model = self.model
            self.store = store
            self.main = runtime
            if store not in self._visited_stores:
                self._visited_stores.append(store)
            self.bus.emit("session_start", self)

    def set_model(self, spec: str) -> None:
        with self._lock:
            model = self.resolve(spec)
            small_model = self._small_model(model)
            self.model = self.main.model = model
            self.small_model = small_model
            self.store.append("model_change", model=self.model.spec)

    def t(self, key: str, /, **values: object) -> str:
        return self.translator(key, **values)

    def set_language(self, language: str, *, persist: bool = True) -> None:
        """Apply a session language only after its requested preference is saved."""
        code = normalize_language(language)
        with self._lock:
            if self.busy:
                raise ValueError(self.t("language.busy"))
            if persist:
                self.settings.save_user({"language": code})
            else:
                self.settings.set("language", code)
            self.translator.set_language(code)
            self.session_options["settings"] = {**(self.session_options.get("settings") or {}), "language": code}
        try:
            self.ui.language_changed()
        except Exception as error:
            # The preference is already committed. A custom client's refresh
            # failure must not be reported as a failed save or undo the choice.
            self.report_error(error, phase="ui.language_changed")

    # -- tools -------------------------------------------------------------------
    def register_tool(self, tool: AgentTool, roles: tuple = ("main",)) -> None:
        """Register a tool for the given roles; replaces a same-named tool for those roles only."""
        kept = []
        for t, r in self._tools:
            if t.name == tool.name:
                r = tuple(x for x in r if x not in roles)
            if r:
                kept.append((t, r))
        self._tools = [*kept, (tool, tuple(roles))]

    def unregister_tool(self, name: str) -> None:
        """Remove a tool from every role, e.g. when the service behind it is replaced."""
        self._tools = [(t, r) for t, r in self._tools if t.name != name]

    def tools_for(self, role: str) -> list[AgentTool]:
        # Features use object identity to distinguish builtin tools from overrides.
        # Localize copies only when projecting ToolSpec for a model request.
        return [t for t, roles in self._tools if role in roles]

    def tool(self, name: str, role: str = "main") -> AgentTool | None:
        return next((t for t in self.tools_for(role) if t.name == name), None)

    def register_permission_policy(self, policy: PermissionPolicy) -> None:
        if not callable(getattr(policy, "preflight", None)) or not callable(getattr(policy, "before_execute", None)):
            raise TypeError("Policy requires preflight(event) and before_execute(event)")
        if self._permission_boundary is None:
            self._permission_boundary = self.bus.boundary(100)
        self.permission_policy = policy
        self.services["permissions"] = policy

    def _persistence_block(self, ev):
        runtime = ev.review_source.runtime if ev.review_source is not None else self.main
        return None if runtime.can_run() else Block(self.t("harness.session.unsaved"))

    def _check_policy(self, method, ev):
        if block := self._persistence_block(ev):
            return block
        try:
            policy = getattr(ev, "_permission_policy", self.permission_policy)
            result = getattr(policy, method)(ev)
            return result or self._persistence_block(ev)
        except Exception as error:
            runtime = ev.review_source.runtime if ev.review_source is not None else self.main
            details = runtime.report_error(error, phase=f"permission.{method}", context={
                "tool_name": ev.tool_name, "tool_call_id": ev.call_id,
                "policy": type(policy).__qualname__,
            })
            return Block(self.t("harness.permission.failed", error=safe_text(error)), details={"error": details})

    def _before_tool(self, ev: ToolCallEvent):
        if block := self._persistence_block(ev):
            return block
        ev._permission_policy = self.permission_policy
        snapshot = self.bus.snapshot("tool_call")
        boundary = self._permission_boundary
        if boundary is None:
            return self.bus.first_snapshot("tool_call", snapshot, ev, self)
        before = tuple(item for item in snapshot if item[:2] < boundary)
        after = tuple(item for item in snapshot if item[:2] > boundary)
        result = self.bus.first_snapshot("tool_call", before, ev, self)
        if result is not None:
            return result
        result = self._check_policy("preflight", ev)
        return result if result is not None else self.bus.first_snapshot("tool_call", after, ev, self)

    def _before_tool_execute(self, ev: ToolCallEvent):
        return self._check_policy("before_execute", ev)

    def _after_tool(self, ev: ToolCallEvent, res: ToolResult) -> ToolResult:
        res = self.bus.chain("tool_result", res, ev, self)
        bounded = []
        for b in res.content:
            if isinstance(b, TextContent) and len(b.text.encode("utf-8")) > MAX_BYTES:
                b = TextContent(budget(b.text, ev.cwd, ev.tool_name, t=self.t))
            bounded.append(b)
        res.content = bounded
        return res

    # -- running -----------------------------------------------------------------
    def prompt(self, text: str) -> None:
        """Handle one line of user input without disguising persistence failures."""
        try:
            self._prompt(text)
        except SessionSaveError as error:
            self.report_save_error(error)
        except Exception as error:
            command = text[1:].split(" ", 1)[0] if text.startswith("/") else None
            self.report_error(error, phase="command.execute" if command else "session.prompt",
                              context={"command": command} if command else None)
            raise

    def _prompt(self, text: str) -> None:
        result = self.bus.chain("input", text, self)
        if isinstance(result, Handled):
            if result.message:
                self.ui.print(result.message)
            return
        text = result
        if text.startswith("/") and not text.startswith("//"):
            name, _, args = text[1:].partition(" ")
            cmd = self.commands.get(name)
            if cmd is not None:
                cmd.fn(self, args)
                return
            if name in self.templates:
                text = expand_template(self.templates[name][1], args)
            else:
                self.ui.print(self.t("harness.commands.unknown", name=name), "error")
                return
        self.run([UserMessage(text)])

    def report_save_error(self, error: SessionSaveError) -> None:
        key = id(error.store)
        with self._lock:
            if key in self._reported_save_errors:
                return
            self._reported_save_errors.add(key)
            self._failed_stores.append(error.store)
        details = self.report_error(error, phase="session.save", store=error.store, notify=False,
                                    context={"entry_id": error.entry.get("id"), "entry_type": error.entry.get("type")})
        callback = getattr(self.ui, "save_failed", None)
        try:
            if callback is not None:
                callback(error.store.id, str(error.store.path), error_summary(details, t=self.t))
            else:
                self.ui.print(error_summary(details, t=self.t), "error")
        except Exception as reporting_error:
            emergency_write(details, reporting_error)

    @property
    def has_save_failure(self) -> bool:
        return bool(self._failed_stores) or any(store.save_error for store in self._visited_stores)

    def run(self, prompts: list) -> None:
        with self._lock:
            if self._shutdown_started:
                return
            rt = self.main
            if not rt.can_run():
                self.wakeups.extend(prompts)
                return
            self.busy = True
        wake = False
        try:
            rt.prepare_run()
            self.bus.emit("before_agent_start", rt, prompts)
            rt.agent.prompt(prompts)
        except Exception as error:
            rt.report_error(error, phase="session.run")
            raise
        finally:
            with self._lock:
                self.busy = False
                self._apply_model_metadata()
                left = rt.agent.drain_queues()
                if left and not self._shutdown_started:
                    self.wakeups.extend(left)
                    wake = rt.can_run()
            self.bus.emit("agent_end", rt)
        if wake:
            self.ui.wake()

    def run_pending(self) -> bool:
        """Run queued notifications, if any. Returns True if a run happened."""
        with self._lock:
            if self._shutdown_started or self.busy or not self.wakeups or not self.main.can_run():
                return False
            msgs = list(self.wakeups)
            self.wakeups.clear()
        self.run(msgs)
        return True

    def steer(self, text: str) -> bool:
        """Queue user input in an active run, or let the caller schedule it later.

        The busy check and enqueue share the same lock as run's final queue
        drain. Checking a UI worker's lifetime is insufficient: the agent may
        already have drained its queues, or the worker may be running a command.
        """
        with self._lock:
            if self._shutdown_started or not self.busy or not self.main.can_run():
                return False
            self.main.agent.steer(UserMessage(text))
        # Queued messages reach the model only after the current tool batch; this tells
        # features right away that the user is present.
        self.bus.emit("user_steer", text, self)
        return True

    def runtime_for_store(self, store) -> Runtime | None:
        """Find the originating lead, including an inactive or in-memory session."""
        return self._main_runtimes.get(store.id) if store is not None else self.main

    def retain_diagnostics(self, store, records) -> None:
        with self._lock:
            runtime = self.runtime_for_store(store)
            if self._shutdown_started or runtime is None:
                return
            for record in records:
                # Deletion uses the same lock. Recheck after obtaining an item,
                # too: an extension iterable may re-enter lifecycle operations.
                if self._main_runtimes.get(runtime.store.id) is not runtime:
                    return
                if isinstance(record, dict):
                    runtime.recovery.safely("queue", record)

    def notify(self, text: str, mode: str = "steer", source: str | None = None, *, store=None,
               diagnostics=()) -> None:
        """Deliver to the originating lead; never wake a different selected chat."""
        tag = f' source="{source}"' if source else ""
        msg = UserMessage([TextContent(f"<notification{tag}>\n{text}\n</notification>")], kind="notification")
        with self._lock:
            if self._shutdown_started:
                return
            runtime = self.runtime_for_store(store)
            if runtime is None:
                return
            self.retain_diagnostics(runtime.store, diagnostics)
            if self._main_runtimes.get(runtime.store.id) is not runtime:
                return
            if runtime is not self.main:
                runtime.wakeups.append(msg)
                return
            if self.busy:
                (runtime.agent.steer if mode == "steer" else runtime.agent.follow_up)(msg)
                return
            self.wakeups.append(msg)
        self.ui.wake()

    def abort(self) -> None:
        self.main.agent.abort()

    def compact(self, focus: str | None = None) -> dict | None:
        return self.main.compact(focus=focus, reason="manual")

    def context_tokens(self) -> int:
        return self.main.context_tokens()

    def wait_idle(self, timeout: float = 600.0) -> None:
        """Process notifications until nothing is pending (used by print mode)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.main.can_run():
                return
            if self.run_pending():
                continue
            busy = [b for b in self.bus.emit("pending_work", self) if b]
            if not busy and not self.wakeups:
                return
            time.sleep(0.1)

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self._catalog_stop.set()
            self.wakeups.clear()
        self.abort()
        try:
            self.bus.emit("session_shutdown", self)
        finally:
            self.cleanup_empty_stores()

    def cleanup_empty_stores(self) -> None:
        """Remove only empty stores visited by this process, including abandoned /new sessions."""
        for store in self._visited_stores:
            try:
                store.discard_if_empty()
            except OSError as error:
                self.report_error(error, phase="session.cleanup", store=store)
