"""Extension system: an event bus plus the API that built-in features and extensions share.

Built-in extensions live in ``virgo.extensions`` (``mcp``, ``todo``) and load
automatically unless disabled via ``extensions.disabled`` or ``--no-extensions``.
A user or trusted project extension is a Python file in ``~/.virgo/extensions/``
or ``.virgo/extensions/`` (overriding any built-in extension with the same stem)::

    from virgo.agent import Block

    def setup(api):
        def guard(ev, session):
            if ev.tool_name == "bash" and "rm -rf" in ev.args.get("command", ""):
                return Block("rm -rf is not allowed here")
        api.on("tool_call", guard)

Events (handler signature):
  session_start(session)                 session_shutdown(session)
  input(text, session) -> str | Handled | None      (transform or consume user input)
  user_steer(text, session)              the user queued a steering message in a running run
  before_agent_start(runtime, prompts)   agent_end(runtime)
  agent_event(event, runtime)            every AgentEvent from any agent
  context(messages, runtime) -> list | None          (request-time view; keep it append-only)
  tool_call(ToolCallEvent, session) -> Block | None  (may mutate event.args)
  tool_result(ToolResult, ToolCallEvent, session) -> ToolResult | None
  turn_end(event, runtime)               agent_stop(runtime) -> Continue | None
  before_compact(runtime)                compact(runtime, summary)
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..agent.types import AgentTool, Block, Continue, ToolResult
from ..ai.types import TextContent
from ..diagnostics import capture_error, emergency_write, safe_text
from ..file_access import same_directory
from .contracts import ExtensionHost
from .settings import BUILTIN_EXTENSIONS, PLUGIN_IDS

ROLES_ALL = ("main", "subagent")


@dataclass
class Handled:
    """Returned by an `input` handler that fully handled the input."""

    message: str | None = None


class EventBus:
    def __init__(self, on_error: Callable[[str], None] | None = None, *,
                 on_diagnostic: Callable[[dict, tuple], None] | None = None):
        self._handlers: dict[str, list] = defaultdict(list)
        self._seq = 0
        self._owners: dict[tuple[str, int], str] = {}
        self.on_error = on_error or (lambda msg: print(msg, file=sys.stderr))
        self.on_diagnostic = on_diagnostic

    def on(self, event: str, fn: Callable, priority: int = 0, *, owner: str = "host") -> Callable[[], None]:
        self._seq += 1
        entry = (-priority, self._seq, fn)
        self._handlers[event].append(entry)
        self._handlers[event].sort(key=lambda e: (e[0], e[1]))
        self._owners[event, self._seq] = owner

        def unsubscribe():
            if entry in self._handlers[event]:
                self._handlers[event].remove(entry)
            self._owners.pop((event, entry[1]), None)

        return unsubscribe

    def boundary(self, priority: int) -> tuple[int, int]:
        """Reserve a stable registration position without registering a callback."""
        self._seq += 1
        return (-priority, self._seq)

    def snapshot(self, event: str) -> tuple:
        return tuple(self._handlers[event])

    def first_snapshot(self, event: str, handlers: tuple, *args):
        for _, _, fn in handlers:
            result = self._call(event, fn, args)
            if result is not None:
                return result
        return None

    def has_handlers(self, event: str, *, exclude=None, exclude_owners: tuple[str, ...] = ()) -> bool:
        return any(
            fn != exclude and self._owners.get((event, seq)) not in exclude_owners
            for _, seq, fn in self._handlers.get(event, [])
        )

    def _call(self, event: str, fn: Callable, args: tuple) -> Any:
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 - one broken extension must not break the session
            handler = getattr(fn, "__qualname__", type(fn).__qualname__)
            details = capture_error(e, phase="extension.handler", code="EXTENSION_HANDLER_FAILED",
                                    context={"event": event, "handler": handler})
            try:
                if self.on_diagnostic is not None:
                    # The harness uses the original dispatch arguments to bind
                    # the error to its runtime, including a background child.
                    self.on_diagnostic(details, args)
                else:
                    self.on_error(f"[extension error in {event} handler {handler}] {details['message']}")
                    emergency_write(details)
            except Exception as reporting_error:  # a broken reporter must not interrupt later handlers
                emergency_write(details, reporting_error)
            return None

    def emit(self, event: str, *args) -> list:
        return [
            r for _, _, fn in list(self._handlers[event]) if (r := self._call(event, fn, args)) is not None
        ]

    def first(self, event: str, *args) -> Any:
        for _, _, fn in list(self._handlers[event]):
            r = self._call(event, fn, args)
            if r is not None:
                return r
        return None

    def chain(self, event: str, value: Any, *args) -> Any:
        for _, _, fn in list(self._handlers[event]):
            r = self._call(event, fn, (value, *args))
            if isinstance(r, Handled):
                return r
            if r is not None:
                value = r
        return value


class ExtensionAPI:
    """Owned registration and runtime services for trusted in-process plugins.

    ``session`` is a limited compatibility escape hatch for official plugins.
    New extensions use this facade. Python extensions are trusted code, not a security sandbox.
    """

    def __init__(self, session: ExtensionHost, name: str):
        self._session = session
        self.name = name
        self._undos: list[Callable] = []

    @property
    def session(self):
        return self._session

    # shortcuts
    @property
    def settings(self):
        return self.session.settings

    @property
    def paths(self):
        return self.session.paths

    @property
    def ui(self):
        return self.session.ui

    @property
    def services(self) -> dict:
        return self.session.services

    @property
    def cwd(self):
        return self._session.cwd

    @property
    def model(self):
        return self._session.model

    @property
    def small_model(self):
        return self._session.small_model

    @property
    def small_model_effort(self):
        return self._session.small_model_effort

    @property
    def auth(self):
        return self._session.auth

    @property
    def translator(self):
        return self._session.translator

    @property
    def project_trusted(self) -> bool:
        """Whether project-level extensions, hooks, and MCP configurations are trusted."""
        return bool(
            getattr(self._session, "trust_project_extensions", False)
            or getattr(self._session.settings, "project_extensions_trusted", False)
        )

    def host_context(self, cwd: Path | None = None):
        """Return an unrestricted HostContext reusing the session's shared SandboxExecutor."""
        from ..execution import HostContext, SandboxExecutor

        provider = self._session.services.get("execution")
        executor = getattr(provider, "executor", None) or SandboxExecutor()
        return HostContext.unrestricted(Path(cwd) if cwd is not None else self._session.cwd, executor=executor)

    def t(self, key: str, /, **values: object) -> str:
        """Translate an extension-owned key using the current session language."""
        return self.session.t(key, **values)

    def on(self, event: str, fn: Callable, priority: int = 0):
        unsubscribe = self._session.bus.on(event, fn, priority, owner=self.name)
        self._undos.append(unsubscribe)
        return unsubscribe

    def register_tool(self, tool: AgentTool, roles: tuple = ("main",)) -> None:
        self._session.register_tool(tool, roles, owner=self.name)

    def replace_tools(self, namespace: str, tools: list[tuple[AgentTool, tuple]]) -> None:
        """Validate a whole owned set before atomically publishing its next version.

        Existing runtime requests/batches keep their tool objects. New schemas
        become visible to each runtime at its next model request boundary.
        """
        self._session.replace_tools(namespace, tools, owner=self.name)

    def register_command(
        self, name: str, fn: Callable, description: str = "", concurrent: bool = False, *, argument_hint: str = "",
        description_key: str = "", argument_hint_key: str = "",
    ) -> None:
        """concurrent=True permits read-only state queries during another run."""
        self.session.commands.register(name, fn, description, source=self.name, concurrent=concurrent,
                                       argument_hint=argument_hint, description_key=description_key,
                                       argument_hint_key=argument_hint_key)

    def add_runtime_system_section(self, name: str, fn: Callable, order: int = 50) -> None:
        """fn(RuntimeContext) -> text; construction errors reject the candidate."""
        self._add_section(self._session.runtime_sections, name, fn, order)

    def add_request_system_section(self, name: str, fn: Callable, order: int = 50) -> None:
        """fn(Runtime) -> instructions evaluated for every request and compaction.

        This changes task instructions only; tools and execution permissions are
        owned by the normal registry and permission policies.
        """
        self._add_section(self._session.request_sections, name, fn, order)

    def declare_diagnostic_query_tool(self, tool_name: str) -> None:
        """Declare a registered tool as a runtime diagnostic-query capability."""
        undo = self._session.declare_diagnostic_query_tool(self.name, tool_name)
        self._undos.append(undo)

    def add_request_preparer(self, name: str, fn: Callable, order: int = 50) -> None:
        """fn(Runtime, Context, Model) -> None invoked before every model or compaction request."""
        self._add_section(self._session.request_preparers, name, fn, order)

    def add_request_fallback(
        self, name: str, fn: Callable, order: int = 50, *, matches: Callable | None = None
    ) -> None:
        """fn(Runtime, Model, Context, AssistantMessage) -> purpose | None on failed model requests.

        Optional ``matches(Context, AssistantMessage) -> bool`` identifies request-extension errors
        for read-only fallback decisions (such as in-context compaction) without mutating state.
        """
        key = f"{self.name}:{name}"
        if any(entry[1] == key for entry in self._session.request_fallbacks):
            raise ValueError(f"Request fallback {key!r} is already registered")
        entry = (order, key, fn, matches)
        self._session.request_fallbacks.append(entry)
        self._undos.append(
            lambda: self._session.request_fallbacks.remove(entry)
            if entry in self._session.request_fallbacks
            else None
        )

    def _add_section(self, sections, name, fn, order):
        key = f"{self.name}:{name}"
        if any(existing == key for _, existing, _ in sections):
            raise ValueError(f"System section {key!r} is already registered")
        entry = (order, key, fn)
        sections.append(entry)
        self._undos.append(lambda: sections.remove(entry) if entry in sections else None)

    def on_cleanup(self, callback: Callable[[], None]) -> None:
        """Close owned resources in reverse order, even if another cleanup fails."""
        if not callable(callback):
            raise TypeError("Cleanup must be callable")
        self._session.register_cleanup(self.name, callback)

    def current_runtime(self):
        with self._session.state_lock:
            return self._session.main

    def current_store(self):
        with self._session.state_lock:
            return self._session.store

    def subagent_store_for(self, store=None):
        return self._session.subagent_store_for(store)

    def runtime_for_context(self, context):
        return self._session.runtime_for_context(context)

    def create_runtime(self, name: str, role: str, *, parent=None, **kwargs):
        if role == "main":
            raise ValueError("Plugins create child runtimes; the Host owns main sessions")
        parent = parent or self.current_runtime()
        if parent.session is not self._session:
            raise ValueError("Child runtime parent belongs to another session")
        return self._session.create_runtime(name, role, parent=parent, **kwargs)

    def release_runtime(self, runtime):
        self._session.release_runtime(runtime)

    def reserve_child_work(self, store) -> Callable[[], None]:
        """Return an idempotent release for agent work queued for this owner."""
        return self._session.reserve_child_work(store)

    def tools_for(self, role: str):
        return self._session.tools_for(role)

    def resolve(self, spec: str):
        return self._session.resolve(spec)

    def complete(self, *args, **kwargs):
        return self._session.complete(*args, **kwargs)

    def authorize_tool(self, event):
        return self._session._before_tool(event) or self._session._before_tool_execute(event)

    def has_tool_hooks(self):
        return (
            self._session.bus.has_handlers("tool_call")
            or self._session.bus.has_handlers("tool_result", exclude_owners=("core:execution",))
        )

    def report_error(self, error, **kwargs):
        return self._session.report_error(error, **kwargs)

    def retain_diagnostics(self, store, records):
        self._session.retain_diagnostics(store, records)

    def notify(self, text: str, mode: str = "steer", source: str | None = None, *, store=None,
               diagnostics=()) -> None:
        self.session.notify(text, mode=mode, source=source or self.name, store=store, diagnostics=diagnostics)

    def rollback(self):
        self._session.cleanup_plugins(owner=self.name)
        for undo in reversed(self._undos):
            undo()
        self._session.remove_owned_tools(self.name)
        self._session.commands.remove_source(self.name)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

BUILTIN_PLUGINS = PLUGIN_IDS


def _setup(session: ExtensionHost, name: str, setup: Callable[[ExtensionAPI], None], *, context=None) -> None:
    api = ExtensionAPI(session, name)
    services = dict(session.services)
    tools = session.checkpoint_tools()
    try:
        setup(api)
        session.loaded_plugins.append(name)
    except Exception as error:
        api.rollback()
        # A capability may ask a Core adapter to rebuild its owned tools (shell
        # background mode). Restore that adapter's registration too on failure.
        session.restore_tools(tools)
        session.services.clear()
        session.services.update(services)
        session.report_error(error, phase="extension.load", code="PLUGIN_DEGRADED",
                             context={"plugin": name, "status": "disabled_after_failure", **(context or {})})
        session.ui.print(session.t("harness.plugins.degraded", name=name), "warn")


def load_builtin_features(session: ExtensionHost) -> None:
    for name in BUILTIN_PLUGINS:
        if not session.settings.plugin_enabled(name):
            continue
        # The import is inside the transaction: disabled plugins are never imported.
        _setup(session, name, lambda api, name=name: importlib.import_module(f"virgo.plugins.{name}").setup(api))


def _file_loader(path: Path) -> Callable[[ExtensionAPI], None]:
    def load(api: ExtensionAPI) -> None:
        spec = importlib.util.spec_from_file_location(f"virgo_ext_{path.stem}", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "setup"):
            mod.setup(api)

    return load


def load_extensions(session: ExtensionHost) -> None:
    """Load built-in, user, and trusted project extensions with stem-based override."""
    entries: dict[str, tuple[str, Callable[[ExtensionAPI], None], dict[str, str]]] = {
        name: (
            f"builtin:{name}",
            lambda api, name=name: importlib.import_module(f"virgo.extensions.{name}").setup(api),
            {"extension": f"builtin:{name}"},
        )
        for name in BUILTIN_EXTENSIONS
    }

    user_dir = session.paths.user / "extensions"
    if user_dir.is_dir():
        for path in sorted(user_dir.glob("*.py")):
            entries[path.stem] = (f"user:{path.stem}", _file_loader(path), {"extension": str(path)})

    project_dir = session.paths.project / "extensions"
    if not same_directory(session.paths.project, session.paths.user) and project_dir.is_dir():
        if not (session.trust_project_extensions or session.settings.project_extensions_trusted):
            if any(project_dir.glob("*.py")):
                session.ui.print(session.t("harness.extensions.untrusted_project"), "warn")
        else:
            for path in sorted(project_dir.glob("*.py")):
                entries[path.stem] = (f"project:{path.stem}", _file_loader(path), {"extension": str(path)})

    disabled = session.settings.disabled_extensions()
    for stem, (owner, loader, context) in entries.items():
        if stem in disabled:
            continue
        _setup(session, owner, loader, context=context)


# ---------------------------------------------------------------------------
# shell command hooks from settings ("hooks": {"PreToolUse": [{"matcher": "bash", "command": "..."}]})
# ---------------------------------------------------------------------------


def _matches(matcher: str | None, name: str) -> bool:
    if not matcher or matcher == "*":
        return True
    try:
        return re.fullmatch(matcher, name) is not None
    except re.error:
        return matcher == name


def _run_hook(hook: dict, payload: dict, cwd: Path, *, on_error=None, hook_index: int = 1) -> tuple[int, str, str]:
    context = {
        "event": payload.get("hook_event_name"), "hook_index": hook_index, "cwd": str(cwd),
        **{key: payload[key] for key in ("session_id", "agent", "tool_name", "tool_call_id") if key in payload},
    }

    def report(error, *, code, **metadata):
        details = capture_error(error, phase="extension.command_hook", code=code,
                                context={**context, **metadata}, secrets=(hook.get("command", ""),))
        try:
            if on_error is not None:
                on_error(details)
            else:
                emergency_write(details)
        except Exception as reporting_error:
            emergency_write(details, reporting_error)

    try:
        timeout = float(hook.get("timeout", 60))
        p = subprocess.run(
            hook["command"],
            shell=True,
            cwd=str(cwd),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if p.returncode not in (0, 2):
            report(f"Command hook exited with status {p.returncode}.", code="HOOK_EXIT_FAILED",
                   exit_code=p.returncode, stderr=safe_text(p.stderr.strip())[:2000])
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired as error:
        # TimeoutExpired.__str__ embeds the shell command. Preserve its stack,
        # but never copy a command (which can include credentials) into a log.
        failure = TimeoutError(f"Command hook timed out after {timeout:g} seconds.")
        failure.__traceback__ = error.__traceback__
        report(failure, code="HOOK_TIMEOUT", timeout_seconds=timeout)
        return 1, "", "hook timed out"
    except Exception as error:
        report(error, code="HOOK_EXECUTION_FAILED")
        return 1, "", safe_text(str(error))


def _with_feedback(res: ToolResult, note: str) -> ToolResult:
    """Append a note to the result text, keeping images, details and flags."""
    content = list(res.content)
    last = next((i for i in range(len(content) - 1, -1, -1) if isinstance(content[i], TextContent)), None)
    if last is None:
        content.append(TextContent(note))
    else:
        content[last] = TextContent(f"{content[last].text}\n\n{note}")
    return replace(res, content=content)


def install_command_hooks(session: ExtensionHost) -> None:
    raw_hooks = session.settings.get("hooks", {})
    if raw_hooks is None:
        return
    if not isinstance(raw_hooks, dict):
        session.ui.print("Ignoring invalid hooks configuration: expected a JSON object.", "warn")
        return
    hooks: dict = raw_hooks
    cwd = session.cwd

    def valid_entries(event_name: str) -> list[dict]:
        entries = hooks.get(event_name)
        if entries is None:
            return []
        if not isinstance(entries, list):
            session.ui.print(
                f"Ignoring invalid hooks.{event_name}: expected a list of hook objects.",
                "warn",
            )
            return []
        valid: list[dict] = []
        for idx, item in enumerate(entries, 1):
            if (not isinstance(item, dict)
                    or not isinstance(item.get("command"), str)
                    or not item["command"].strip()):
                session.ui.print(
                    f"Ignoring invalid hook entry in hooks.{event_name} at index {idx}: "
                    "expected an object with a nonempty string 'command'.",
                    "warn",
                )
                continue
            valid.append(item)
        return valid

    def base(event: str, store=None) -> dict:
        return {"hook_event_name": event, "cwd": str(cwd), "session_id": (store or session.store).id}

    def run(hook, payload, index, *, store=None):
        return _run_hook(hook, payload, cwd, hook_index=index,
                         on_error=lambda details: session.report_error(details, phase="extension.command_hook",
                                                                        store=store))

    def source(ev):
        return ev.origin.root if ev.origin is not None else session.store

    for index, hook in enumerate(valid_entries("PreToolUse"), 1):

        def pre(ev, _s, hook=hook, index=index):
            if not _matches(hook.get("matcher"), ev.tool_name):
                return None
            store = source(ev)
            code, out, err = run(
                hook,
                {**base("PreToolUse", store), "tool_name": ev.tool_name, "tool_call_id": ev.call_id,
                 "tool_input": ev.args, "agent": ev.agent},
                index, store=store,
            )
            return Block(err or out or session.t("harness.hooks.blocked", index=index)) if code == 2 else None

        session.bus.on("tool_call", pre, priority=50)

    for index, hook in enumerate(valid_entries("PostToolUse"), 1):

        def post(res: ToolResult, ev, _s, hook=hook, index=index):
            if not _matches(hook.get("matcher"), ev.tool_name):
                return None
            store = source(ev)
            payload = {
                **base("PostToolUse", store),
                "tool_name": ev.tool_name,
                "tool_call_id": ev.call_id,
                "agent": ev.agent,
                "tool_input": ev.args,
                "tool_response": res.as_text(),
                "is_error": res.is_error,
            }
            code, out, err = run(hook, payload, index, store=store)
            if code == 2 and (err or out):
                return _with_feedback(res, session.t("harness.hooks.feedback", text=err or out))
            return None

        session.bus.on("tool_result", post)

    for index, hook in enumerate(valid_entries("UserPromptSubmit"), 1):

        def submit(text: str, _s, hook=hook, index=index):
            code, out, err = run(hook, {**base("UserPromptSubmit"), "prompt": text}, index)
            if code == 2:
                return Handled(session.t("harness.hooks.prompt_blocked", text=err or out))
            if code == 0 and out:
                return f"{text}\n\n<hook_context>\n{out}\n</hook_context>"
            return None

        session.bus.on("input", submit)

    for index, hook in enumerate(valid_entries("Stop"), 1):

        def stop(rt, hook=hook, index=index):
            if rt.role != "main":
                return None
            code, out, err = run(hook, {**base("Stop", rt.accounting_store), "last_message": rt.agent.last_text},
                                 index, store=rt.accounting_store)
            return Continue(err or out) if code == 2 and (err or out) else None

        session.bus.on("agent_stop", stop, priority=20)
