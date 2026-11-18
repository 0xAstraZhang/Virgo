"""Named resource policies. No backend imports, discovery or process startup."""
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

    def flags(self) -> dict:
        return {"read_paths": list(self.read_paths), "write_paths": list(self.write_paths),
                "network_allowed": network_config(self.network)}

    def config(self) -> dict:
        return {"name": self.name, "readPaths": list(self.read_paths), "writePaths": list(self.write_paths),
                "networkAllowed": network_config(self.network)}

    def resolve(self, workspace: Path) -> PermissionPreset:
        def paths(values):
            return tuple(dict.fromkeys(str((workspace / Path(value).expanduser()).resolve()) for value in values))
        return replace(self, read_paths=paths(self.read_paths), write_paths=paths(self.write_paths))


HOST_MODE = PermissionPreset("host", "Host", (), (), FULL_NETWORK)
RESERVED_PRESET_IDS = frozenset({"host", "no-limit", "ask", "never", "revoke", "show", "status", "sandbox", "list", "save"})
DEFAULT_MODES = {mode.id: mode for mode in (
    PermissionPreset("read-only", "Read-Only", (".",), (), FULL_NETWORK),
    PermissionPreset("workspace-edit", "Workspace-Edit", (".",), (".",), FULL_NETWORK),
)}


def parse_mode(identifier: str, value: dict) -> PermissionPreset:
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", identifier):
        raise ValueError("Permission preset IDs must be 1–64 letters, digits, underscores or hyphens")
    if identifier in RESERVED_PRESET_IDS:
        raise ValueError(f"Permission preset {identifier!r} is reserved; Host uses execution.provider=host")
    if not isinstance(value, dict) or set(value) - {"name", "readPaths", "writePaths", "networkAllowed"}:
        raise ValueError(f"Invalid permission preset {identifier!r}; migrate to readPaths/writePaths/networkAllowed")
    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ValueError("Permission preset names must be 1–100 printable characters")
    for field in ("readPaths", "writePaths"):
        paths = value.get(field)
        if not isinstance(paths, list) or any(not isinstance(p, str) or not p or "\0" in p for p in paths):
            raise ValueError(f"{field} must be a list of nonempty paths")
    return PermissionPreset(identifier, name.strip(), tuple(value["readPaths"]), tuple(value["writePaths"]),
                            parse_network(value.get("networkAllowed")))


def configured_modes(value: object) -> dict[str, PermissionPreset]:
    if not isinstance(value, dict):
        raise ValueError("permissions.presets must be an object keyed by stable preset ID")
    modes = dict(DEFAULT_MODES)
    for identifier, entry in value.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid permission preset {identifier!r}")
        modes[identifier] = parse_mode(identifier, {**(modes[identifier].config() if identifier in modes else {}), **entry})
    return modes


@dataclass(frozen=True)
class PermissionSnapshot:
    mode: PermissionPreset
    approval: str
    revision: int
