"""Explicit approval choices and reusable read-only capability checks."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..agent import AgentTool, Block, ToolCallEvent
from ..i18n import Translator

APPROVAL_POLICIES = ("ask", "never")
_READ_ONLY_EXCLUDED = {"bash", "powershell", "job_kill", "todo_write"}


def normalize_approval_policy(value: str) -> str:
    if value not in APPROVAL_POLICIES:
        raise ValueError("approval policy must be ask or never; legacy permission modes are retired")
    return value


def approval_policy_label(value: str, t: Callable[[str], str] | None = None) -> str:
    return (t or Translator())("features.permission.approval." + normalize_approval_policy(value))


def read_only_tool_allowed(tool: AgentTool | None) -> bool:
    return bool(tool and tool.read_only and tool.name not in _READ_ONLY_EXCLUDED)


class PermissionPolicy(Protocol):
    def preflight(self, event: ToolCallEvent) -> Block | None: ...

    def before_execute(self, event: ToolCallEvent) -> Block | None: ...
