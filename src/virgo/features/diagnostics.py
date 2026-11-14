"""Compatibility import for virgo.plugins.diagnostic_get; removed in 0.3."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("virgo.plugins.diagnostic_get")
