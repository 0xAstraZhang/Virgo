"""AgentSession: wires the LLM layer, the agent loop, the session store and extensions together.

This is the "integrated harness": every feature plugs into one loop through extension events.
Per model call, in order:
  1. steering messages (notifications, teammate messages) are appended by the loop
  2. auto-compaction check (at a turn boundary)
  3. `context` handlers build the request view; plan mode restricts instructions and tools
  4. stream with retries (backoff, optional fallback model) and one reactive compaction on overflow
Per tool call: `tool_call` handlers (permission first) -> execute -> `tool_result` handlers -> budget.
When the agent would stop: follow-ups (schedules), then `agent_stop` handlers (todo, goal, hooks).
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from pathlib import Path

from .. import ai
from ..agent import Agent, AgentConfig, AgentEvent, AgentTool, ToolCallEvent, ToolResult
from ..ai.auth import AuthStore
from ..ai.stream import EventStream, StreamEvent
from ..ai.types import AssistantMessage, Context, StreamOptions, TextContent, ToolSpec, UserMessage
from ..features.permission import normalize_permission_mode, plan_tool_allowed
from ..tools import BASE_TOOLS
from ..tools.truncate import MAX_BYTES, budget
from . import compaction, context
from .commands import Commands, expand_template, load_templates, register_builtins
from .extensions import (
    ROLES_ALL,
    EventBus,
    Handled,
    install_command_hooks,
    load_builtin_features,
    load_user_extensions,
)
from .prompt import build_system_parts
from .settings import Paths, Settings
from .store import SessionStore
from .ui import UI


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
    ):
        self.session = session
        self.name = name
        self.role = role
        self.cwd = Path(cwd or session.cwd)
        self.store = store
        self.ids: list = list(ids or [])
        self.model = model or session.model
        self.system_extra = system_extra
        self.tools = tools if tools is not None else session.tools_for(role)
        self.compaction = compaction
        self.compacted_at = 1 if messages and getattr(messages[0], "kind", None) == "summary" else 0
        self.system = ""
        self.rebuild_system()
        cfg = AgentConfig(
            tools=self.tools,
            call_model=self.call_model,
            name=name,
            cwd=self.cwd,
            before_tool_call=session._before_tool,
            before_tool_execute=session._before_tool_execute,
            after_tool_call=session._after_tool,
            should_stop=lambda _msgs: session.bus.first("agent_stop", self),
            max_turns=max_turns,
        )
        self.agent = Agent(cfg, messages=list(messages or []), signal=signal)
        self.agent.subscribe(self._on_event)
        self.usage_context_signature = context.context_signature(self.model, *self.request_system_and_tools())
        self.usage_valid_from = self.compacted_at

    @property
    def messages(self) -> list:
        return self.agent.messages

    def rebuild_system(self) -> None:
        self.system_parts = build_system_parts(self.session, self.role, self.system_extra, self.cwd)
        self.system = "\n\n".join(part.text for part in self.system_parts)
        if hasattr(self, "agent"):
            # Rebuilds follow compaction/reconnect; old usage describes another
            # prompt, and message indices may have changed after compaction.
            self.usage_valid_from = len(self.messages)
            self.usage_context_signature = context.context_signature(self.model, *self.request_system_and_tools())

    def request_system_and_tools(self) -> tuple[str, list[ToolSpec]]:
        """The same permission-filtered prompt for requests and context inspection."""
        permissions = self.session.services.get("permissions")
        mode = permissions.mode_for(self.name) if permissions else self.session.permission_mode
        system = self.system
        tools = [t.spec() for t in self.tools if mode != "plan" or plan_tool_allowed(t)]
        if mode == "plan":
            system += (
                "\n\n<permission_mode>\n"
                "Plan mode is active. Investigate with read-only tools, then provide a concrete plan "
                "for the user. Do not implement changes, modify project files, install dependencies, "
                "or execute commands with side effects. Planning task records are allowed. "
                "Only tools offered in this request are available.\n</permission_mode>"
            )
            for tool in tools:
                if tool.name == "bash":
                    tool.description = "Run a read-only shell command to inspect the project. Do not modify files or system state."
        return system, tools

    def _on_event(self, ev: AgentEvent) -> None:
        if ev.type == "message_end":
            self.ids.append(self.store.append_message(ev.message) if self.store else None)
        self.session.bus.emit("agent_event", ev, self)
        if ev.type == "turn_end":
            self.session.bus.emit("turn_end", ev, self)
        self.session.ui.on_event(ev)

    def compact(self, focus: str | None = None, reason: str = "manual") -> dict | None:
        return compaction.compact(self, focus=focus, reason=reason)

    def context_tokens(self) -> int:
        return context.estimate_context_tokens(self)

    def _compaction_due(self) -> bool:
        enabled, trigger = context.compaction_threshold(self)
        return enabled and self.context_tokens() > trigger

    # -- model calls ---------------------------------------------------------
    def call_model(self, messages: list, signal: threading.Event) -> EventStream:
        fallback = AssistantMessage(api=self.model.api, provider=self.model.provider, model=self.model.id)
        return EventStream(self._attempts(messages, signal), fallback)

    def _attempts(self, messages: list, signal: threading.Event):
        s = self.session
        if self._compaction_due():
            self.compact(reason="auto")
        max_retries = int(s.settings.get("retry.maxRetries", 3))
        retries = overloaded = 0
        overflow_done = False
        model = self.model
        while True:
            view = s.bus.chain("context", list(messages), self)
            system, tools = self.request_system_and_tools()
            signature = context.context_signature(model, system, tools)
            if signature != self.usage_context_signature:
                self.usage_valid_from = len(messages)
                self.usage_context_signature = signature
            ctx = Context(system, view, tools)
            opts = StreamOptions(signal=signal, max_tokens=s.settings.get("maxTokens"), effort=s.effort)
            failed: AssistantMessage | None = None
            for ev in ai.stream(model, ctx, opts):
                if ev.type == "error" and ev.partial.stop_reason == "error":
                    failed = ev.partial
                    break
                yield ev
            if failed is None:
                return
            if failed.error_kind == "overflow" and not overflow_done and self.compaction:
                overflow_done = True
                yield StreamEvent("retry", failed)
                if self.compact(reason="overflow") is not None:
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
    ):
        self.session_options = {
            "model": model,
            "permission_mode": permission_mode,
            "settings": settings,
            "extensions": extensions,
            "home": home,
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
        self._tools: list[tuple[AgentTool, tuple]] = []
        self.templates: dict = {}
        self.permission_mode = permission_mode or self.settings.get("permissions.mode", "auto-review")
        self.effort = self.settings.get("effort", "medium")
        self._lock = threading.RLock()
        self._shutdown_started = False
        self.busy = False
        self.wakeups: deque = deque()
        self.discovered_models: dict[str, dict] = {}
        self.model = self.resolve(model or self.settings.get("model"))
        self.small_model = self._small_model(model)

        for t in BASE_TOOLS:
            self.register_tool(t, ROLES_ALL)
        register_builtins(self)
        self.store = self._open_store(session)
        self._visited_stores = [self.store]
        load_builtin_features(self)
        if extensions:
            load_user_extensions(self)
        install_command_hooks(self)
        self.templates = load_templates(self.paths.both("prompts"))
        self.main = self._new_main()
        self.bus.emit("session_start", self)

    # -- setup helpers ---------------------------------------------------------
    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @permission_mode.setter
    def permission_mode(self, mode: str) -> None:
        self._permission_mode = normalize_permission_mode(mode)

    def resolve(self, spec: str) -> ai.Model:
        providers = self.settings.get("providers", {})
        if metadata := self.discovered_models.get(spec):
            provider, model_id = spec.split("/", 1)
            config = providers.get(provider, {})
            models = config.get("models") or {}
            names = {"context_window": "contextWindow", "max_tokens": "maxTokens", "reasoning": "reasoning", "vision": "vision"}
            live = {names[key]: value for key, value in metadata.items() if key in names}
            providers = {**providers, provider: {**config, "models": {**models, model_id: {**models.get(model_id, {}), **live}}}}
        m = ai.resolve_model(spec, providers)
        m.auth_path = str(self.auth.path)
        if m.api == "anthropic":
            for k in ("fallbacks", "thinkingDisplay", "thinkingBinding"):
                m.options.setdefault(k, self.settings.get(f"anthropic.{k}"))
        return m

    def _small_model(self, override: str | None) -> ai.Model:
        configured = self.settings.get("smallModel")
        if override and override.startswith("faux/"):
            return self.model
        if self.model.provider != "anthropic" and configured == "anthropic/claude-haiku-5-5":
            return self.model  # don't silently require a second provider
        return self.resolve(configured) if configured else self.model

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

    def _new_main(self) -> Runtime:
        messages, ids = self.store.build_context()
        return Runtime(self, "main", "main", store=self.store, messages=messages, ids=ids)

    def open_session(self, path: str | Path) -> None:
        self._activate_store(SessionStore.load(Path(path)))

    def new_session(self) -> None:
        self._activate_store(SessionStore.new(self.paths.sessions if self.store.path else None, self.cwd))

    def _activate_store(self, store: SessionStore) -> None:
        with self._lock:
            if self._shutdown_started:
                return
            previous = self.store
            self.store = store
            try:
                runtime = self._new_main()
            except Exception:
                self.store = previous
                raise
            self.main = runtime
            self._visited_stores.append(store)
            self.bus.emit("session_start", self)

    def set_model(self, spec: str) -> None:
        self.model = self.resolve(spec)
        self.main.model = self.model
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

    def tools_for(self, role: str) -> list[AgentTool]:
        return [t for t, roles in self._tools if role in roles]

    def tool(self, name: str, role: str = "main") -> AgentTool | None:
        return next((t for t in self.tools_for(role) if t.name == name), None)

    def _before_tool(self, ev: ToolCallEvent):
        return self.bus.first("tool_call", ev, self)

    def _before_tool_execute(self, ev: ToolCallEvent):
        permissions = self.services.get("permissions")
        return permissions.before_execute(ev) if permissions is not None else None

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
        """Handle one line of user input: hooks, commands, templates, then a run."""
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

    def run(self, prompts: list) -> None:
        rt = self.main
        with self._lock:
            if self._shutdown_started:
                return
            self.busy = True
        wake = False
        try:
            self.bus.emit("before_agent_start", rt, prompts)
            rt.agent.prompt(prompts)
        finally:
            with self._lock:
                self.busy = False
                left = rt.agent.drain_queues()
                if left and not self._shutdown_started:
                    self.wakeups.extend(left)
                    wake = True
            self.bus.emit("agent_end", rt)
        if wake:
            self.ui.wake()

    def run_pending(self) -> bool:
        """Run queued notifications, if any. Returns True if a run happened."""
        with self._lock:
            if self._shutdown_started or self.busy or not self.wakeups:
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
            if self._shutdown_started or not self.busy:
                return False
            self.main.agent.steer(UserMessage(text))
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
