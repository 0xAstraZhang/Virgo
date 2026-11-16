"""Explicit approval choices and the execution-policy protocol."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..agent import Block, ToolCallEvent
from ..i18n import Translator

APPROVAL_POLICIES = ("ask", "never")


def normalize_approval_policy(value: str) -> str:
    if value not in APPROVAL_POLICIES:
        raise ValueError("approval policy must be ask or never; legacy permission modes are retired")
    return value


def approval_policy_label(value: str, t: Callable[[str], str] | None = None) -> str:
    return (t or Translator())("features.permission.approval." + normalize_approval_policy(value))


class PermissionPolicy(Protocol):
    def preflight(self, event: ToolCallEvent) -> Block | None: ...

    def before_execute(self, event: ToolCallEvent) -> Block | None: ...
