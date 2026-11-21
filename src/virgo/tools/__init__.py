"""Built-in file tools, loaded on first access."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .fs import EDIT, GLOB, GREP, READ, WRITE

__all__ = ["EDIT", "GLOB", "GREP", "READ", "WRITE"]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import fs

    return getattr(fs, name)
