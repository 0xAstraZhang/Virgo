"""Compatibility import for virgo.plugins.web_access; removed in 0.3."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("virgo.plugins.web_access")
