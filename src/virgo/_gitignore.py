"""Lightweight, policy-safe .gitignore matching for Broker file traversal.

Pure Python with zero external subprocesses so Broker file tools enforce
PermissionPolicy directly and never execute untrusted repository hooks/config.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_GITIGNORE_BYTES = 512 * 1024


@dataclass(frozen=True)
class GitIgnoreRule:
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool
    anchored: bool


def _strip_trailing_spaces(line: str) -> str:
    end = len(line)
    while end > 0 and line[end - 1] == " ":
        slashes = 0
        idx = end - 2
        while idx >= 0 and line[idx] == "\\":
            slashes += 1
            idx -= 1
        if slashes % 2 == 1:
            break
        end -= 1
    return line[:end]


def _ends_with_unescaped_slash(line: str) -> bool:
    if not line.endswith("/"):
        return False
    slashes = 0
    idx = len(line) - 2
    while idx >= 0 and line[idx] == "\\":
        slashes += 1
        idx -= 1
    return slashes % 2 == 0


def _compile_glob(pattern: str) -> re.Pattern[str] | None:
    """Translate a gitignore glob pattern into a bounded-backtracking regex."""
    out: list[str] = ["^"]
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == "*":
            # Check for directory-spanning double-star forms: **/, /**/, /**, or standalone **.
            if i + 1 < n and pattern[i + 1] == "*":
                at_start = i == 0 or pattern[i - 1] == "/"
                j = i + 2
                while j < n and pattern[j] == "*":
                    j += 1
                at_end = j == n or pattern[j] == "/"
                if at_start and at_end:
                    # Collapse any consecutive `**/` segments into one.
                    while j + 2 < n and pattern[j] == "/" and pattern[j + 1 : j + 3] == "**":
                        k = j + 3
                        while k < n and pattern[k] == "*":
                            k += 1
                        if k == n or pattern[k] == "/":
                            j = k
                        else:
                            break
                    if i == 0 and j == n:
                        out.append(".*")
                        i = j
                        continue
                    if i == 0 and j < n and pattern[j] == "/":
                        out.append("(?:.+/)?")
                        i = j + 1
                        continue
                    if i > 0 and j == n:
                        # Preceding `/` was already emitted; match anything inside that dir.
                        out.append(".+")
                        i = j
                        continue
                    if i > 0 and j < n and pattern[j] == "/":
                        # Preceding `/` was already emitted; match zero or more subdirectories.
                        out.append("(?:.+/)?")
                        i = j + 1
                        continue
            # Regular wildcard(s) within one path segment.
            while i < n and pattern[i] == "*":
                i += 1
            out.append("[^/]*")
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == "[":
            # Parse character class safely; fall back to literal `[` if unclosed.
            j = i + 1
            negate = False
            if j < n and pattern[j] in ("!", "^"):
                negate = True
                j += 1
            body: list[str] = []
            if j < n and pattern[j] == "]":
                body.append(r"\]")
                j += 1
            closed = False
            while j < n:
                c = pattern[j]
                if c == "\\" and j + 1 < n:
                    body.append(re.escape(pattern[j + 1]))
                    j += 2
                    continue
                if c == "]":
                    closed = True
                    j += 1
                    break
                if c == "[":
                    body.append(r"\[")
                else:
                    body.append(c)
                j += 1
            if closed and body:
                inner = "".join(body)
                if negate:
                    out.append(f"(?!/)[^{inner}]")
                else:
                    out.append(f"(?!/)[{inner}]")
                i = j
                continue
            out.append(r"\[")
            i += 1
            continue
        out.append(re.escape(ch))
        i += 1
    out.append("$")
    try:
        return re.compile("".join(out))
    except re.error:
        return None


def parse_gitignore(text: str) -> tuple[GitIgnoreRule, ...]:
    """Parse the contents of a .gitignore or .git/info/exclude file."""
    rules: list[GitIgnoreRule] = []
    for raw in text.splitlines():
        line = _strip_trailing_spaces(raw)
        if not line or line.startswith("#"):
            continue
        negated = False
        if line.startswith("!"):
            negated = True
            line = line[1:]
            if not line:
                continue
        dir_only = False
        if _ends_with_unescaped_slash(line):
            dir_only = True
            line = line[:-1]
            if not line:
                continue
        anchored = False
        if line.startswith("/"):
            anchored = True
            line = line[1:]
            if not line:
                continue
        elif "/" in line:
            anchored = True
        regex = _compile_glob(line)
        if regex is not None:
            rules.append(GitIgnoreRule(regex=regex, negated=negated, dir_only=dir_only, anchored=anchored))
    return tuple(rules)


def matches_scopes(
    path: Path,
    is_dir: bool,
    scopes: list[tuple[Path, tuple[GitIgnoreRule, ...]]],
) -> bool:
    """Return True if ``path`` is ignored by the ordered ``(base_dir, rules)`` scopes."""
    if not scopes:
        return False
    name = path.name
    ignored = False
    for base_dir, rules in scopes:
        rel: str | None = None
        for rule in rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.anchored:
                if rel is None:
                    try:
                        rel = path.relative_to(base_dir).as_posix()
                    except ValueError:
                        break
                target = rel
            else:
                target = name
            if rule.regex.match(target) is not None:
                ignored = not rule.negated
    return ignored


def read_gitignore_rules(
    file_path: Path,
    read_file: Callable[[Path], bytes | None],
) -> tuple[GitIgnoreRule, ...]:
    """Safely read and parse a single gitignore file via a policy-checked reader."""
    try:
        raw = read_file(file_path)
    except Exception:  # noqa: BLE001 - unreadable/denied/symlink ignore files fail closed silently
        return ()
    if not raw or len(raw) > MAX_GITIGNORE_BYTES:
        return ()
    return parse_gitignore(raw.decode("utf-8", errors="replace"))


def load_ancestor_scopes(
    root_path: Path,
    read_file: Callable[[Path], bytes | None],
    *,
    can_descend: Callable[[Path], bool] | None = None,
    stop_root: Path | None = None,
) -> list[tuple[Path, tuple[GitIgnoreRule, ...]]]:
    """Load .git/info/exclude and .gitignore scopes from the enclosing repo/workspace down to ``root_path``."""
    resolved_stop = stop_root.resolve() if stop_root is not None else None
    chain: list[Path] = []
    git_root: Path | None = None
    current = root_path
    while True:
        if can_descend is not None and not can_descend(current):
            break
        chain.append(current)
        git_marker = current / ".git"
        try:
            has_git = git_marker.exists()
        except OSError:
            has_git = False
        if has_git:
            git_root = current
            break
        if resolved_stop is not None and current == resolved_stop:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    # If no enclosing .git was found and root_path isn't under stop_root, only use root_path itself.
    if git_root is None and (resolved_stop is None or not root_path.is_relative_to(resolved_stop)):
        chain = [root_path]

    scopes: list[tuple[Path, tuple[GitIgnoreRule, ...]]] = []
    if git_root is not None:
        git_dir = git_root / ".git"
        try:
            if git_dir.is_dir() and not git_dir.is_symlink():
                exclude = git_dir / "info" / "exclude"
                if exclude.is_file():
                    rules = read_gitignore_rules(exclude, read_file)
                    if rules:
                        scopes.append((git_root, rules))
        except OSError:
            pass

    for directory in reversed(chain):
        ignore_file = directory / ".gitignore"
        try:
            exists = ignore_file.is_file()
        except OSError:
            exists = False
        if exists:
            rules = read_gitignore_rules(ignore_file, read_file)
            if rules:
                scopes.append((directory, rules))
    return scopes
