"""Versioned, owner-scoped tool registry for AgentSession."""

from __future__ import annotations

import threading

from ..agent.types import AgentTool

ToolGroupMap = dict[tuple[str, str], tuple[tuple[AgentTool, tuple], ...]]


class ToolRegistry:
    """Publishes atomic, owner-namespaced tool sets with monotonic version numbers."""

    def __init__(self, lock: threading.RLock):
        self._lock = lock
        self._groups: ToolGroupMap = {}
        self._version = 0

    def register(self, tool: AgentTool, roles: tuple = ("main",), *, owner: str = "host") -> None:
        """Register once; all replacements must use an explicitly owned namespace."""
        with self._lock:
            namespace = f"tool:{tool.name}"
            if (owner, namespace) in self._groups:
                raise ValueError(f"Tool {tool.name!r} is already registered by {owner}")
            self.replace(namespace, [(tool, roles)], owner=owner)

    def replace(self, namespace: str, tools, *, owner: str = "host") -> None:
        """Validate and publish one complete namespace without partial mutations."""
        if not owner or not namespace:
            raise ValueError("Tool registrations require an owner and namespace")
        entries = tuple((tool, tuple(roles)) for tool, roles in tools)
        if any(
            not isinstance(tool, AgentTool)
            or not tool.name
            or not roles
            or any(not isinstance(role, str) or not role for role in roles)
            for tool, roles in entries
        ):
            raise ValueError("Tool registrations require AgentTool objects and nonempty role names")
        key = (owner, namespace)
        with self._lock:
            occupied = {}
            for group, values in self._groups.items():
                if group == key:
                    continue
                for tool, roles in values:
                    for role in roles:
                        occupied[tool.name, role] = group[0]
            for tool, roles in entries:
                for role in roles:
                    if (tool.name, role) in occupied:
                        raise ValueError(
                            f"Tool {tool.name!r} for {role} is already registered by {occupied[tool.name, role]}"
                        )
                    occupied[tool.name, role] = owner
            groups = dict(self._groups)
            if entries:
                groups[key] = entries
            else:
                groups.pop(key, None)
            self._groups = groups
            self._version += 1

    def remove_owner(self, owner: str) -> None:
        with self._lock:
            self._groups = {key: values for key, values in self._groups.items() if key[0] != owner}
            self._version += 1

    def snapshot(self, role: str) -> tuple[int, list[AgentTool]]:
        with self._lock:
            return self._version, [
                tool for values in self._groups.values() for tool, roles in values if role in roles
            ]

    def tools_for(self, role: str) -> list[AgentTool]:
        return self.snapshot(role)[1]

    def tool(self, name: str, role: str = "main") -> AgentTool | None:
        return next((t for t in self.tools_for(role) if t.name == name), None)

    def checkpoint(self) -> ToolGroupMap:
        with self._lock:
            return dict(self._groups)

    def restore(self, checkpoint: ToolGroupMap) -> None:
        with self._lock:
            self._groups = dict(checkpoint)
            self._version += 1
