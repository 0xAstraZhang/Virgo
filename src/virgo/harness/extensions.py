"""Extension system: an event bus plus the API that built-in features and user extensions share.

A user extension is a Python file in ``~/.virgo/extensions/`` or ``.virgo/extensions/``::

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
from typing import TYPE_CHECKING, Any

from ..agent.types import AgentTool, Block, Continue, ToolResult
from ..ai.types import TextContent
from ..diagnostics import capture_error, emergency_write, safe_text

if TYPE_CHECKING:
    from .session import AgentSession

ROLES_ALL = ("main", "subagent", "teammate")


@dataclass
class Handled:
    """Returned by an `input` handler that fully handled the input."""

    message: str | None = None


class EventBus:
    def __init__(self, on_error: Callable[[str], None] | None = None, *,
                 on_diagnostic: Callable[[dict, tuple], None] | None = None):
        self._handlers: dict[str, list] = defaultdict(list)
        self._seq = 0
        self.on_error = on_error or (lambda msg: print(msg, file=sys.stderr))
        self.on_diagnostic = on_diagnostic

    def on(self, event: str, fn: Callable, priority: int = 0) -> Callable[[], None]:
        self._seq += 1
        entry = (-priority, self._seq, fn)
        self._handlers[event].append(entry)
        self._handlers[event].sort(key=lambda e: (e[0], e[1]))
        return lambda: self._handlers[event].remove(entry)

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

    def has_handlers(self, event: str, *, exclude=None) -> bool:
        return any(fn != exclude for _, _, fn in self._handlers.get(event, []))

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
    def __init__(self, session: AgentSession, name: str):
        self.session = session
        self.name = name

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

    def on(self, event: str, fn: Callable, priority: int = 0):
        return self.session.bus.on(event, fn, priority)

    def register_tool(self, tool: AgentTool, roles: tuple = ("main",)) -> None:
        self.session.register_tool(tool, roles)

    def register_command(
        self, name: str, fn: Callable, description: str = "", concurrent: bool = False, *, argument_hint: str = ""
    ) -> None:
        """concurrent=True for commands that only read state (they may run while the agent works)."""
        self.session.commands.register(name, fn, description, source=self.name, concurrent=concurrent, argument_hint=argument_hint)

    def add_system_section(self, name: str, fn: Callable[[str], str | None], order: int = 50) -> None:
        """fn(role) -> text for the system prompt (role: main | subagent | teammate)."""
        self.session.sections.append((order, name, fn))

    def register_permission_policy(self, policy) -> None:
        self.session.register_permission_policy(policy)

    def add_runtime_system_section(self, name: str, fn: Callable, order: int = 50) -> None:
        """fn(RuntimeContext) -> text; construction errors reject the candidate."""
        self.session.runtime_sections.append((order, name, fn))

    def notify(self, text: str, mode: str = "steer", source: str | None = None) -> None:
        self.session.notify(text, mode=mode, source=source or self.name)

    def append_entry(self, kind: str, data: Any) -> None:
        self.session.store.append("custom", kind=kind, data=data)

    def entries(self, kind: str) -> list:
        return self.session.store.custom(kind)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

BUILTIN_FEATURES = [
    "permission",
    "search",
    "fetch",
    "todo",
    "ask_user",
    "subagent",
    "skills",
    "memory",
    "tasks",
    "background",
    "schedule",
    "team",
    "mcp",
    "workflow",
    "goal",
]


def load_builtin_features(session: AgentSession) -> None:
    for name in BUILTIN_FEATURES:
        if not session.settings.feature_enabled(name):
            continue
        mod = importlib.import_module(f"virgo.features.{name}")
        mod.setup(ExtensionAPI(session, name))


def load_user_extensions(session: AgentSession) -> None:
    for d in session.paths.both("extensions"):
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.py")):
            _load_file(session, path)


def _load_file(session: AgentSession, path: Path) -> None:
    try:
        spec = importlib.util.spec_from_file_location(f"virgo_ext_{path.stem}", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "setup"):
            mod.setup(ExtensionAPI(session, path.stem))
    except Exception as e:  # noqa: BLE001
        session.report_error(e, phase="extension.load", code="EXTENSION_LOAD_FAILED",
                             context={"extension": str(path)})


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


def install_command_hooks(session: AgentSession) -> None:
    hooks: dict = session.settings.get("hooks", {}) or {}
    cwd = session.cwd

    def base(event: str, store=None) -> dict:
        return {"hook_event_name": event, "cwd": str(cwd), "session_id": (store or session.store).id}

    def run(hook, payload, index, *, store=None):
        return _run_hook(hook, payload, cwd, hook_index=index,
                         on_error=lambda details: session.report_error(details, phase="extension.command_hook",
                                                                        store=store))

    def source(ev):
        return ev.review_source.root if ev.review_source is not None else session.store

    for index, hook in enumerate(hooks.get("PreToolUse", []), 1):

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
            return Block(err or out or f"Blocked by PreToolUse hook #{index}.") if code == 2 else None

        session.bus.on("tool_call", pre, priority=50)

    for index, hook in enumerate(hooks.get("PostToolUse", []), 1):

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
                return _with_feedback(res, f"[hook feedback] {err or out}")
            return None

        session.bus.on("tool_result", post)

    for index, hook in enumerate(hooks.get("UserPromptSubmit", []), 1):

        def submit(text: str, _s, hook=hook, index=index):
            code, out, err = run(hook, {**base("UserPromptSubmit"), "prompt": text}, index)
            if code == 2:
                return Handled(f"Prompt blocked by hook: {err or out}")
            if code == 0 and out:
                return f"{text}\n\n<hook_context>\n{out}\n</hook_context>"
            return None

        session.bus.on("input", submit)

    for index, hook in enumerate(hooks.get("Stop", []), 1):

        def stop(rt, hook=hook, index=index):
            if rt.role != "main":
                return None
            code, out, err = run(hook, {**base("Stop", rt.accounting_store), "last_message": rt.agent.last_text},
                                 index, store=rt.accounting_store)
            return Continue(err or out) if code == 2 and (err or out) else None

        session.bus.on("agent_stop", stop, priority=20)
