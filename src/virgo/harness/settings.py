"""Settings: defaults ⊕ ~/.virgo/settings.json ⊕ <project>/.virgo/settings.json (deep merge, project wins)."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

PROFILES = ("minimal", "coding")
PLUGIN_IDS = (
    "auto_review", "diagnostic_get", "skills", "todo", "ask_user", "subagent", "background", "web", "mcp",
)
REMOVED_FEATURES = frozenset({"schedule", "workflow", "team", "tasks", "memory", "goal"})
_FEATURE_ALIASES = {"diagnostics": "diagnostic_get", "permission": "auto_review"}

DEFAULTS: dict[str, Any] = {
    "language": "en",
    "model": "anthropic/claude-opus-5-5",
    "effort": "medium",
    "maxTokens": None,
    "providers": {},
    "permissions": {"mode": "auto-review", "allow": [], "deny": [], "readOnlyCommands": []},
    "hooks": {},
    "shell": {"executable": "auto"},
    "profile": "coding",
    "plugins": {},
    "budget": {"maxModelCalls": None},
    "compaction": {"enabled": True, "triggerRatio": 0.8, "keepRecentTokens": 20000, "triggerTokens": None},
    "retry": {"maxRetries": 3, "fallbackModel": None, "maxWaitSeconds": 120},
    "anthropic": {"fallbacks": True, "thinkingDisplay": "summarized", "thinkingBinding": True},
    "mcpServers": {},
    "fetch": {"mode": "auto", "backend": "http", "timeout": 20, "maxBytes": 2097152, "maxUses": None, "maxContentTokens": 8000},
    "search": {"mode": "auto", "backend": "duckduckgo", "timeout": 15, "maxUses": None},
    "subagents": {"maxTurns": 50},
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
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"settings in {path} must be a JSON object")
    return data


def _normalize_layer(layer: dict, notices: list[str]) -> dict:
    """Translate one configuration source before merging precedence layers.

    A project-level legacy flag must still override a user-level plugin flag;
    explicit new keys win over legacy keys in the same source.
    """
    if not isinstance(layer, dict):
        raise ValueError("settings must be a JSON object")
    out = copy.deepcopy(layer)
    legacy = out.pop("features", {})
    if not isinstance(legacy, dict):
        raise ValueError("features must be an object of boolean flags")
    translated: dict[str, dict] = {}
    for name, enabled in legacy.items():
        if not isinstance(enabled, bool):
            raise ValueError(f"features.{name} must be a boolean")
        if name in REMOVED_FEATURES:
            notices.append(f"features.{name} is retired and ignored; existing data is preserved. See the Core/plugins migration guide.")
            continue
        if name in {"search", "fetch"}:
            web = translated.setdefault("web", {})
            web[f"{name}Enabled"] = enabled
            if enabled:
                web["enabled"] = True
            notices.append(f"features.{name} is deprecated; use plugins.web.{name}Enabled instead.")
            continue
        canonical = _FEATURE_ALIASES.get(name, name)
        if canonical not in PLUGIN_IDS:
            raise ValueError(f"Unknown legacy feature {name!r}; known plugins: {', '.join(PLUGIN_IDS)}")
        translated.setdefault(canonical, {})["enabled"] = enabled
        notices.append(f"features.{name} is deprecated; use plugins.{canonical}.enabled instead.")
        if name == "permission" and not enabled:
            notices.append("Core permission checks cannot be disabled by features.permission. Only automatic review is disabled; permissions.mode=accept-all remains an explicit opt-in.")
    web = translated.get("web", {})
    if web.get("searchEnabled") is False and web.get("fetchEnabled") is False:
        web["enabled"] = False
    plugins = out.get("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("plugins must be an object keyed by plugin ID")
    out["plugins"] = deep_merge(translated, plugins)
    for name in sorted(REMOVED_FEATURES.intersection(out)):
        notices.append(f"The retired {name} configuration is ignored; existing data is preserved. See the Core/plugins migration guide.")
    return out


def _validate(data: dict) -> None:
    profile = data.get("profile", "coding")
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("plugins must be an object keyed by plugin ID")
    for name, config in plugins.items():
        if name not in PLUGIN_IDS:
            raise ValueError(f"Unknown plugin {name!r}; known plugins: {', '.join(PLUGIN_IDS)}")
        if not isinstance(config, dict):
            raise ValueError(f"plugins.{name} must be an object")
        boolean_keys = ("enabled", "searchEnabled", "fetchEnabled") if name == "web" else ("enabled",)
        for key in boolean_keys:
            if key in config and not isinstance(config[key], bool):
                raise ValueError(f"plugins.{name}.{key} must be a boolean")
    budget = data.get("budget", {})
    if not isinstance(budget, dict):
        raise ValueError("budget must be an object")
    calls = budget.get("maxModelCalls")
    if calls is not None and (type(calls) is not int or calls < 1):
        raise ValueError("budget.maxModelCalls must be null or a positive integer")


def _trusted_projects(user: dict) -> set[Path]:
    extensions = user.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ValueError("extensions must be an object")
    paths = extensions.get("trustedProjects", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("extensions.trustedProjects must be a list of absolute project paths in user settings")
    resolved = set()
    for path in paths:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("extensions.trustedProjects entries must be absolute project paths")
        resolved.add(candidate.resolve())
    return resolved


class Settings:
    def __init__(self, data: dict, paths: Paths):
        self.data = data
        self.paths = paths
        self.overrides: dict = {}
        self.migration_warnings: list[str] = []
        self.project_extensions_trusted = False

    @classmethod
    def load(cls, paths: Paths, overrides: dict | None = None) -> Settings:
        notices: list[str] = []
        user = _read_json(paths.user / "settings.json")
        project = _read_json(paths.project / "settings.json")
        trusted = _trusted_projects(user)
        for layer in (project, overrides or {}):
            extension_config = layer.get("extensions", {})
            if isinstance(extension_config, dict) and "trustedProjects" in extension_config:
                notices.append("Project/runtime extensions.trustedProjects is ignored. Trust must come from user settings or --trust-project-extensions.")
        data = copy.deepcopy(DEFAULTS)
        for layer in (user, project):
            data = deep_merge(data, _normalize_layer(layer, notices))
        if os.environ.get("VIRGO_MODEL"):
            data = deep_merge(data, {"model": os.environ["VIRGO_MODEL"]})
        normalized_overrides = _normalize_layer(overrides or {}, notices)
        data = deep_merge(data, normalized_overrides)
        _validate(data)
        settings = cls(data, paths)
        settings.overrides = normalized_overrides
        settings.migration_warnings = list(dict.fromkeys(notices))
        settings.project_extensions_trusted = paths.cwd in trusted
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

    def plugin_enabled(self, name: str) -> bool:
        if name not in PLUGIN_IDS:
            raise ValueError(f"Unknown plugin {name!r}")
        explicit = self.get(f"plugins.{name}.enabled")
        enabled = explicit if explicit is not None else (
            self.get("profile", "coding") == "coding" and (name != "mcp" or self._mcp_configured())
        )
        if name == "web":
            enabled = enabled and (self.get("plugins.web.searchEnabled", True) or self.get("plugins.web.fetchEnabled", True))
        return bool(enabled)

    def _mcp_configured(self) -> bool:
        """Discover legacy configuration only after the preset/explicit flag gate.

        Empty configuration does not expose MCP management tools. Malformed
        configuration enables its loader so the existing structured diagnostic
        explains the error; it must not disappear silently during migration.
        """
        configured = self.get("mcpServers", {})
        if configured or not isinstance(configured, dict):
            return True
        for path in self.paths.both("mcp.json"):
            try:
                data = _read_json(path)
            except (OSError, ValueError):
                return True
            servers = data.get("mcpServers", {})
            if servers or not isinstance(servers, dict):
                return True
        return False

    def feature_enabled(self, name: str) -> bool:
        """Compatibility read for extensions migrating from the old feature flags."""
        if name == "permission":
            return True
        if name in REMOVED_FEATURES:
            return False
        if name in {"search", "fetch"}:
            return self.plugin_enabled("web") and bool(self.get(f"plugins.web.{name}Enabled", True))
        return self.plugin_enabled(_FEATURE_ALIASES.get(name, name))

    def save_user(self, patch: dict) -> None:
        """Persist only explicit changes, preserving unrelated user configuration."""
        notices: list[str] = []
        normalized = _normalize_layer(patch, notices)
        candidate = deep_merge(self.data, normalized)
        _validate(candidate)
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
        self.data = candidate
        self.migration_warnings = list(dict.fromkeys([*self.migration_warnings, *notices]))

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
