"""The two fail-closed Harness checkpoints, independent of policy internals."""
from __future__ import annotations

from typing import Protocol

from ..agent import Block, ToolCallEvent


class PermissionPolicy(Protocol):
    def preflight(self, event: ToolCallEvent) -> Block | None: ...

    def before_execute(self, event: ToolCallEvent) -> Block | None: ...


class NoPermissions:
    """Only selected when the permission feature is explicitly disabled."""

    def preflight(self, event: ToolCallEvent) -> None:
        return None

    def before_execute(self, event: ToolCallEvent) -> None:
        return None
