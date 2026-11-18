"""Fixed permission modes and legacy resource decoding. No backend imports, discovery or process startup."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

NETWORK_DIRECTIONS = ("egress", "ingress", "hostLoopback")
FULL_NETWORK = frozenset(NETWORK_DIRECTIONS)


def parse_network(value: object) -> frozenset[str]:
    if isinstance(value, bool):
        return FULL_NETWORK if value else frozenset()
    if (not isinstance(value, list) or any(direction not in NETWORK_DIRECTIONS for direction in value)
            or len(set(value)) != len(value)):
        raise ValueError("networkAllowed must be boolean or a list of distinct egress, ingress, hostLoopback directions")
    return frozenset(value)


def network_config(directions: frozenset[str]) -> bool | list[str]:
    if not directions or directions == FULL_NETWORK:
        return bool(directions)
    return [direction for direction in NETWORK_DIRECTIONS if direction in directions]


@dataclass(frozen=True)
class PermissionPreset:
    id: str
    name: str
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    network: frozenset[str]

    @property
    def web_allowed(self) -> bool:
        return True

    def flags(self) -> dict:
        return {"read_paths": list(self.read_paths), "write_paths": list(self.write_paths),
                "network_allowed": network_config(self.network), "web_allowed": self.web_allowed}

    def resolve(self, workspace: Path) -> PermissionPreset:
        def paths(values):
            return tuple(dict.fromkeys(str((workspace / Path(value).expanduser()).resolve()) for value in values))
        return replace(self, read_paths=paths(self.read_paths), write_paths=paths(self.write_paths))


HOST_MODE = PermissionPreset("no-limit", "No-Limit", (), (), FULL_NETWORK)
DEFAULT_MODES = {mode.id: mode for mode in (
    PermissionPreset("read-only", "Read-Only", (".",), (), frozenset()),
    PermissionPreset("workspace-edit", "Workspace-Edit", (".",), (".",), FULL_NETWORK),
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
