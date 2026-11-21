"""OS-native Shell sandbox backends (Linux bubblewrap, macOS Seatbelt, Windows AppContainer).

This package stays free of ``virgo.harness`` imports so execution backends remain
strictly lower-layer infrastructure.
"""
from __future__ import annotations

from ..execution_policy import default_deny_rules as _default_deny_rules

__all__ = ["_default_deny_rules"]
