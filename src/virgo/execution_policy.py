"""Immutable execution permission policies and built-in execution modes.

This core module has no harness or OS-backend imports. Rules follow deny-first
precedence with a boolean network toggle.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

Access = Literal["read", "write"]
Effect = Literal["allow", "deny"]

PROTECTED_NAMES = (".git", ".virgo", "AGENTS.md", "CLAUDE.md")


def is_native_windows() -> bool:
    """Return True only for native Windows (WSL2 runs Linux Python)."""
    return sys.platform == "win32"


def default_execution_mode() -> str:
    """Native Windows defaults to No-Limit while its LPAC backend is experimental."""
    return "no-limit" if is_native_windows() else "workspace-edit"


def parse_network(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError("network_allowed must be a boolean")


def _normalize_path(path: Path) -> Path:
    text = os.path.normpath(path)
    # POSIX normpath preserves a leading '//' (implementation-defined root).
    # Collapse it so lexical comparisons cannot miss a leading '/' deny root.
    if os.name == "posix" and text.startswith("//"):
        text = "/" + text.lstrip("/")
    return Path(text)


def lexical_path(value: str | Path, cwd: Path, *, home: Path | None = None) -> Path:
    """Expand current-user ``~`` and normalize spelling without following symlinks."""
    text = str(value)
    if "\0" in text:
        raise ValueError("A resource path cannot contain a null byte")
    if text.startswith("~"):
        user_home = home if home is not None else Path.home()
        if text == "~":
            text = str(user_home)
        elif text[1:2] in {"/", "\\"}:
            suffix = text[2:].lstrip("/\\")
            if Path(suffix).drive or Path(suffix).is_absolute():
                raise ValueError("Resource path after ~ must be relative to the user home")
            text = str(user_home / suffix)
        else:
            raise ValueError("Resource paths support ~ for the current user, not ~username")
    path = Path(text)
    if not path.is_absolute():
        path = cwd / path
    path = _normalize_path(path)
    if not path.is_absolute():
        raise ValueError("Resource paths require an absolute working directory and cannot be drive-relative")
    return path


@dataclass(frozen=True)
class PathRule:
    path: Path
    access: Access
    effect: Effect = "allow"

    def __post_init__(self):
        object.__setattr__(self, "path", _normalize_path(Path(self.path)))
        if self.access not in {"read", "write"} or self.effect not in {"allow", "deny"}:
            raise ValueError("Path rules require read/write access and allow/deny effect")

    def covers(self, target: Path, access: Access) -> bool:
        if not target.is_relative_to(self.path):
            return False
        # Writable grants include reads. Denying reads also denies mutations;
        # denying writes leaves independently authorized reads available.
        return self.access == access or (self.effect, self.access) in {
            ("allow", "write"), ("deny", "read"),
        }

    def as_dict(self) -> dict:
        return {"path": str(self.path), "access": self.access, "effect": self.effect}


@dataclass(frozen=True)
class PermissionPolicy:
    filesystem: tuple[PathRule, ...]
    network_allowed: bool
    unrestricted: bool = False

    def __post_init__(self):
        if type(self.network_allowed) is not bool or type(self.unrestricted) is not bool:
            raise ValueError("Network and unrestricted permissions must be booleans")
        if self.unrestricted and not self.network_allowed:
            raise ValueError("Unrestricted execution cannot claim a network restriction")
        if any(not isinstance(rule, PathRule) for rule in self.filesystem):
            raise ValueError("Filesystem permissions must contain PathRule values")
        object.__setattr__(self, "filesystem", tuple(dict.fromkeys(self.filesystem)))

    def resolve(self, workspace: Path) -> PermissionPolicy:
        return replace(self, filesystem=tuple(
            replace(rule, path=lexical_path(rule.path, workspace)) for rule in self.filesystem
        ))

    def denied(self, target: Path, access: Access) -> bool:
        if self.unrestricted:
            return False
        normalized = _normalize_path(target)
        return any(rule.effect == "deny" and rule.covers(normalized, access) for rule in self.filesystem)

    def allows(self, target: Path, access: Access) -> bool:
        if self.unrestricted:
            return True
        normalized = _normalize_path(target)
        return not self.denied(normalized, access) and any(
            rule.effect == "allow" and rule.covers(normalized, access) for rule in self.filesystem
        )

    def allow_roots(self, access: Access) -> tuple[Path, ...]:
        return tuple(dict.fromkeys(rule.path for rule in self.filesystem
                                   if rule.effect == "allow" and (
                                       rule.access == access or access == "read" and rule.access == "write")))

    def deny_roots(self, access: Access) -> tuple[Path, ...]:
        return tuple(dict.fromkeys(rule.path for rule in self.filesystem
                                   if rule.effect == "deny" and (
                                       rule.access == access or access == "write" and rule.access == "read")))

    def with_rules(self, *rules: PathRule) -> PermissionPolicy:
        return replace(self, filesystem=(*self.filesystem, *rules))

    def as_dict(self) -> dict:
        return {"filesystem": [rule.as_dict() for rule in self.filesystem],
                "network_allowed": self.network_allowed, "unrestricted": self.unrestricted}


@dataclass(frozen=True)
class ExecutionMode:
    id: str
    name: str
    policy: PermissionPolicy
    experimental_on_windows: bool = False

    @property
    def web_allowed(self) -> bool:
        return True

    @property
    def read_paths(self) -> tuple[str, ...]:
        return tuple(str(path) for path in self.policy.allow_roots("read"))

    @property
    def write_paths(self) -> tuple[str, ...]:
        return tuple(str(path) for path in self.policy.allow_roots("write"))

    @property
    def network(self) -> bool:
        return self.policy.network_allowed

    def flags(self) -> dict:
        return {
            "read_paths": [str(path) for path in self.policy.allow_roots("read")],
            "write_paths": [str(path) for path in self.policy.allow_roots("write")],
            "read_deny_paths": [str(path) for path in self.policy.deny_roots("read")],
            "write_deny_paths": [str(path) for path in self.policy.deny_roots("write")],
            "network_allowed": self.policy.network_allowed,
            "unrestricted": self.policy.unrestricted,
            "web_allowed": self.web_allowed,
        }

    def resolve(self, workspace: Path) -> ExecutionMode:
        return replace(self, policy=self.policy.resolve(workspace))


HOST_MODE = ExecutionMode(
    "no-limit", "No-Limit",
    PermissionPolicy((), True, unrestricted=True),
)
READ_ONLY_MODE = ExecutionMode(
    "read-only", "Read-Only",
    PermissionPolicy((PathRule(Path("."), "read"),), False),
    experimental_on_windows=True,
)
WORKSPACE_EDIT_MODE = ExecutionMode(
    "workspace-edit", "Workspace-Edit",
    PermissionPolicy((PathRule(Path("."), "write"),), True),
    experimental_on_windows=True,
)
DEFAULT_MODES = {mode.id: mode for mode in (READ_ONLY_MODE, WORKSPACE_EDIT_MODE, HOST_MODE)}


@dataclass(frozen=True)
class PermissionSnapshot:
    mode: ExecutionMode
    approval: str
    revision: int
