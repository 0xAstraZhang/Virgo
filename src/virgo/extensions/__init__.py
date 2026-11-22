"""Built-in Python extensions loaded through ``setup(api)``.

Built-in extensions participate in the normal extension pipeline: they can be
disabled individually via ``extensions.disabled`` (or altogether via
``--no-extensions``) and overridden by a same-stem ``*.py`` file in
``~/.virgo/extensions/`` or a trusted project's ``.virgo/extensions/``.
"""

from __future__ import annotations

BUILTIN_EXTENSIONS: tuple[str, ...] = ("mcp", "todo")

__all__ = ["BUILTIN_EXTENSIONS"]
