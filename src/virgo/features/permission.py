"""Permissions: decide allow / ask / deny for every tool call.

Modes: ``default`` (ask before changes), ``accept-edits`` (file edits inside the workspace are
allowed), ``plan`` (read-only), ``yolo`` (allow everything except the hard-deny list).

Order: hard deny -> deny rules -> plan mode -> allow rules / session approvals -> defaults.
Rules look like ``bash(npm test*)``, ``edit(src/**)``, ``read(~/.ssh/*)``, ``mcp__github__*``.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from pathlib import Path

from ..agent.types import Block, ToolCallEvent
from ..harness.extensions import ExtensionAPI

MODES = ("default", "accept-edits", "plan", "yolo")

HARD_DENY = [
    re.compile(
        r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rRfF][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|~|\$HOME)/?(\*)?(\s|$)"
    ),
    re.compile(r"\bmkfs(\.\w+)?\b"),
    re.compile(r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|disk)"),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    re.compile(r">\s*/dev/(sd|nvme|hd|disk)\w*"),
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r"\bchmod\s+(-R\s+)?[0-7]*777\s+/(\s|$)"),
]

READ_ONLY_COMMANDS = {
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "pwd",
    "echo",
    "printf",
    "grep",
    "egrep",
    "rg",
    "ag",
    "tree",
    "file",
    "stat",
    "du",
    "df",
    "which",
    "whereis",
    "type",
    "whoami",
    "id",
    "date",
    "uname",
    "hostname",
    "env",
    "printenv",
    "sort",
    "uniq",
    "cut",
    "tr",
    "diff",
    "cmp",
    "md5sum",
    "sha256sum",
    "basename",
    "dirname",
    "realpath",
    "readlink",
    "true",
    "false",
    "test",
    "[",
    "jq",
    "less",
    "more",
    "nl",
    "column",
    "seq",
    "find",
    "sed",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch",
    "git rev-parse",
    "git ls-files",
    "git blame",
    "git remote",
    "git describe",
    "git tag",
    "git config --get",
    "git grep",
    "python --version",
    "python3 --version",
    "node --version",
    "npm --version",
    "uv --version",
    "pip list",
    "pip show",
    "uv pip list",
    "npm ls",
    "cargo --version",
    "go version",
}
_UNSAFE_FLAGS = {"find": {"-delete", "-exec", "-execdir", "-ok", "-fprint", "-fprintf"}, "sed": {"-i"}}


def split_segments(command: str) -> list[list[str]] | None:
    """Split a shell command into simple commands. None if it uses constructs we can't vet."""
    if any(tok in command for tok in ("$(", "`", "<(", ">(")):
        return None
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    segs, cur = [], []
    for t in tokens:
        if t in (";", "&&", "||", "|", "&", ";;", "\n"):
            if cur:
                segs.append(cur)
            cur = []
        elif set(t) <= set("<>&|;"):
            if ">" in t:
                return None  # output redirection writes files
            cur.append(t)
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def is_read_only_command(command: str, extra: set[str] | None = None) -> bool:
    allowed = READ_ONLY_COMMANDS | (extra or set())
    segs = split_segments(command.replace("\n", " ; "))
    if not segs:
        return False
    for words in segs:
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]  # env assignments
        if not words:
            continue
        name = os.path.basename(words[0])
        flags = _UNSAFE_FLAGS.get(name)
        if flags and any(w in flags or (name == "sed" and w.startswith("-i")) for w in words[1:]):
            return False
        candidates = {name, " ".join([name, *words[1:2]]), " ".join([name, *words[1:3]])}
        if not (candidates & allowed):
            return False
    return True


def parse_rule(rule: str) -> tuple[str, str | None]:
    m = re.fullmatch(r"\s*([^()\s]+)\s*(?:\((.*)\))?\s*", rule)
    if not m:
        return rule, None
    return m.group(1), m.group(2)


def _subjects(ev: ToolCallEvent) -> list[str]:
    a = ev.args
    if ev.tool_name == "bash":
        return [a.get("command", "")]
    if isinstance(a.get("path"), str):
        p = Path(os.path.expanduser(a["path"]))
        absolute = (p if p.is_absolute() else ev.cwd / p).resolve()
        out = [a["path"], str(absolute)]
        try:
            out.append(str(absolute.relative_to(ev.cwd.resolve())))
        except ValueError:
            pass
        return out
    import json

    return [json.dumps(a, sort_keys=True, default=str)]


def rule_matches(rule: str, ev: ToolCallEvent) -> bool:
    tool, pattern = parse_rule(rule)
    if not fnmatch.fnmatchcase(ev.tool_name, tool):
        return False
    if pattern is None:
        return True
    pats = [pattern, os.path.expanduser(pattern)]
    return any(fnmatch.fnmatchcase(s, p) for s in _subjects(ev) for p in pats)


def inside(path: str, cwd: Path) -> bool:
    p = Path(os.path.expanduser(path))
    p = (p if p.is_absolute() else cwd / p).resolve()
    try:
        p.relative_to(cwd.resolve())
        return True
    except ValueError:
        return False


class Permissions:
    def __init__(self, api: ExtensionAPI):
        self.api = api
        self.session = api.session
        self.session_rules: list[str] = []
        self.agent_modes: dict[str, str] = {}  # per-agent overrides (e.g. teammates in plan mode)

    def mode_for(self, agent: str) -> str:
        return self.agent_modes.get(agent, self.session.permission_mode)

    def set_agent_mode(self, agent: str, mode: str | None) -> None:
        if mode is None:
            self.agent_modes.pop(agent, None)
        else:
            self.agent_modes[agent] = mode

    def decide(self, ev: ToolCallEvent) -> tuple[str, str]:
        s = self.session.settings
        tool = ev.tool
        mode = self.mode_for(ev.agent)
        if ev.tool_name == "bash":
            cmd = ev.args.get("command", "")
            for rx in HARD_DENY:
                if rx.search(cmd):
                    return "deny", "This command is blocked by Virgo's safety list."
        for rule in s.get("permissions.deny", []) or []:
            if rule_matches(rule, ev):
                return "deny", f"Denied by permission rule {rule!r}."
        read_only = bool(tool and tool.read_only)
        if ev.tool_name == "bash":
            extra = set(s.get("permissions.readOnlyCommands", []) or [])
            read_only = is_read_only_command(ev.args.get("command", ""), extra)
        if mode == "plan" and not read_only:
            return "deny", "Plan mode is read-only: investigate and plan, but do not modify anything yet."
        if read_only or mode == "yolo":
            return "allow", ""
        for rule in [*(s.get("permissions.allow", []) or []), *self.session_rules]:
            if rule_matches(rule, ev):
                return "allow", ""
        if ev.tool_name in ("write", "edit"):
            if not inside(ev.args.get("path", ""), ev.cwd):
                return "ask", "writes outside the workspace"
            return ("allow", "") if mode == "accept-edits" else ("ask", "modifies a file")
        return "ask", "may change your system" if ev.tool_name == "bash" else "is not marked read-only"

    def remember(self, ev: ToolCallEvent) -> str:
        if ev.tool_name == "bash":
            words = (ev.args.get("command", "").split() or [""])[:2]
            rule = f"bash({' '.join(words)}*)"
        elif ev.tool_name in ("write", "edit"):
            rule = f"{ev.tool_name}({ev.cwd}/**)"
        else:
            rule = ev.tool_name
        self.session_rules.append(rule)
        return rule

    def on_tool_call(self, ev: ToolCallEvent, _session):
        decision, reason = self.decide(ev)
        if decision == "allow":
            return None
        if decision == "deny":
            return Block(reason)
        summary = describe(ev)
        who = "" if ev.agent == "main" else f"[{ev.agent}] "
        answer = self.session.ui.confirm(
            f"{who}{summary}\n  ({reason})  Allow? [y]es / [n]o / [a]lways this session", ev.agent
        )
        if answer is None:
            return Block(
                f"Permission required ({reason}) but nobody is available to approve it. "
                "Ask the user to rerun with an allow rule or a different --permission-mode."
            )
        if answer == "a":
            self.remember(ev)
            return None
        if answer == "y":
            return None
        return Block(
            "The user denied this tool call. Ask what they want instead, or try a different approach."
        )


def describe(ev: ToolCallEvent) -> str:
    a = ev.args
    if ev.tool_name == "bash":
        return f"bash: {a.get('command', '')}"
    if "path" in a:
        return f"{ev.tool_name}: {a['path']}"
    import json

    text = json.dumps(a, ensure_ascii=False, default=str)
    return f"{ev.tool_name}: {text[:200]}"


def setup(api: ExtensionAPI) -> None:
    perms = Permissions(api)
    api.services["permissions"] = perms
    api.on("tool_call", perms.on_tool_call, priority=100)

    def cmd(session, args):
        a = args.strip()
        if a:
            if a not in MODES:
                api.ui.print(f"mode must be one of {', '.join(MODES)}", "error")
                return
            session.permission_mode = a
        s = session.settings
        lines = [f"permission mode: {session.permission_mode}"]
        for key in ("allow", "deny"):
            rules = s.get(f"permissions.{key}", []) or []
            if rules:
                lines.append(f"{key}: {', '.join(rules)}")
        if perms.session_rules:
            lines.append(f"approved this session: {', '.join(perms.session_rules)}")
        api.ui.print("\n".join(lines))

    api.register_command(
        "permissions", cmd, "show or set the permission mode (default|accept-edits|plan|yolo)"
    )
