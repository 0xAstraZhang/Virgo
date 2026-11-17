"""Session-owned service registry.

The mapping remains available to plugins for compatibility, while Core-owned
services get named accessors so new code does not spell string keys.
"""
from __future__ import annotations

from typing import Any


class Services(dict):
    @property
    def shell(self) -> Any:
        return self["shell"]

    @property
    def sandbox(self) -> Any:
        return self["sandbox"]

    @property
    def permissions(self) -> Any:
        return self["permissions"]
