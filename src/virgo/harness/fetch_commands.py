"""Compatibility import for virgo.plugins.fetch_commands; removed in 0.3."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("virgo.plugins.fetch_commands")
