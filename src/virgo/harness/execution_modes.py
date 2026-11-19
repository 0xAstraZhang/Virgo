"""Named execution policies; no backend discovery or process startup."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from ..execution_policy import PathRule, PermissionPolicy


def parse_network(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("network_allowed must be boolean; directional network grants are unsupported")
    return value


def network_config(value: bool) -> bool:
    return parse_network(value)


@dataclass(frozen=True)
class PermissionPreset:
    id: str
    name: str
    policy: PermissionPolicy

    @property
    def read_paths(self) -> tuple[str, ...]:
        return tuple(str(path) for path in self.policy.allow_roots("read"))

    @property
    def write_paths(self) -> tuple[str, ...]:
        return tuple(str(path) for path in self.policy.allow_roots("write"))

    @property
    def network(self) -> bool:
        return self.policy.network_allowed

    @property
    def web_allowed(self) -> bool:
        return True

    def flags(self) -> dict:
        return {**self.policy.as_dict(), "web_allowed": self.web_allowed}

    def resolve(self, workspace: Path) -> PermissionPreset:
        return replace(self, policy=self.policy.resolve(workspace))


HOST_MODE = PermissionPreset("no-limit", "No-Limit", PermissionPolicy((), True, unrestricted=True))
DEFAULT_MODES = {mode.id: mode for mode in (
    PermissionPreset("read-only", "Read-Only", PermissionPolicy((PathRule(Path("."), "read"),), False)),
    PermissionPreset("workspace-edit", "Workspace-Edit", PermissionPolicy((PathRule(Path("."), "write"),), True)),
    HOST_MODE,
)}


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


@dataclass(frozen=True)
class PermissionSnapshot:
    mode: PermissionPreset
    approval: str
    revision: int
