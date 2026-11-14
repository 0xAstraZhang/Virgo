"""Shared permission modes and fail-closed checkpoints, independent of policy internals."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..agent import AgentTool, Block, ToolCallEvent
from ..i18n import Translator

MODES = ("auto-review", "accept-all", "plan")
_ALIASES = {"default": "auto-review", "accept-edits": "auto-review", "yolo": "accept-all"}
_PLAN_SIDE_EFFECTS = {"job_kill"}


def normalize_permission_mode(mode: str) -> str:
    mode = _ALIASES.get(mode, mode)
    if mode not in MODES:
        raise ValueError(f"permission mode must be one of {', '.join(MODES)}")
    return mode


def permission_mode_label(mode: str, t: Callable[[str], str] | None = None) -> str:
    t = t or Translator()
    return t("features.permission.mode." + normalize_permission_mode(mode))


def plan_tool_allowed(tool: AgentTool | None) -> bool:
    return bool(tool and (tool.name == "bash" or (tool.read_only and tool.name not in _PLAN_SIDE_EFFECTS)))


class PermissionPolicy(Protocol):
    def preflight(self, event: ToolCallEvent) -> Block | None: ...

    def before_execute(self, event: ToolCallEvent) -> Block | None: ...
