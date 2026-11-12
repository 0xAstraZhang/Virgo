"""Persist capability metadata only; never use this cache as a model availability list."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

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


def read_metadata(path: Path) -> dict:
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("models"), dict):
            return {}
        return {key: clean_metadata(value) for key, value in data["models"].items() if isinstance(key, str)}
    except (OSError, ValueError, RecursionError):
        return {}


def write_metadata(path: Path, models: dict) -> None:
    """Replace a single connection snapshot atomically, including successful empty lists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".metadata-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "models": {key: clean_metadata(value) for key, value in models.items()}}, stream)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
