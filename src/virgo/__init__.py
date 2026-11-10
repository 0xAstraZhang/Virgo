"""Virgo: a lightweight, extensible coding agent harness inspired by Pi."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("virgo")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
