"""Fixed permission modes and legacy resource decoding. No backend imports, discovery or process startup."""
from __future__ import annotations

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

    def config(self) -> dict:
        return {"name": self.name, "readPaths": list(self.read_paths), "writePaths": list(self.write_paths),
                "networkAllowed": network_config(self.network)}

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


def migrate_preset(identifier: str, presets: dict, workspace: Path) -> str:
    """Legacy networkAllowed covered web and Shell together. Preserve both."""
    entry = presets.get(identifier, {})
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid legacy permission preset {identifier!r}")
    builtin = DEFAULT_MODES.get(identifier)
    if builtin is None and identifier not in presets:
        raise ValueError(f"Unknown retired permission preset {identifier!r}; choose execution.mode")
    if identifier == "no-limit" or "readOnly" in entry or "workspaceOnly" in entry:
        if set(entry) - {"name", "readOnly", "workspaceOnly", "networkAllowed"}:
            raise ValueError(f"Invalid legacy permission flags for {identifier!r}")
        defaults = {"readOnly": identifier == "read-only", "workspaceOnly": identifier != "no-limit",
                    "networkAllowed": True} if builtin else {}
        flags = {**defaults, **entry}
        if any(type(flags.get(k)) is not bool for k in ("readOnly", "workspaceOnly", "networkAllowed")):
            raise ValueError(f"Invalid legacy permission flags for {identifier!r}")
        if flags["networkAllowed"] and not flags["readOnly"]:
            return "workspace-edit" if flags["workspaceOnly"] else "no-limit"
    else:
        defaults = {**builtin.config(), "networkAllowed": True} if builtin else {}
        flags = {**defaults, **entry}
        if set(flags) - {"name", "readPaths", "writePaths", "networkAllowed"}:
            raise ValueError(f"Invalid legacy permission paths for {identifier!r}")
        roots = []
        for field in ("readPaths", "writePaths"):
            paths = flags.get(field)
            if not isinstance(paths, list) or any(not isinstance(p, str) or not p or "\0" in p for p in paths):
                raise ValueError(f"{field} must be a list of nonempty paths")
            roots.append({(workspace / Path(p).expanduser()).resolve() for p in paths})
        reads, writes = roots
        if (parse_network(flags.get("networkAllowed")) == FULL_NETWORK
                and workspace in writes and all(path.is_relative_to(workspace) for path in reads | writes)):
            return "workspace-edit"
    raise ValueError(f"Cannot migrate legacy preset {identifier!r}: no fixed mode has the same "
                     "filesystem, web and Shell network permissions. Select execution.mode explicitly.")


@dataclass(frozen=True)
class PermissionSnapshot:
    mode: PermissionPreset
    approval: str
    revision: int
