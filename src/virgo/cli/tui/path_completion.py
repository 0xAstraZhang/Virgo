"""User-directed path completion; never grants the agent file access."""

from __future__ import annotations

import heapq
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from ...execution_policy import PathRule, PermissionPolicy, default_private_paths, lexical_path
from ...file_access import IGNORE_DIRS, MAX_WALK_FILES, check_read, walk_files

# Do not interpret email addresses or identifiers containing @ as mentions.
_MENTION = re.compile(r'(?<![\w@])@(?:"(?:\\.|[^"\\])*"?|[^\s"]*)')
MAX_CHOICES = 100


@dataclass(frozen=True)
class Mention:
    row: int
    start: int
    end: int
    query: str


def mention_at(line: str, row: int, column: int) -> Mention | None:
    """Find the mention at the cursor, including the suffix to be replaced."""
    for match in _MENTION.finditer(line):
        if not match.start() < column <= match.end():
            continue
        query = line[match.start() + 1:column]
        if query.startswith('"'):
            # Navigation can leave the cursor inside the closing quote.
            for value in (query, query + '"'):
                try:
                    query = json.loads(value)
                    break
                except ValueError:
                    continue
            else:
                query = query[1:]
        return Mention(row, match.start(), match.end(), query)
    return None


def quote_path(path: str, *, force: bool = False) -> str:
    """Keep a path with whitespace or quotes unambiguous in a chat message."""
    if force or path.startswith("@") or any(c.isspace() or c in '"\\' or ord(c) < 32 for c in path):
        return json.dumps(path, ensure_ascii=False)
    return path


def display_path(path: Path, cwd: Path, is_dir: bool) -> str:
    try:
        value = path.relative_to(cwd).as_posix()
    except ValueError:
        value = path.as_posix()
    return value + "/" if is_dir and not value.endswith("/") else value


@dataclass(frozen=True)
class SearchScope:
    directory: Path
    recursive: bool = False


def search_scope(cwd: Path, query: str) -> tuple[SearchScope, str]:
    """Bare names search the workspace; explicit paths browse one level."""
    if os.name == "nt":
        query = query.replace("\\", "/")
    if query in {".", "..", "~"}:
        return SearchScope(lexical_path(query, cwd)), ""
    if "/" in query:
        parent, _, name = query.rpartition("/")
        return SearchScope(lexical_path(parent + "/", cwd)), name
    return SearchScope(cwd, recursive=bool(query)), query


@dataclass(frozen=True)
class PathChoice:
    path: Path
    is_dir: bool


@dataclass(frozen=True)
class ScanResult:
    entries: tuple[PathChoice, ...] = ()
    available: bool = True
    limited: bool = False


def scan_paths(scope: SearchScope, cwd: Path, user_dir: Path, cancel: Event) -> ScanResult:
    """Collect only names and metadata, using a disposable discovery policy.

    Explicit external directory browsing is a trusted user operation, not an
    agent read grant. Keep credential paths excluded, avoid following directory
    symlinks, and reuse the Broker's ignore-aware bounded traversal.
    """
    try:
        root = scope.directory.resolve(strict=True)
        if not root.is_dir():
            return ScanResult(available=False)
        in_workspace = root.is_relative_to(cwd)
        policy = PermissionPolicy((
            PathRule(cwd if in_workspace else root, "read"),
            *(PathRule(path, "read", "deny") for path in default_private_paths(user_dir)),
        ), False)
        check_read(root, policy)
        # os.walk skips unreadable roots; distinguish them from empty ones.
        with os.scandir(root):
            pass
        entries = []
        scanned = 0
        for path, info in walk_files(
            root, policy, IGNORE_DIRS if in_workspace else set(),
            max_depth=None if scope.recursive else 1,
            respect_gitignore=in_workspace, gitignore_root=cwd if in_workspace else root,
            include_directories=True,
        ):
            if cancel.is_set():
                return ScanResult()
            scanned += 1
            if info is None or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                continue
            visible = scope.directory / path.relative_to(root)
            entries.append(PathChoice(visible, stat.S_ISDIR(info.st_mode)))
        return ScanResult(tuple(entries), limited=scanned >= MAX_WALK_FILES)
    except (OSError, ValueError, RuntimeError):
        return ScanResult(available=False)


def _subsequence(query: str, text: str) -> bool:
    chars = iter(text)
    return all(char in chars for char in query)


def matching_paths(result: ScanResult, scope: SearchScope, query: str) -> list[PathChoice]:
    if not result.available:
        return []
    needle = query.casefold()

    def score(choice: PathChoice):
        relative = choice.path.relative_to(scope.directory).as_posix()
        name = choice.path.name.casefold()
        path = relative.casefold()
        if not needle:
            quality = 0 if choice.is_dir else 1
        elif needle in (name, path):
            quality = 0
        elif name.startswith(needle):
            quality = 1
        elif path.startswith(needle):
            quality = 2
        elif needle in name:
            quality = 3
        elif needle in path:
            quality = 4
        elif _subsequence(needle, name):
            quality = 5
        elif _subsequence(needle, path):
            quality = 6
        else:
            return None
        return (quality, len(choice.path.relative_to(scope.directory).parts), len(path), path, relative)

    scored = ((rank, choice) for choice in result.entries if (rank := score(choice)) is not None)
    matches = [choice for _, choice in heapq.nsmallest(MAX_CHOICES, scored, key=lambda item: item[0])]
    if not needle and not scope.recursive:
        matches.insert(0, PathChoice(scope.directory, True))
    return matches


@dataclass
class PathCompletion:
    mention: Mention
    scope: SearchScope | None = None
    choices: list[PathChoice] = field(default_factory=list)
    cache: dict[SearchScope, ScanResult] = field(default_factory=dict)
    pending: set[SearchScope] = field(default_factory=set)
    restore: Path | None = None
    cancel: Event = field(default_factory=Event)
