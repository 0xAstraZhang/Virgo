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
from collections import deque
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .. import ai
from ..agent import Agent, AgentConfig, AgentEvent, AgentTool, Block, ToolCallEvent, ToolResult
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
from ..features.permission import normalize_permission_mode, plan_tool_allowed
from ..features.permission_context import ReviewSource
from ..tools import BASE_TOOLS
from ..tools.shell import Shell
from ..tools.truncate import MAX_BYTES, budget
from . import compaction
from .commands import Commands, expand_template, load_templates, register_builtins
from .extensions import (
    ROLES_ALL,
    EventBus,
    Handled,
    install_command_hooks,
    load_builtin_features,
    load_user_extensions,
)
from .permissions import NoPermissions, PermissionPolicy
from .prompt import RuntimeContext, build_system
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
        self.compaction = compaction
        self.compacted_at = 1 if messages and (
            getattr(messages[0], "kind", None) == "summary" or is_native_context(messages[0])
        ) else 0
        self._native_source: list | None = None
        self._call_started: float | None = None  # set per model call; None for loop-made messages
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

    def _on_event(self, ev: AgentEvent) -> None:
        try:
            self._record_event(ev)
        except SessionSaveError as error:
            self.session.report_save_error(error)
        self.can_run()
        self.session.bus.emit("agent_event", ev, self)
        if ev.type == "turn_end":
            self.session.bus.emit("turn_end", ev, self)
        self.session.ui.on_event(ev)

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
        return compaction.compact(self, focus=focus, reason=reason, model=model)

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

    def _base_context(self, messages: list) -> Context:
        """Assemble instructions and permitted tool schemas without request side effects."""
        s = self.session
        permissions = s.services.get("permissions")
        mode = permissions.mode_for(self.name) if hasattr(permissions, "mode_for") else s.permission_mode
        system = self.system
        tools = [t.spec() for t in self.tools if mode != "plan" or plan_tool_allowed(t)]
        if mode == "plan":
            system += (
                "\n\n<permission_mode>\n"
                "Plan mode is active: investigate and plan; file and system changes are not permitted. "
                "Planning task records are allowed.\n</permission_mode>"
            )
            for tool in tools:
                if tool.name == "bash":
                    tool.description = "Run a read-only shell command to inspect the project. Do not modify files or system state."
        return Context(system, messages, tools)

    def request_context(self, messages: list, model=None) -> Context:
        """Use the same instructions, tools and extension view for generation and compaction."""
        s = self.session
        view = s.bus.chain("context", list(messages), self)
        context = self._base_context(view)
        from ..features.web import web_service

        web_service(s).prepare(self, context, model or self.model)
        if any(is_native_context(message) for message in messages) and self.native_binding != context_binding(model or self.model, context):
            context.messages = strip_thinking(view)
        return context

    # -- model calls ---------------------------------------------------------
    def call_model(self, messages: list, signal: threading.Event) -> EventStream:
        self._call_started = time.monotonic()
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
            ctx = self.request_context(messages, model)
            opts = StreamOptions(signal=signal, max_tokens=s.settings.get("maxTokens"), effort=s.effort)
            if not self.can_run() or signal.is_set():
                stopped = AssistantMessage(stop_reason="aborted")
                yield StreamEvent("error", stopped)
                return
            request_model = model
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
                        s.ui.print(f"[switching to fallback model {model.spec}]", "warn")
                delay = min(32.0, 0.5 * 2**retries) * (1 + random.random() * 0.25)
                s.ui.print(
                    f"[{failed.error_message} — retrying in {delay:.1f}s ({retries}/{max_retries})]", "warn"
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
        self.settings = Settings.load(self.paths, settings)
        self.auth = AuthStore(self.paths.user / "auth.json")
        self.ui = ui or UI()
        self.bus = EventBus(on_error=lambda m: self.ui.print(m, "error"))
        self.commands = Commands()
        self.services: dict = {}
        self.sections: list = []
        self.runtime_sections: list = []
        self.permission_policy: PermissionPolicy | None = None
        self._permission_boundary = None
        self._reported_save_errors: set[int] = set()
        self._failed_stores: list = []
        self._retained_sessions: dict[Path, tuple[Runtime, deque]] = {}
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
        self.bus.emit("session_start", self)
        if refresh_models:
            from .preferences import start_model_refresh

            start_model_refresh(self)
        else:
            self._catalog_refresh_done.set()

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

    def remember_model_metadata(self, connection: ai.Model, models: list) -> None:
        with self._lock:
            path = cache_path(self.paths.user, connection)
            self._model_catalogs[path] = list(models)
            self._replace_metadata(connection.provider, {item.id: item.metadata for item in models})
            self._metadata_paths[connection.provider] = path
            try:
                write_metadata(path, models)
            except OSError:
                self.ui.print("Could not save model metadata; refreshed parameters remain available in this session.", "warn")
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
        message = ai.complete(model, system, prompt, max_tokens=max_tokens, total_max_tokens=total_max_tokens,
                              effort=effort, signal=signal)
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
            return Runtime(self, name, role, **kwargs)

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
            with ExitStack() as locks:
                for store in stores:
                    locks.enter_context(store._lock)
                stores[0].trash()
                for store in stores[1:]:
                    store.path = None
            self._retained_sessions.pop(path, None)
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
        return None if runtime.can_run() else Block("Session history is unsaved; work is paused.")

    def _check_policy(self, method, ev):
        if block := self._persistence_block(ev):
            return block
        try:
            policy = getattr(ev, "_permission_policy", self.permission_policy)
            result = getattr(policy, method)(ev)
            return result or self._persistence_block(ev)
        except Exception as error:
            return Block(f"Permission check failed: {error}")

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
                b = TextContent(budget(b.text, ev.cwd, ev.tool_name))
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
                self.ui.print(f"Unknown command /{name}. Type /help.", "error")
                return
        self.run([UserMessage(text)])

    def report_save_error(self, error: SessionSaveError) -> None:
        key = id(error.store)
        with self._lock:
            if key in self._reported_save_errors:
                return
            self._reported_save_errors.add(key)
            self._failed_stores.append(error.store)
        callback = getattr(self.ui, "save_failed", None)
        if callback is not None:
            callback(error.store.id, str(error.store.path), str(error))
        else:
            self.ui.print(str(error), "error")

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
            self.bus.emit("before_agent_start", rt, prompts)
            rt.agent.prompt(prompts)
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

    def notify(self, text: str, mode: str = "steer", source: str | None = None) -> None:
        """Deliver harness information to the lead agent; wakes it if idle."""
        tag = f' source="{source}"' if source else ""
        msg = UserMessage([TextContent(f"<notification{tag}>\n{text}\n</notification>")], kind="notification")
        with self._lock:
            if self._shutdown_started:
                return
            if self.busy:
                (self.main.agent.steer if mode == "steer" else self.main.agent.follow_up)(msg)
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
                self.ui.print(f"Cannot remove empty session: {error}", "error")
