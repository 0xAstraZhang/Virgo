"""Execution mode re-exports and legacy preset migration helper (removed in S7-02)."""
from __future__ import annotations

import re

from ..execution_policy import (
    DEFAULT_MODES,
    HOST_MODE,
    READ_ONLY_MODE,
    WORKSPACE_EDIT_MODE,
    ExecutionMode,
    PermissionSnapshot,
    default_execution_mode,
    is_native_windows,
    parse_network,
)

__all__ = [
    "DEFAULT_MODES",
    "HOST_MODE",
    "READ_ONLY_MODE",
    "WORKSPACE_EDIT_MODE",
    "ExecutionMode",
    "PermissionSnapshot",
    "default_execution_mode",
    "is_native_windows",
    "migrate_preset",
    "parse_network",
]


def migrate_preset(identifier: str, presets: dict) -> str:
    """Translate a native preset when the fixed mode has the same authority."""
    entry = presets.get(identifier, {})
    if identifier not in DEFAULT_MODES:
        if identifier not in presets:
            raise ValueError(f"Unknown retired permission preset {identifier!r}; choose execution.mode")
        if (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", identifier)
                or identifier in {"ask", "never", "revoke", "show", "status", "sandbox", "list", "save"}):
            raise ValueError(f"Cannot migrate invalid or reserved permission preset {identifier!r}")
    if not isinstance(entry, dict) or set(entry) - {"name", "readOnly", "workspaceOnly", "networkAllowed"}:
        raise ValueError(f"Invalid legacy permission flags for {identifier!r}; choose execution.mode")
    flags = {"readOnly": identifier == "read-only", "workspaceOnly": identifier != "no-limit",
             "networkAllowed": True} if identifier in DEFAULT_MODES else {}
    flags.update(entry)
    if any(type(flags.get(k)) is not bool for k in ("readOnly", "workspaceOnly", "networkAllowed")):
        raise ValueError(f"Invalid legacy permission flags for {identifier!r}; choose execution.mode")
    if flags["networkAllowed"] and not flags["readOnly"]:
        return "workspace-edit" if flags["workspaceOnly"] else "no-limit"
    raise ValueError(f"Cannot migrate legacy preset {identifier!r}: no fixed mode has the same "
                     "filesystem, web and Shell network permissions; choose execution.mode explicitly.")
