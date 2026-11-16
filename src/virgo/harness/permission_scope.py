"""Approval boundaries and resolved path targets for exact-operation grants.

Shell syntax is not an authorization boundary. The functions here only derive a
stable identity for a repeated approval request; they never decide that a command
is safe or read-only.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from ..i18n import Translator


@dataclass(frozen=True)
class ApprovalBoundary:
    """A policy result whose identity never depends on translated display text."""

    key: str
    values: dict[str, object] = field(default_factory=dict)

    def describe(self, t=None) -> str:
        t = t or Translator()
        return t(self.key, **self.values) or Translator()(self.key, **self.values)


def split_segments(command: str, *, redirects: bool = False) -> list[list[str]] | None:
    """Return simple shell commands; opaque expansion and control flow cannot be bound."""
    if any(token in command for token in ("$", "`", "(", ")", "{", "}")):
        return None
    try:
        lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|<>")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    segments, words = [], []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&"}:
            if words:
                segments.append(words)
            words = []
        elif token and set(token) <= set("<>&|;"):
            if not redirects or token not in {"<", ">", ">>", "<>"}:
                return None
            words.append(token)
        else:
            words.append(token)
    if words:
        segments.append(words)
    return segments or None


_PATH_OPTIONS = {
    "cp": "t",
    "mv": "t",
    "ln": "t",
    "touch": "r",
    "truncate": "r",
    "sort": "To",
    "git": "C",
    "grep": "f",
    "rg": "f",
    "sed": "f",
    "curl": "oKTDd",
    "wget": "OP",
}
_OPAQUE_ENV = {"PATH", "CDPATH", "ENV", "BASH_ENV", "SHELLOPTS", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"}


def _strip_assignments(words: list[str]) -> list[str] | None:
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        key = words[0].split("=", 1)[0]
        if key in _OPAQUE_ENV or key.startswith("GIT_"):
            return None
        words = words[1:]
    return words


def _argument_value(name: str, argument: str) -> str:
    value = argument.split("=", 1)[-1] if "=" in argument else argument
    if argument.startswith("-") and not argument.startswith("--"):
        for position, option in enumerate(argument[1:], 2):
            if option in _PATH_OPTIONS.get(name, ""):
                if argument[position:]:
                    value = argument[position:]
                break
    return re.sub(r"^-[A-Za-z]+(?=[/~]|\.{1,2}/)", "", value)


def command_path_targets(command: str, cwd: Path) -> tuple[str, ...] | None:
    """Bind repeated approvals to static path targets; dynamic paths cannot be remembered."""
    segments = split_segments(command, redirects=True)
    if not segments or any(char in command for char in "*?["):
        return None
    current = cwd.resolve()
    targets = []
    try:
        for raw_words in segments:
            words = _strip_assignments(raw_words)
            if not words:
                return None
            executable, *args = words
            name = os.path.basename(executable)
            if "/" in executable:
                targets.append(str((current / os.path.expanduser(executable)).resolve()))
            local_cwd = current
            if name == "git":
                while args and args[0].startswith("-C"):
                    if args[0] == "-C":
                        if len(args) < 2:
                            return None
                        directory, args = args[1], args[2:]
                    else:
                        directory, args = args[0][2:], args[1:]
                    local_cwd = (local_cwd / os.path.expanduser(directory)).resolve()
                    targets.append(str(local_cwd))
            for argument in args:
                value = _argument_value(name, argument)
                if value.startswith("@"):
                    value = value[1:]
                if not value or value.startswith("-") or value in {"<", ">", ">>", "<>"} or "://" in value:
                    continue
                targets.append(str((local_cwd / os.path.expanduser(value)).resolve()))
            if name == "cd":
                if (
                    len(args) != 1
                    or os.environ.get("CDPATH")
                    or any(token in command for token in (";", "\n", "|"))
                    or re.search(r"(?<!&)&(?!&)", command)
                ):
                    return None
                current = (current / os.path.expanduser(args[0])).resolve()
        return tuple(targets)
    except (OSError, RuntimeError, ValueError):
        return None
