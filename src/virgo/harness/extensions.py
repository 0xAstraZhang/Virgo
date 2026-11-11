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
import os
import re
import subprocess
import sys
import traceback
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agent.types import AgentTool, Block, Continue, ToolResult
from ..ai.types import TextContent

if TYPE_CHECKING:
    from .session import AgentSession

ROLES_ALL = ("main", "subagent", "teammate")


@dataclass
class Handled:
    """Returned by an `input` handler that fully handled the input."""

    message: str | None = None


class EventBus:
    def __init__(self, on_error: Callable[[str], None] | None = None):
        self._handlers: dict[str, list] = defaultdict(list)
        self._seq = 0
        self.on_error = on_error or (lambda msg: print(msg, file=sys.stderr))

    def on(self, event: str, fn: Callable, priority: int = 0) -> Callable[[], None]:
        self._seq += 1
        entry = (-priority, self._seq, fn)
        self._handlers[event].append(entry)
        self._handlers[event].sort(key=lambda e: (e[0], e[1]))
        return lambda: self._handlers[event].remove(entry)

    def has_handlers(self, event: str, *, exclude=None) -> bool:
        return any(fn != exclude for _, _, fn in self._handlers.get(event, []))

    def _call(self, event: str, fn: Callable, args: tuple) -> Any:
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 - one broken extension must not break the session
            self.on_error(f"[extension error in {event} handler {getattr(fn, '__qualname__', fn)}] {e}")
            if "VIRGO_DEBUG" in os.environ:
                traceback.print_exc()
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
        self, name: str, fn: Callable, description: str = "", concurrent: bool = False
    ) -> None:
        """concurrent=True for commands that only read state (they may run while the agent works)."""
        self.session.commands.register(name, fn, description, source=self.name, concurrent=concurrent)

    def add_system_section(self, name: str, fn: Callable[[str], str | None], order: int = 50) -> None:
        """fn(role) -> text for the system prompt (role: main | subagent | teammate)."""
        self.session.sections.append((order, name, fn))

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
    "todo",
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
        session.ui.print(f"[extension {path} failed to load: {e}]", "error")


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


def _run_hook(hook: dict, payload: dict, cwd: Path) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            hook["command"],
            shell=True,
            cwd=str(cwd),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=float(hook.get("timeout", 60)),
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "hook timed out"


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

    def base(event: str) -> dict:
        return {"hook_event_name": event, "cwd": str(cwd), "session_id": session.store.id}

    for hook in hooks.get("PreToolUse", []):

        def pre(ev, _s, hook=hook):
            if not _matches(hook.get("matcher"), ev.tool_name):
                return None
            code, out, err = _run_hook(
                hook,
                {**base("PreToolUse"), "tool_name": ev.tool_name, "tool_input": ev.args, "agent": ev.agent},
                cwd,
            )
            return Block(err or out or f"Blocked by hook: {hook['command']}") if code == 2 else None

        session.bus.on("tool_call", pre, priority=50)

    for hook in hooks.get("PostToolUse", []):

        def post(res: ToolResult, ev, _s, hook=hook):
            if not _matches(hook.get("matcher"), ev.tool_name):
                return None
            payload = {
                **base("PostToolUse"),
                "tool_name": ev.tool_name,
                "tool_input": ev.args,
                "tool_response": res.as_text(),
                "is_error": res.is_error,
            }
            code, out, err = _run_hook(hook, payload, cwd)
            if code == 2 and (err or out):
                return _with_feedback(res, f"[hook feedback] {err or out}")
            return None

        session.bus.on("tool_result", post)

    for hook in hooks.get("UserPromptSubmit", []):

        def submit(text: str, _s, hook=hook):
            code, out, err = _run_hook(hook, {**base("UserPromptSubmit"), "prompt": text}, cwd)
            if code == 2:
                return Handled(f"Prompt blocked by hook: {err or out}")
            if code == 0 and out:
                return f"{text}\n\n<hook_context>\n{out}\n</hook_context>"
            return None

        session.bus.on("input", submit)

    for hook in hooks.get("Stop", []):

        def stop(rt, hook=hook):
            if rt.role != "main":
                return None
            code, out, err = _run_hook(hook, {**base("Stop"), "last_message": rt.agent.last_text}, cwd)
            return Continue(err or out) if code == 2 and (err or out) else None

        session.bus.on("agent_stop", stop, priority=20)
