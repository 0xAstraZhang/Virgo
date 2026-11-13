"""Settings: defaults ⊕ ~/.virgo/settings.json ⊕ <project>/.virgo/settings.json (deep merge, project wins)."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "model": "anthropic/claude-opus-5-5",
    "effort": "medium",
    "maxTokens": None,
    "providers": {},
    "permissions": {"mode": "auto-review", "allow": [], "deny": [], "readOnlyCommands": []},
    "hooks": {},
    "shell": {"executable": "auto"},
    "features": {},
    "compaction": {"enabled": True, "triggerRatio": 0.8, "keepRecentTokens": 20000, "triggerTokens": None},
    "retry": {"maxRetries": 3, "fallbackModel": None, "maxWaitSeconds": 120},
    "anthropic": {"fallbacks": True, "thinkingDisplay": "summarized", "thinkingBinding": True},
    "mcpServers": {},
    "fetch": {"mode": "auto", "backend": "http", "timeout": 20, "maxBytes": 2097152, "maxUses": 3, "maxContentTokens": 8000},
    "search": {"mode": "auto", "backend": "duckduckgo", "timeout": 15, "maxUses": 3},
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
        self.overrides: dict = {}

    @classmethod
    def load(cls, paths: Paths, overrides: dict | None = None) -> Settings:
        data = DEFAULTS
        for f in paths.both("settings.json"):
            data = deep_merge(data, _read_json(f))
        if os.environ.get("VIRGO_MODEL"):
            data = deep_merge(data, {"model": os.environ["VIRGO_MODEL"]})
        data = deep_merge(data, overrides or {})
        settings = cls(data, paths)
        settings.overrides = copy.deepcopy(overrides or {})
        return settings

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

    def save_user(self, patch: dict) -> None:
        """Persist only explicit changes, preserving unrelated user configuration."""
        path = self.paths.user / "settings.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = deep_merge(_read_json(path), patch)
        fd, name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
        self.data = deep_merge(self.data, patch)

    def check_provider_update(self, provider: str, config: dict) -> None:
        """Reject a login that would use a different endpoint on the next startup."""
        project = _read_json(self.paths.project / "settings.json")
        effective = deep_merge(config, project.get("providers", {}).get(provider, {}))
        effective = deep_merge(effective, self.overrides.get("providers", {}).get(provider, {}))
        for key in ("api", "baseUrl", "apiKeyEnv"):
            if effective.get(key) != config.get(key):
                raise ValueError(
                    f"Project or runtime settings override {provider}.{key}. "
                    "Use that configured value, update the override, or choose a custom Provider ID."
                )
