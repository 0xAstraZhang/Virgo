"""User-owned, named filesystem/network permission presets."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionPreset:
    id: str
    name: str
    read_only: bool
    workspace_only: bool
    network_allowed: bool

    @property
    def unrestricted(self) -> bool:
        return not self.read_only and not self.workspace_only and self.network_allowed

    @property
    def legacy_mode(self) -> str:
        return "disabled" if self.unrestricted else "read-only" if self.read_only else "workspace-write"

    def flags(self) -> dict:
        return {"read_only": self.read_only, "workspace_only": self.workspace_only,
                "network_allowed": self.network_allowed}

    def config(self) -> dict:
        return {"name": self.name, "readOnly": self.read_only, "workspaceOnly": self.workspace_only,
                "networkAllowed": self.network_allowed}


# Compatibility alias for extensions written against the 0.2 name.
PermissionMode = PermissionPreset

RESERVED_PRESET_IDS = frozenset({"ask", "never", "revoke", "show", "status", "sandbox", "list", "save"})

DEFAULT_MODES = {
    preset.id: preset for preset in (
        PermissionPreset("read-only", "Read-Only", True, True, True),
        PermissionPreset("workspace-edit", "Workspace-Edit", False, True, True),
        PermissionPreset("no-limit", "No-Limit", False, False, True),
    )
}


def parse_mode(identifier: str, value: dict) -> PermissionPreset:
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", identifier):
        raise ValueError("Permission preset IDs must be 1–64 letters, digits, underscores or hyphens")
    if identifier in RESERVED_PRESET_IDS:
        raise ValueError(f"Permission preset ID {identifier!r} is reserved by /permissions")
    if not isinstance(value, dict) or set(value) - {"name", "readOnly", "workspaceOnly", "networkAllowed"}:
        raise ValueError(f"Invalid permission preset {identifier!r}")
    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ValueError("Permission preset names must be 1–100 printable characters")
    fields = ("readOnly", "workspaceOnly", "networkAllowed")
    if any(type(value.get(key)) is not bool for key in fields):
        raise ValueError("Permission presets require boolean readOnly, workspaceOnly and networkAllowed")
    return PermissionMode(identifier, name.strip(), *(value[key] for key in fields))


def configured_modes(value: object) -> dict[str, PermissionPreset]:
    if not isinstance(value, dict):
        raise ValueError("permissions.presets must be an object keyed by stable preset ID")
    modes = dict(DEFAULT_MODES)
    for identifier, entry in value.items():
        inherited = modes[identifier].config() if identifier in modes else {}
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid permission preset {identifier!r}")
        modes[identifier] = parse_mode(identifier, {**inherited, **entry})
    return modes


def legacy_mode(value: str) -> PermissionPreset:
    """Explicit old sandbox options preserve their original offline semantics."""
    if value == "disabled":
        return DEFAULT_MODES["no-limit"]
    if value in {"read-only", "workspace-write"}:
        return PermissionMode("legacy-" + value, value + " (offline)", value == "read-only", True, False)
    raise ValueError("Legacy sandbox mode must be disabled, read-only or workspace-write")


@dataclass(frozen=True)
class PermissionSnapshot:
    """The exact permission decision captured for one run."""

    mode: PermissionPreset
    approval: str
    revision: int

    def flags(self) -> dict:
        return self.mode.flags()
