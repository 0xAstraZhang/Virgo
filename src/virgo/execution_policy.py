"""Immutable execution permissions, independent of tools and native backends.

Rules are lexical authorizations. They never inspect a model-supplied path on
the host. The executor must establish the corresponding operating-system
boundary before the process that resolves and opens that path is released.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

Access = Literal["read", "write"]
Effect = Literal["allow", "deny"]
_USER_HOME = Path.home()


def lexical_path(value: str | Path, cwd: Path, *, home: Path = _USER_HOME) -> Path:
    """Expand our captured home and normalize spelling without following links."""
    text = str(value)
    if "\0" in text:
        raise ValueError("A resource path cannot contain a null byte")
    if text.startswith("~"):
        if text == "~":
            text = str(home)
        elif text[1:2] in {"/", "\\"}:
            text = str(home / text[2:])
        else:
            raise ValueError("Resource paths support ~ for the current user, not ~username")
    path = Path(text)
    if not path.is_absolute():
        path = cwd / path
    path = Path(os.path.normpath(path))
    if not path.is_absolute():
        raise ValueError("Resource paths require an absolute working directory and cannot be drive-relative")
    return path


@dataclass(frozen=True)
class PathRule:
    path: Path
    access: Access
    effect: Effect = "allow"

    def __post_init__(self):
        object.__setattr__(self, "path", Path(os.path.normpath(self.path)))
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
        return not self.unrestricted and any(
            rule.effect == "deny" and rule.covers(target, access) for rule in self.filesystem
        )

    def allows(self, target: Path, access: Access) -> bool:
        if self.unrestricted:
            return True
        return not self.denied(target, access) and any(
            rule.effect == "allow" and rule.covers(target, access) for rule in self.filesystem
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
