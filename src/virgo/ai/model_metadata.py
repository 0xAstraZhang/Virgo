"""Persist validated provider directory snapshots for startup and offline pickers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

from .catalog import CatalogModel
from .models import base_url
from .types import Model


def cache_path(home: Path, model: Model) -> Path:
    # Endpoint identity is hashed so URLs containing credentials are never persisted.
    identity = json.dumps([model.provider, model.api, base_url(model).rstrip("/")])
    return home / "model-metadata" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")


def clean_metadata(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("context_window", "max_tokens"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            result[key] = number
    for key in ("reasoning", "vision"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    levels = value.get("supported_reasoning_levels")
    if isinstance(levels, (list, tuple)) and all(isinstance(x, str) and x for x in levels):
        result["supported_reasoning_levels"] = list(levels)
    default = value.get("default_reasoning_level")
    if isinstance(default, str) and default and (levels is None or default in result.get("supported_reasoning_levels", [])):
        result["default_reasoning_level"] = default
    return result


def read_catalog(path: Path) -> list[CatalogModel]:
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("models"), dict):
            return []
        dates = data.get("released_at", {})
        dates = dates if isinstance(dates, dict) else {}
        result = []
        for key, value in data["models"].items():
            if not isinstance(key, str) or not key or any(c.isspace() or ord(c) < 32 for c in key):
                continue
            date = dates.get(key)
            if not isinstance(date, (float, int)) or isinstance(date, bool) or not math.isfinite(date) or date < 0:
                date = None
            result.append(CatalogModel(key, clean_metadata(value), date))
        return result
    except (OSError, ValueError, RecursionError, OverflowError):
        return []


def write_metadata(path: Path, models: list[CatalogModel]) -> None:
    """Replace a single connection snapshot atomically, including successful empty lists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".metadata-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "models": {item.id: clean_metadata(item.metadata) for item in models},
                       "released_at": {item.id: item.released_at for item in models}}, stream)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
