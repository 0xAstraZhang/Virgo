"""AgentSession: wires the LLM layer, the agent loop, the session store and extensions together.

This is the "integrated harness": every feature plugs into one loop through extension events.
Per model call, in order:
  1. steering messages and notifications are appended by the loop
  2. auto-compaction check (at a turn boundary)
  3. `context` handlers and request sections build the request view without changing tool availability
  4. stream with retries (backoff, optional fallback model) and one reactive compaction on overflow
Per tool call: ordered extensions and explicit permission preflight -> execution recheck -> tool -> results.
When the agent would stop: queued follow-ups, then `agent_stop` extension handlers.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from stat import S_ISREG
from urllib.parse import urlsplit

from .. import ai
from ..agent import AgentTool, Block, ToolCallEvent, ToolContext, ToolResult
from ..ai.auth import AuthStore
from ..ai.model_metadata import cache_path, clean_metadata, read_catalog, write_metadata
from ..ai.models import base_url
from ..ai.providers._http import AsyncSDKBridge
from ..ai.types import (
    TextContent,
    UserMessage,
)
from ..diagnostics import capture_error, emergency_write, error_summary, safe_text
from ..execution_policy import is_native_windows, is_windows_sandbox_supported, normalize_approval_policy
from ..i18n import Translator, normalize_language
from ..output_store import OutputStore
from ..tools.output import MAX_BYTES, budget
from ..tools.shell import Shell
from .commands import Commands, expand_template, load_templates, parse_frontmatter, register_builtins
from .diagnostics import DiagnosticLog
from .execution import install_execution
from .extensions import (
    EventBus,
    Handled,
    install_command_hooks,
    load_builtin_features,
    load_extensions,
)
from .jobs import JobManager
from .origin_context import ExecutionOrigin, source_for
from .resource_rules import ResourceRules
from .runtime import Runtime
from .runtime_limits import ChildCancellation
from .settings import Paths, Settings
from .shell_commands import ShellSelection
from .store import SessionSaveError, SessionStore
from .telemetry import record_call
from .ui import UI

_DEFAULT_EFFORT = object()


class AgentSession:
    def __init__(
        self,
        cwd: str | Path = ".",
        *,
        ui: UI | None = None,
        model: str | None = None,
        session: str | Path | None = "new",  # "new" | "continue" | path | None (in memory)
        approval_policy: str | None = None,
        settings: dict | None = None,
        extensions: bool = True,
        trust_project_extensions: bool = False,
        home: str | Path | None = None,
        refresh_models: bool = True,
        shell_catalog: tuple[Shell, ...] | None = None,
    ):
        self.session_options = {
            "model": model,
            "approval_policy": approval_policy,
            "settings": settings,
            "extensions": extensions,
            "trust_project_extensions": trust_project_extensions,
            "home": home,
            "refresh_models": False,
        }
        self.paths = Paths(Path(cwd), Path(home) if home else None)
        self.cwd = self.paths.cwd
        self.ui = ui or UI()
        self.diagnostics = DiagnosticLog(self.paths.user / "logs")
        self._diagnostic_lock = threading.RLock()
        self._notified_errors: set[str] = set()
        self.settings = Settings.load(
            self.paths, settings, trust_project_extensions=trust_project_extensions
        )
        self.trust_project_extensions = trust_project_extensions
        self.translator = Translator(self.settings.get("language", "en"))
        self.ui.set_translator(self.translator)
        self.auth = AuthStore(self.paths.user / "auth.json")
        self.bus = EventBus(on_error=lambda m: self.ui.print(m, "error"), on_diagnostic=self._extension_error)
        self.templates: dict = {}
        self._template_paths: dict[str, Path] = {}
        self.commands = Commands(self.translator)
        self.services: dict = {}
        self.sections: list = []
        self.runtime_sections: list = []
        self.request_sections: list = []
        self._permission_boundary = None
        self._reported_save_errors: set[int] = set()
        self._failed_stores: list = []
        self._retained_sessions: dict[Path, tuple[Runtime, deque]] = {}
        self._main_runtimes: dict[str, Runtime] = {}
        self._output_stores: dict[str, OutputStore] = {}
        self._pending_child_work: dict[str, tuple[Runtime, int]] = {}
        self._tool_groups: dict[tuple[str, str], tuple[tuple[AgentTool, tuple], ...]] = {}
        self._tool_version = 0
        self._plugin_cleanups: list[tuple[str, object]] = []
        self.loaded_plugins: list[str] = []
        self._child_runtimes: dict[str, Runtime] = {}
        self._notification_depth = 0
        self._lock = threading.RLock()
        self.approval_revision = 0
        self.approval_policy = approval_policy or self.settings.get("permissions.approval", "ask")
        self.effort = self.settings.get("effort", "medium")
        self._io_bridge: AsyncSDKBridge | None = None
        self._shutdown_started = False
        self.busy = False
        self.command_exit_code = 0
        self.wakeups: deque = deque()
        self.discovered_models: dict[str, dict] = {}
        self._metadata_paths: dict[str, Path] = {}
        self._model_catalogs: dict[Path, list] = {}
        self._catalog_stop = threading.Event()
        self._catalog_refresh_done = threading.Event()
        self._metadata_pending = False
        self.model = self.resolve(model or self.settings.get("model"))
        self.small_model = self._small_model(self.model)

        install_execution(self)
        self.services["shell"] = ShellSelection(self, shell_catalog)
        # Core owns background supervision, so the shell capability and the
        # background plugin both depend on this service instead of on each other.
        self.services["jobs"] = JobManager(
            self, background_enabled=self.settings.plugin_enabled("background")
        )
        self.session_options["shell_catalog"] = self.services["shell"].catalog
        self.services["shell"].install_tools()
        register_builtins(self)
        self.store = self._open_store(session)
        self._visited_stores = [self.store]
        try:
            load_builtin_features(self)
            if extensions:
                load_extensions(self)
            # A plugin that failed setup is not loaded. Background supervision
            # must not outlive the capability that exposes its tools.
            jobs = self.services["jobs"]
            if jobs.background_enabled and "background" not in self.loaded_plugins:
                jobs.background_enabled = False
                self.services["shell"].install_tools()
                subagents = self.services.get("subagents")
                if subagents is not None and callable(getattr(subagents, "install_tools", None)):
                    subagents.install_tools()
            install_command_hooks(self)
            def warn(message: str) -> None:
                self.ui.print(message, "warn")
            self.templates.update(load_templates([self.paths.user / "prompts"], on_warning=warn))
            self.main = self._new_main(self.store)
            self.templates.update(load_templates([self.paths.project / "prompts"],
                                  access=self.main.execution_context, source_paths=self._template_paths,
                                  on_warning=warn))
        except BaseException:
            self.cleanup_plugins()
            raise
        self.main.wakeups = self.wakeups
        for record in self.diagnostics.initial_records():
            context = record.get("context", {})
            if context.get("session_id") == self.store.id and context.get("agent") in (None, "main"):
                self.main.recovery.safely("queue", record)
        self.bus.emit("session_start", self)
        OutputStore.prune_expired(self.paths.user / "outputs")
        if ResourceRules.purge_legacy(self.paths.user):
            self.ui.print(
                "Removed legacy saved resource grants (resource-grants.sqlite3); "
                "previously saved approvals must be granted again.",
                "warn",
            )
        self._warn_windows_sandbox()
        for warning in self.settings.migration_warnings:
            self.ui.print(warning, "warn")
        self._warn_retired_data()
        if refresh_models:
            from .preferences import start_model_refresh

            start_model_refresh(self)
        else:
            self._catalog_refresh_done.set()

    # -- diagnostics -----------------------------------------------------------
    def _warn_windows_sandbox(self) -> None:
        """Show guidance once when native Windows defaults to No-Limit on an unsupported OS build."""
        if not is_native_windows():
            return
        if self.settings.sources.get("execution.mode") != "default":
            return
        if self.settings.get("execution.mode") != "no-limit":
            return
        if is_windows_sandbox_supported():
            return
        marker = self.paths.user / "migrations" / "windows-sandbox-unsupported-notice"
        if marker.exists():
            return
        self.ui.print(
            "Native Windows sandboxing is disabled by default on this Windows version because "
            "\\Device\\Null does not grant ALL APPLICATION PACKAGES access (requires Windows 11 "
            "with updated null.sys / Feature 60692039). Running in No-Limit mode. Options:\n"
            "  1. WSL2: run Virgo inside WSL2 for full Linux sandbox isolation (recommended).\n"
            "  2. No-Limit: continue with No-Limit (--permission no-limit / /permissions no-limit).\n"
            "  3. Update Windows: update to Windows 11 with the null.sys AppContainer update (Feature 60692039).",
            "warn",
        )
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)
        except OSError as error:
            self.report_error(error, phase="windows.notice", notify=False)
    def _warn_retired_data(self):
        """Only inspect existence; old files are neither parsed nor migrated."""
        retired = ("schedules.json", "tasks", "runs", "workflows", "memory", "MEMORY.md", "worktrees")
        if not any(path.exists() for part in retired for path in self.paths.both(part)):
            return
        marker = self.paths.user / "migrations" / "core-plugins-v1-notice"
        if marker.exists():
            return
        self.ui.print(self.t("harness.migration.retired_data"), "warn")
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)
        except OSError as error:
            self.report_error(error, phase="migration.notice", notify=False)

    def register_cleanup(self, owner: str, callback) -> None:
        with self._lock:
            if self._shutdown_started:
                raise RuntimeError("Cannot register cleanup after shutdown")
            self._plugin_cleanups.append((owner, callback))

    def cleanup_plugins(self, *, owner: str | None = None) -> None:
        with self._lock:
            callbacks = [entry for entry in self._plugin_cleanups if owner is None or entry[0] == owner]
            self._plugin_cleanups = [entry for entry in self._plugin_cleanups if owner is not None and entry[0] != owner]
        for plugin, callback in reversed(callbacks):
            try:
                callback()
            except Exception as error:
                self.report_error(error, phase="extension.cleanup", code="PLUGIN_CLEANUP_FAILED",
                                  context={"plugin": plugin})

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
            if isinstance(value, ToolCallEvent) and value.origin is not None:
                runtime = value.origin.runtime
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
    def approval_policy(self) -> str:
        return self._approval_policy

    @approval_policy.setter
    def approval_policy(self, mode: str) -> None:
        with self._lock:
            self.assert_state_change_allowed()
            self._approval_policy = normalize_approval_policy(mode)
            self.approval_revision += 1

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

        def refreshed(current):
            resolved = self.resolve(current.spec)
            # A directory refresh owns capacity/capability metadata, not the
            # runtime's transport, credentials or explicit provider options.
            model = replace(
                current,
                context_window=resolved.context_window,
                max_tokens=resolved.max_tokens,
                reasoning=resolved.reasoning,
                vision=resolved.vision,
                supported_reasoning_levels=resolved.supported_reasoning_levels,
                default_reasoning_level=resolved.default_reasoning_level,
            )
            return current if model == current else model

        shared_model = self.small_model is self.model
        model = refreshed(self.model)
        # An unrelated catalog refresh must not invalidate prepared request
        # bindings by replacing an otherwise unchanged runtime model object.
        if model is not self.model:
            self.model = self.main.model = model
        self.small_model = self.model if shared_model else refreshed(self.small_model)
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
                 effort=_DEFAULT_EFFORT, max_tokens=None, signal=None, agent="main", tool_call_id=None,
                 accounting_store=None):
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
        runtime = self._child_runtimes.get(agent) or self.runtime_for_store(store)
        if runtime is not None:
            runtime.request_budget.consume()
        started = time.monotonic()
        diagnostic_context = {
            "agent": agent, "purpose": purpose, "tool_call_id": tool_call_id,
            "model": model.spec, "api": model.api, "model_call_id": uuid.uuid4().hex, "attempt": 1,
        }
        try:
            message = ai.complete(model, system, prompt, max_tokens=max_tokens, effort=effort, signal=signal)
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
            model, effort, ai.completion_token_limit(model, max_tokens)
        )
        metadata = {**metadata, "requested_effort": requested_effort, "effort_fallback_reason": fallback_reason}
        if effort is not None and metadata["effective_effort"] != effort:
            metadata["effort_fallback_reason"] = (
                f"adapter maps {effort!r} to {metadata['effective_effort']!r}"
            )
        record_call(store, model, message, purpose=purpose, started=started, effort=effort,
                    agent=agent, tool_call_id=tool_call_id, request_metadata=metadata)
        return message

    def output_store_for(self, store) -> OutputStore:
        """Return the per-session persistent OutputStore for ``store``."""
        with self._lock:
            session_id = store.id if store is not None else "ephemeral"
            existing = self._output_stores.get(session_id)
            if existing is None:
                persist = store is not None and store.path is not None
                existing = OutputStore.for_session(self.paths.user, session_id, persist=persist)
                self._output_stores[session_id] = existing
            return existing

    def _open_store(self, session) -> SessionStore:
        if session is None:
            return SessionStore(None, self.cwd)
        if session == "continue":
            try:
                paths = list(self.paths.sessions.iterdir())
            except (FileNotFoundError, NotADirectoryError):
                paths = []
            candidates = {}
            for path in paths:
                if not path.match("*.jsonl"):
                    continue
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                if S_ISREG(stat.st_mode):
                    candidates[path] = stat.st_mtime
            session = min(candidates, key=lambda path: (-candidates[path], str(path)), default="new")
        if session == "new":
            return SessionStore.new(self.paths.sessions, self.cwd)
        store, tail = SessionStore.resume(Path(session))
        if tail is not None:
            self.ui.warn(self.t(
                "harness.session.repaired_tail", path=store.path, line=tail.line, size=tail.size,
                last=store.entries[-1]["id"] if store.entries else store.id,
            ))
        return store

    def create_runtime(self, name: str, role: str, *, parent=None, **kwargs) -> Runtime:
        with self._lock:
            self.assert_state_change_allowed()
            origin_parent = kwargs.get("origin_parent")
            if role != "main":
                parent = parent or (origin_parent.runtime if origin_parent is not None else self.main)
                if not isinstance(parent, Runtime) or parent.session is not self:
                    raise ValueError("Runtime parent must belong to this session")
                if origin_parent is not None and origin_parent is not parent.origin:
                    raise ValueError("Runtime origin must match its parent")
                if name in self._child_runtimes or name == "main":
                    raise ValueError(f"Runtime name {name!r} is already in use")
                origin_parent = parent.origin
                kwargs["origin_parent"] = origin_parent
                kwargs.setdefault("cwd", parent.cwd)
                kwargs["request_budget"] = parent.request_budget
                kwargs["signal"] = ChildCancellation(parent.agent.signal, kwargs.get("signal"))
                parent_limit = parent.agent.cfg.max_turns
                if parent_limit is not None:
                    kwargs["max_turns"] = min(parent_limit, kwargs.get("max_turns") or parent_limit)
            if origin_parent is not None:
                origin_parent.root.entries()
            kwargs.setdefault("accounting_store", kwargs.get("store") if role == "main" else origin_parent.root)
            try:
                runtime = Runtime(self, name, role, **kwargs)
                self.bus.emit("runtime_created", runtime)
                if role == "main" and runtime.store is not None:
                    self._main_runtimes[runtime.store.id] = runtime
                elif role != "main":
                    self._child_runtimes[name] = runtime
                return runtime
            except Exception as error:
                owner = kwargs.get("store") or kwargs.get("accounting_store")
                self.report_error(error, phase="runtime.create", store=owner,
                                  context={"agent": name, "role": role})
                raise

    def release_runtime(self, runtime: Runtime) -> None:
        with self._lock:
            if self._child_runtimes.get(runtime.name) is not runtime:
                return
            self._child_runtimes.pop(runtime.name)
            for store in (runtime.store, runtime.accounting_store):
                if store is not None:
                    with store._lock:
                        if runtime.agent.signal in store.failure_signals:
                            store.failure_signals.remove(runtime.agent.signal)
        try:
            self.bus.emit("runtime_closed", runtime)
        finally:
            runtime.diagnostic_access.close()

    def runtime_for_context(self, ctx: ToolContext) -> Runtime | None:
        """Resolve a trusted caller without borrowing the currently selected runtime."""
        source = ctx.origin
        runtime = source.runtime if isinstance(source, ExecutionOrigin) else None
        if (not isinstance(runtime, Runtime) or runtime.session is not self
                or source is not runtime.origin or ctx.agent != runtime.name):
            return None
        return runtime

    def _new_main(self, store: SessionStore) -> Runtime:
        messages, ids = store.build_context()
        return self.create_runtime("main", "main", store=store, cwd=store.cwd, messages=messages, ids=ids)

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
            self.assert_state_change_allowed()
            if self.store.path == path:
                return
            retained = self._retained_sessions.get(path)
            if retained is not None:
                runtime, pending = retained
                self._activate_store(runtime.store, runtime=runtime, pending=pending)
                return
            self._activate_store(self._open_store(path))

    def new_session(self) -> None:
        self.assert_state_change_allowed()
        self._activate_store(SessionStore.new(self.paths.sessions if self.store.path else None, self.cwd))

    def trash_session(self, path: str | Path) -> None:
        """Trash a conversation, replacing the active one with a new saved session."""
        path = Path(path).absolute()
        with self._lock:
            self.assert_state_change_allowed()
            if self._shutdown_started:
                return
            stores = [store for store in self._visited_stores if store.path == path]
            if not stores:
                stores = [SessionStore.load(path)]
            current = self.store in stores
            if current:
                replacement = SessionStore.new(self.paths.sessions, self.cwd)
                runtime = self._new_main(replacement)
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
                output_store = self._output_stores.pop(store.id, None)
                if output_store is not None:
                    output_store.delete_all()
                else:
                    OutputStore.delete_session(self.paths.user, store.id)
            if current:
                self._activate_store(replacement, runtime=runtime)

    def _activate_store(self, store: SessionStore, *, runtime: Runtime | None = None,
                        pending: deque | None = None) -> None:
        with self._lock:
            self.assert_state_change_allowed()
            if self._shutdown_started:
                return
            # Apply the startup selection before session_start observers can
            # inspect tools or begin the initial request.
            if runtime is None:
                runtime = self._new_main(store)
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
    def register_tool(self, tool: AgentTool, roles: tuple = ("main",), *, owner: str = "host") -> None:
        """Register once; all replacements must use an explicitly owned namespace."""
        with self._lock:
            namespace = f"tool:{tool.name}"
            if (owner, namespace) in self._tool_groups:
                raise ValueError(f"Tool {tool.name!r} is already registered by {owner}")
            self.replace_tools(namespace, [(tool, roles)], owner=owner)

    def replace_tools(self, namespace: str, tools, *, owner: str = "host") -> None:
        """Validate and publish one complete namespace without partial mutations."""
        if not owner or not namespace:
            raise ValueError("Tool registrations require an owner and namespace")
        entries = tuple((tool, tuple(roles)) for tool, roles in tools)
        if any(not isinstance(tool, AgentTool) or not tool.name or not roles
               or any(not isinstance(role, str) or not role for role in roles) for tool, roles in entries):
            raise ValueError("Tool registrations require AgentTool objects and nonempty role names")
        key = (owner, namespace)
        with self._lock:
            occupied = {}
            for group, values in self._tool_groups.items():
                if group == key:
                    continue
                for tool, roles in values:
                    for role in roles:
                        occupied[tool.name, role] = group[0]
            for tool, roles in entries:
                for role in roles:
                    if (tool.name, role) in occupied:
                        raise ValueError(f"Tool {tool.name!r} for {role} is already registered by {occupied[tool.name, role]}")
                    occupied[tool.name, role] = owner
            groups = dict(self._tool_groups)
            if entries:
                groups[key] = entries
            else:
                groups.pop(key, None)
            self._tool_groups = groups
            self._tool_version += 1

    def remove_owned_tools(self, owner: str) -> None:
        with self._lock:
            self._tool_groups = {key: values for key, values in self._tool_groups.items() if key[0] != owner}
            self._tool_version += 1

    def tool_snapshot(self, role: str) -> tuple[int, list[AgentTool]]:
        with self._lock:
            return self._tool_version, [tool for values in self._tool_groups.values()
                                        for tool, roles in values if role in roles]

    def tools_for(self, role: str) -> list[AgentTool]:
        return self.tool_snapshot(role)[1]

    def tool(self, name: str, role: str = "main") -> AgentTool | None:
        return next((t for t in self.tools_for(role) if t.name == name), None)

    def _persistence_block(self, ev):
        runtime = ev.origin.runtime if ev.origin is not None else self.main
        return None if runtime.can_run() else Block(self.t("harness.session.unsaved"))

    def _check_policy(self, method, ev):
        if block := self._persistence_block(ev):
            return block
        try:
            provider = self.services["execution"]
            return getattr(provider, method)(ev) or self._persistence_block(ev)
        except Exception as error:
            runtime = ev.origin.runtime if ev.origin is not None else self.main
            details = runtime.report_error(error, phase=f"permission.{method}", context={
                "tool_name": ev.tool_name, "tool_call_id": ev.call_id,
                "policy": type(self.services["execution"]).__qualname__,
            })
            return Block(self.t("harness.permission.failed", error=safe_text(error)) + "; payload not started", details={
                "error": details, "status": "execution_unavailable", "boundary": {
                    **runtime.execution_context.describe(), "state": "admission_failed", "payload_started": False,
                    "side_effects": "none", "restricted_resource": "execution admission",
                }})

    def _before_tool(self, ev: ToolCallEvent):
        source_for(self, ev)  # Bind direct main integrations before policies or hooks can observe them.
        if block := self._persistence_block(ev):
            return block
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
                b = TextContent(budget(b.text, ev.cwd, ev.tool_name, t=self.t,
                                       execution_context=ev.origin.runtime.execution_context))
            bounded.append(b)
        res.content = bounded
        return res

    # -- running -----------------------------------------------------------------
    def prompt(self, text: str) -> None:
        """Handle one line of user input without disguising persistence failures."""
        self.command_exit_code = 0
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
                with self.ui.command_scope():
                    cmd.fn(self, args)
                return
            if name in self.templates:
                body = self.templates[name][1]
                if path := self._template_paths.get(name):
                    try:
                        data = self.main.execution_context.read_bytes(path)
                        if data is None:
                            raise OSError("Template resource is unavailable")
                        _, body = parse_frontmatter(data.decode("utf-8"))
                    except OSError as error:
                        self.ui.print(f"Prompt template resource denied: {error}", "error")
                        return
                text = expand_template(body, args)
            else:
                if name in {
                    "schedule", "schedules", "team", "tasks", "task", "memory", "goal",
                    "plan", "review", "workflow", "todos",
                }:
                    self.ui.print(self.t("harness.commands.retired", name=name), "error")
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

    def reserve_child_work(self, store):
        """Reserve queued agent work before a plugin publishes or starts it."""
        owner = store.id
        with self._lock:
            self.assert_state_change_allowed()
            if pending := self._pending_child_work.get(owner):
                origin, count = pending
            else:
                origin, count = self._main_runtimes.get(owner), 0
                if origin is None:
                    for child in self._child_runtimes.values():
                        candidate = child
                        while candidate.parent is not None:
                            candidate = candidate.parent
                        if candidate.origin.root.id == owner:
                            origin = candidate
                            break
                if origin is None:
                    raise ValueError("Queued child work requires an originating conversation")
            self._pending_child_work[owner] = (origin, count + 1)
        active = True

        def release():
            nonlocal active
            with self._lock:
                if not active:
                    return
                active = False
                origin, count = self._pending_child_work[owner]
                if count > 1:
                    self._pending_child_work[owner] = (origin, count - 1)
                else:
                    self._pending_child_work.pop(owner)

        return release

    def assert_state_change_allowed(self) -> None:
        if self._notification_depth:
            raise ValueError("State observers cannot start work or change session state reentrantly")

    def notify_state_change(self, event: str, *args) -> None:
        """Publish committed state; observers cannot reenter admission or mutation."""
        self._notification_depth += 1
        try:
            self.bus.emit(event, *args)
        finally:
            self._notification_depth -= 1

    def runtime_is_busy(self, runtime: Runtime) -> bool:
        """Inspect Core-owned running/queued work without invoking mutable observers."""
        if runtime.session is not self:
            raise ValueError("Cannot inspect a runtime owned by another session")
        with self._lock:
            origin = runtime
            while origin.parent is not None:
                origin = origin.parent
            if origin.agent.running or (origin is self.main and self.busy):
                return True
            if self._pending_child_work.get(origin.origin.root.id):
                return True
            for child in self._child_runtimes.values():
                if not child.agent.running:
                    continue
                parent = child.parent
                while parent is not None:
                    if parent is origin:
                        return True
                    parent = parent.parent
            return False

    def run(self, prompts: list, *, prepare=None) -> None:
        with self._lock:
            self.assert_state_change_allowed()
            if self._shutdown_started:
                return
            rt = self.main
            if prepare is not None:
                # A command can bind its selected input atomically to admission.
                # Prepared runs are never queued or replayed after a save failure.
                prompts = prepare(rt)
            if not rt.can_run():
                if prepare is None:
                    self.wakeups.extend(prompts)
                return
            try:
                rt.prepare_run()
            except Exception as error:
                rt.report_error(error, phase="session.run")
                raise
            self.busy = True
        wake = False
        try:
            with self.ui.command_suspended():
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

    def cancel_pending_input(self) -> int:
        """Cancel unconsumed user input, including input handed back after a run."""
        def is_input(message) -> bool:
            return isinstance(message, UserMessage) and message.kind is None

        with self._lock:
            removed = self.main.agent.discard_steering(is_input)
            kept = deque(message for message in self.wakeups if not is_input(message))
            removed += len(self.wakeups) - len(kept)
            self.wakeups.clear()
            self.wakeups.extend(kept)
            return removed

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
        self.abort_runtime(self.main)

    def abort_runtime(self, runtime: Runtime) -> None:
        """Latch cancellation for descendants before a main Event can be reused."""
        if runtime.session is not self:
            raise ValueError("Cannot abort a runtime owned by another session")
        with self._lock:
            runtime.agent.abort()
            for child in tuple(self._child_runtimes.values()):
                parent = child.parent
                while parent is not None:
                    if parent is runtime:
                        child.agent.abort()
                        break
                    parent = parent.parent
        self.bus.emit("runtime_abort", runtime)

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

    @property
    def io_bridge(self) -> AsyncSDKBridge:
        with self._lock:
            if self._io_bridge is None:
                self._io_bridge = AsyncSDKBridge()
                if self._shutdown_started:
                    self._io_bridge.close()
            return self._io_bridge

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self._catalog_stop.set()
            self.wakeups.clear()
        self.abort()
        for runtime in tuple(self._child_runtimes.values()):
            runtime.agent.abort()
        try:
            try:
                if self._io_bridge is not None:
                    self._io_bridge.close()
            finally:
                self.bus.emit("session_shutdown", self)
        finally:
            try:
                self.cleanup_plugins()
            finally:
                try:
                    self.cleanup_empty_stores()
                finally:
                    for runtime in (*self._main_runtimes.values(), *self._child_runtimes.values()):
                        runtime.diagnostic_access.close()
                    self.diagnostics.close()

    def cleanup_empty_stores(self) -> None:
        """Remove only empty stores visited by this process, including abandoned /new sessions."""
        for store in self._visited_stores:
            try:
                if store.path is None:
                    output_store = self._output_stores.pop(store.id, None)
                    if output_store is not None:
                        output_store.delete_all()
                else:
                    store.discard_if_empty()
                    if not store.path.exists():
                        output_store = self._output_stores.pop(store.id, None)
                        if output_store is not None:
                            output_store.delete_all()
            except OSError as error:
                self.report_error(error, phase="session.cleanup", store=store)
