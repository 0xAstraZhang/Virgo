"""Slash commands and prompt templates.

Prompt templates are Markdown files in ``~/.virgo/prompts/`` or ``.virgo/prompts/``; ``review.md``
becomes ``/review``. Arguments: ``$1`` ``$2`` …, ``$@`` / ``$ARGUMENTS`` (all), ``${1:-default}``,
``${@:2}`` (from the 2nd on).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .session import AgentSession


@dataclass
class Command:
    name: str
    fn: Callable  # fn(session, args: str) -> None
    description: str = ""
    source: str = "builtin"
    argument_hint: str = field(default="", kw_only=True)
    concurrent: bool = False  # only reads state: may run while the agent is busy


class Commands:
    def __init__(self):
        self._cmds: dict[str, Command] = {}

    def register(
        self,
        name: str,
        fn: Callable,
        description: str = "",
        source: str = "builtin",
        concurrent: bool = False,
        *,
        argument_hint: str = "",
    ) -> None:
        self._cmds[name.lstrip("/")] = Command(name.lstrip("/"), fn, description, source, concurrent, argument_hint=argument_hint)

    def get(self, name: str) -> Command | None:
        return self._cmds.get(name)

    def all(self) -> list[Command]:
        return sorted(self._cmds.values(), key=lambda c: c.name)

    def names(self) -> list[str]:
        return sorted(self._cmds)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a ``---``-delimited YAML header from the body. Tolerates a missing/broken header."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                meta = yaml.safe_load("".join(lines[1:i])) or {}
            except yaml.YAMLError:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), "".join(lines[i + 1 :])
    return {}, text


def load_templates(dirs: list[Path]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for d in dirs:
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
                out[f.stem] = (str(meta.get("description", "")), body.strip())
    return out


_VAR = re.compile(r"\$\{(\d+):-([^}]*)\}|\$\{@:(\d+)(?::(\d+))?\}|\$(\d+)|\$@|\$ARGUMENTS")


def expand_template(body: str, args: str) -> str:
    try:
        argv = shlex.split(args)
    except ValueError:
        argv = args.split()

    def sub(m: re.Match) -> str:
        if m.group(1):
            i = int(m.group(1))
            return argv[i - 1] if len(argv) >= i else m.group(2)
        if m.group(3):
            start = int(m.group(3)) - 1
            length = int(m.group(4)) if m.group(4) else None
            sel = argv[start:] if length is None else argv[start : start + length]
            return " ".join(sel)
        if m.group(5):
            i = int(m.group(5))
            return argv[i - 1] if len(argv) >= i else ""
        return args.strip()

    out = _VAR.sub(sub, body)
    if args.strip() and not _VAR.search(body):
        out = f"{out}\n\n{args.strip()}"
    return out


# ---------------------------------------------------------------------------
# built-in commands
# ---------------------------------------------------------------------------


def register_builtins(session: AgentSession) -> None:
    from .auth_commands import login, logout
    from .preferences import configure_preference
    from .shell_commands import configure_shell

    c = session.commands

    def help_(s, args):
        lines = ["Commands:"]
        for cmd in s.commands.all():
            lines.append(f"  /{cmd.name:<14} {cmd.description}")
        for name, (desc, _) in sorted(s.templates.items()):
            lines.append(f"  /{name:<14} {desc or '(prompt template)'}")
        lines.append("Type text to chat. While the agent works, typed lines are sent as steering messages.")
        s.ui.print("\n".join(lines))

    def model(s, args):
        if not args.strip() and s.ui.supports_preferences:
            configure_preference(s, "model")
            return
        if args.strip():
            s.set_model(args.strip())
        s.ui.print(f"model: {s.model.spec}  (small: {s.small_model.spec}, effort: {s.effort})")

    def effort(s, args):
        a = args.strip()
        if not a and s.ui.supports_preferences:
            configure_preference(s, "effort")
            return
        if a:
            if a not in ("low", "medium", "high", "xhigh", "max"):
                s.ui.print("effort must be one of: low, medium, high, xhigh, max", "error")
                return
            s.effort = a
        s.ui.print(f"effort: {s.effort}")

    def compact(s, args):
        r = s.compact(focus=args.strip() or None)
        s.ui.print(
            "Nothing to compact." if r is None else f"Compacted: ~{r['tokens_before']} tokens summarized."
        )

    def new(s, args):
        s.new_session()
        s.ui.print("Started a new session.")

    def session_(s, args):
        st = s.store
        s.ui.print(
            f"session {st.id}\nfile: {st.path or '(not persisted)'}\nmessages: {len(s.main.messages)}"
            f"\nestimated context tokens: {s.context_tokens()}"
        )

    def sessions(s, args):
        items = s.list_sessions()
        if not items:
            s.ui.print("No saved sessions for this directory.")
            return
        listed = items[:20]
        choice = args.strip()
        if not choice:
            for i, it in enumerate(listed, 1):
                s.ui.print(f"  {i:>2}. {it['title']}  ({it['messages']} msgs)  {it['path'].name}")
            s.ui.print("Use /sessions <number> to open one.")
            return
        # Only listed numbers are valid: 0 or a negative value must not index from the end.
        if not choice.isdecimal() or not 1 <= int(choice) <= len(listed):
            s.ui.print("Invalid session number.", "error")
            return
        it = listed[int(choice) - 1]
        s.open_session(it["path"])
        s.ui.print(f"Resumed {it['path'].name} ({len(s.main.messages)} messages).")

    c.register("help", help_, "show commands", concurrent=True)
    c.register("login", login, "sign in to an account or configure an API (/login [auth|api] [provider])", argument_hint="[auth|api] [provider]")
    c.register("logout", logout, "remove saved credentials (/logout [provider|all])", argument_hint="[provider|all]")
    c.register("shell", configure_shell, "detect and choose the command shell (/shell [name|path|auto])", argument_hint="[name|path|auto]")
    c.register("model", model, "show or switch the model (/model provider/id)", argument_hint="[provider/id]")
    c.register("effort", effort, "show or set reasoning effort", argument_hint="[low|medium|high|xhigh|max]")
    c.register("compact", compact, "summarize older context now (/compact [focus])", argument_hint="[focus]")
    c.register("new", new, "start a fresh session")
    c.register("session", session_, "show session info", concurrent=True)
    c.register("sessions", sessions, "browse saved sessions (g toggles workspace grouping in fullscreen)", argument_hint="[number]")
