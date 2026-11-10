"""Settings: defaults ⊕ ~/.virgo/settings.json ⊕ <project>/.virgo/settings.json (deep merge, project wins)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "model": "anthropic/claude-opus-5-5",
    "smallModel": "anthropic/claude-haiku-5-5",
    "effort": "medium",
    "maxTokens": None,
    "providers": {},
    "permissions": {"mode": "default", "allow": [], "deny": [], "readOnlyCommands": []},
    "hooks": {},
    "features": {},
    "compaction": {"enabled": True, "reserveTokens": 16384, "keepRecentTokens": 20000, "triggerTokens": None},
    "retry": {"maxRetries": 3, "fallbackModel": None},
    "anthropic": {"fallbacks": True, "thinkingDisplay": "summarized", "thinkingBinding": True},
    "mcpServers": {},
    "memory": {"autoExtract": False},
    "subagents": {"maxTurns": 50},
    "goal": {"maxContinues": 8},
    "workflow": {"maxAgents": 200, "concurrency": 8},
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Paths:
    """Where Virgo keeps things. ``VIRGO_HOME`` overrides the user directory (default ~/.virgo)."""

    def __init__(self, cwd: Path, home: Path | None = None):
        self.cwd = Path(cwd).resolve()
        self.user = Path(home or os.environ.get("VIRGO_HOME") or Path.home() / ".virgo")
        self.project = self.cwd / ".virgo"

    def both(self, *parts: str) -> list[Path]:
        """User-level then project-level location of the same resource."""
        return [self.user.joinpath(*parts), self.project.joinpath(*parts)]

    @property
    def sessions(self) -> Path:
        slug = str(self.cwd).strip("/\\").replace("/", "-").replace("\\", "-").replace(":", "") or "root"
        return self.user / "sessions" / slug


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e


class Settings:
    def __init__(self, data: dict, paths: Paths):
        self.data = data
        self.paths = paths

    @classmethod
    def load(cls, paths: Paths, overrides: dict | None = None) -> Settings:
        data = DEFAULTS
        for f in paths.both("settings.json"):
            data = deep_merge(data, _read_json(f))
        if os.environ.get("VIRGO_MODEL"):
            data = deep_merge(data, {"model": os.environ["VIRGO_MODEL"]})
        data = deep_merge(data, overrides or {})
        return cls(data, paths)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur if cur is not None else default

    def set(self, dotted: str, value: Any) -> None:
        cur = self.data
        parts = dotted.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    def feature_enabled(self, name: str) -> bool:
        return bool(self.get(f"features.{name}", True))
