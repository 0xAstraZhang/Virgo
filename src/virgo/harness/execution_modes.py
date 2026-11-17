"""Named resource policies. No backend imports, discovery or process startup."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PermissionPreset:
    id: str
    name: str
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    network_allowed: bool

    def flags(self) -> dict:
        return {"read_paths": list(self.read_paths), "write_paths": list(self.write_paths),
                "network_allowed": self.network_allowed}

    def config(self) -> dict:
        return {"name": self.name, "readPaths": list(self.read_paths), "writePaths": list(self.write_paths),
                "networkAllowed": self.network_allowed}

    def resolve(self, workspace: Path) -> PermissionPreset:
        def paths(values):
            return tuple(dict.fromkeys(str((workspace / Path(value).expanduser()).resolve()) for value in values))
        return PermissionPreset(self.id, self.name, paths(self.read_paths), paths(self.write_paths), self.network_allowed)


HOST_MODE = PermissionPreset("host", "Host", (), (), True)
RESERVED_PRESET_IDS = frozenset({"host", "no-limit", "ask", "never", "revoke", "show", "status", "sandbox", "list", "save"})
DEFAULT_MODES = {mode.id: mode for mode in (
    PermissionPreset("read-only", "Read-Only", (".",), (), True),
    PermissionPreset("workspace-edit", "Workspace-Edit", (".",), (".",), True),
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
    if type(value.get("networkAllowed")) is not bool:
        raise ValueError("networkAllowed must be boolean")
    return PermissionPreset(identifier, name.strip(), tuple(value["readPaths"]), tuple(value["writePaths"]),
                            value["networkAllowed"])


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
