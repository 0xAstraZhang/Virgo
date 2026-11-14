"""Slash commands and prompt templates.

Prompt templates are Markdown files in ``~/.virgo/prompts/`` or ``.virgo/prompts/``; ``review.md``
becomes ``/review``. Arguments: ``$1`` ``$2`` …, ``$@`` / ``$ARGUMENTS`` (all), ``${1:-default}``,
``${@:2}`` (from the 2nd on).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..i18n import Translator, available_languages

if TYPE_CHECKING:
    from .session import AgentSession


@dataclass
class Command:
    name: str
    fn: Callable  # fn(session, args: str) -> None
    description: str = ""
    source: str = "builtin"
    argument_hint: str = field(default="", kw_only=True)
    description_key: str = field(default="", kw_only=True)
    argument_hint_key: str = field(default="", kw_only=True)
    concurrent: bool = False  # only reads state: may run while the agent is busy


class Commands:
    def __init__(self, translator: Translator | None = None):
        self._cmds: dict[str, Command] = {}
        self.translator = translator or Translator()

    def register(
        self,
        name: str,
        fn: Callable,
        description: str = "",
        source: str = "builtin",
        concurrent: bool = False,
        *,
        argument_hint: str = "",
        description_key: str = "",
        argument_hint_key: str = "",
    ) -> None:
        self._cmds[name.lstrip("/")] = Command(
            name.lstrip("/"), fn, description, source, concurrent, argument_hint=argument_hint,
            description_key=description_key, argument_hint_key=argument_hint_key,
        )

    def _localized(self, command: Command) -> Command:
        fields = {}
        for attr in ("description", "argument_hint"):
            key = getattr(command, f"{attr}_key")
            if key and self.translator.has(key):
                fields[attr] = self.translator(key)
        if command.name == "language" and command.source == "builtin":
            fields["argument_hint"] = "[" + "|".join(pack.name for pack in available_languages()) + "]"
        return replace(command, **fields) if fields else command

    def get(self, name: str) -> Command | None:
        command = self._cmds.get(name)
        return self._localized(command) if command is not None else None

    def all(self) -> list[Command]:
        return [self._localized(command) for command in sorted(self._cmds.values(), key=lambda c: c.name)]

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
    from ..diagnostics import error_summary
    from .auth_commands import login, logout
    from .language_commands import configure_language
    from .preferences import configure_preference
    from .shell_commands import configure_shell

    c = session.commands

    def help_(s, args):
        lines = [s.t("harness.help.title")]
        for cmd in s.commands.all():
            lines.append(f"  /{cmd.name:<14} {cmd.description}")
        for name, (desc, _) in sorted(s.templates.items()):
            lines.append(f"  /{name:<14} {desc or s.t('harness.help.template')}")
        lines.append(s.t("harness.help.instructions"))
        s.ui.print("\n".join(lines))

    def model(s, args):
        if not args.strip() and s.ui.supports_preferences:
            configure_preference(s, "model")
            return
        if args.strip():
            s.set_model(args.strip())
        s.ui.print(s.t("harness.model.current", model=s.model.spec, small=s.small_model.spec, effort=s.effort))

    def effort(s, args):
        a = args.strip()
        if not a and s.ui.supports_preferences:
            configure_preference(s, "effort")
            return
        if a:
            if a not in ("low", "medium", "high", "xhigh", "max"):
                s.ui.print(s.t("harness.effort.invalid"), "error")
                return
            s.effort = a
        s.ui.print(s.t("harness.effort.current", effort=s.effort))

    def compact(s, args):
        r = s.compact(focus=args.strip() or None)
        s.ui.print(
            s.t("harness.compact.empty") if r is None else s.t("harness.compact.done", tokens=r["tokens_before"])
        )

    def new(s, args):
        s.new_session()
        s.ui.print(s.t("harness.session.new"))

    def session_(s, args):
        st = s.store
        s.ui.print(
            s.t("harness.session.info", id=st.id, path=st.path or s.t("harness.session.memory"),
                messages=len(s.main.messages), tokens=s.context_tokens())
        )

    def diagnostics(s, args):
        records = s.diagnostics.recent()
        query = args.strip()
        if query:
            matches = [record for record in records if record["error_id"].startswith(query)]
            if len(matches) != 1:
                s.ui.command_result(s.t("harness.diagnostics.unknown"))
                return
            import json

            s.ui.command_result(json.dumps(matches[0], ensure_ascii=False, indent=2))
        else:
            lines = [s.t("harness.diagnostics.path", path=s.diagnostics.path)]
            lines.extend(error_summary(record, t=s.t) for record in records[-10:])
            if not records:
                lines.append(s.t("harness.diagnostics.empty"))
            lines.append(s.t("harness.diagnostics.help"))
            s.ui.command_result("\n".join(lines))

    def sessions(s, args):
        items = s.list_sessions()
        if not items:
            s.ui.print(s.t("harness.sessions.empty"))
            return
        listed = items[:20]
        choice = args.strip()
        if not choice:
            for i, it in enumerate(listed, 1):
                s.ui.print(s.t("harness.sessions.row", index=i, title=it["title"], messages=it["messages"], file=it["path"].name))
            s.ui.print(s.t("harness.sessions.help"))
            return
        # Only listed numbers are valid: 0 or a negative value must not index from the end.
        if not choice.isdecimal() or not 1 <= int(choice) <= len(listed):
            s.ui.print(s.t("harness.sessions.invalid"), "error")
            return
        it = listed[int(choice) - 1]
        s.open_session(it["path"])
        s.ui.print(s.t("harness.sessions.resumed", file=it["path"].name, messages=len(s.main.messages)))

    c.register("help", help_, "show commands", description_key="harness.commands.help", concurrent=True)
    c.register("language", configure_language, "show or change the interface language",
               description_key="language.description", argument_hint="[中文|English]")
    c.register("login", login, "sign in to an account or configure an API (/login [auth|api] [provider])", description_key="harness.commands.login", argument_hint="[auth|api] [provider]")
    c.register("logout", logout, "remove saved credentials (/logout [provider|all])", description_key="harness.commands.logout", argument_hint="[provider|all]")
    c.register("shell", configure_shell, "detect and choose the command shell (/shell [name|path|auto])", description_key="harness.commands.shell", argument_hint="[name|path|auto]", argument_hint_key="harness.hints.shell")
    c.register("model", model, "show or switch the model (/model provider/id)", description_key="harness.commands.model", argument_hint="[provider/id]")
    c.register("effort", effort, "show or set reasoning effort", description_key="harness.commands.effort", argument_hint="[low|medium|high|xhigh|max]")
    c.register("compact", compact, "summarize older context now (/compact [focus])", description_key="harness.commands.compact", argument_hint="[focus]", argument_hint_key="harness.hints.compact")
    c.register("new", new, "start a fresh session", description_key="harness.commands.new")
    c.register("session", session_, "show session info", description_key="harness.commands.session", concurrent=True)
    c.register("diagnostics", diagnostics, "show diagnostic log and recent errors", description_key="harness.commands.diagnostics", concurrent=True,
               argument_hint="[error-id]", argument_hint_key="harness.hints.diagnostics")
    c.register("sessions", sessions, "browse saved sessions (g toggles workspace grouping in fullscreen)", description_key="harness.commands.sessions", argument_hint="[number]", argument_hint_key="harness.hints.sessions")
