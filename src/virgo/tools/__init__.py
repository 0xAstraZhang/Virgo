"""Built-in tools, loaded on first registry access."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._registry import BASE_TOOLS, BASH, EDIT, GLOB, GREP, READ, READ_ONLY_TOOLS, WRITE

__all__ = ["BASE_TOOLS", "BASH", "EDIT", "GLOB", "GREP", "READ", "READ_ONLY_TOOLS", "WRITE"]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import _registry

    return getattr(_registry, name)
