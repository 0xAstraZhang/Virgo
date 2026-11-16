"""Settings precedence with user-owned authorization and project-only restrictions."""

from __future__ import annotations

import copy
import fnmatch
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ..tools.sandbox import validate_config as validate_sandbox_config
from .execution_modes import configured_modes
from .permissions import normalize_approval_policy
from .work_modes import normalize_work_mode

PROFILES = ("minimal", "coding")
PLUGIN_IDS = (
    "diagnostic_get", "skills", "todo", "ask_user", "subagent", "background", "web", "mcp",
    "plan", "code_review",
)
REMOVED_FEATURES = frozenset({"schedule", "workflow", "team", "tasks", "memory", "goal"})
_FEATURE_ALIASES = {"diagnostics": "diagnostic_get"}

DEFAULTS: dict[str, Any] = {
    "language": "en",
    "plan": False,
    "sandbox": {},
    "model": "anthropic/claude-opus-5-5",
    "effort": "medium",
    "maxTokens": None,
    "providers": {},
    "permissions": {"preset": "workspace-edit", "presets": {}, "approval": "ask", "allow": [], "deny": []},
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
    permissions = out.get("permissions")
    if isinstance(permissions, dict):
        _canonicalize_permissions(permissions, notices)
    if "workMode" in out:
        legacy_mode = out.pop("workMode")
        plan = normalize_work_mode(legacy_mode) == "plan"
        if "plan" in out and type(out["plan"]) is not bool:
            raise ValueError("plan must be true or false")
        if "plan" in out and out["plan"] != plan:
            raise ValueError("workMode and plan disagree; remove workMode and use plan")
        out["plan"] = plan
        notices.append("workMode is deprecated; use plan: true or false.")
    if "permissions" in out:
        _validate_permissions(out["permissions"])
    if isinstance(out.get("plugins"), dict) and "auto_review" in out["plugins"]:
        raise ValueError("plugins.auto_review is retired; remove it and select permissions.approval=ask or never")
    legacy = out.pop("features", {})
    if not isinstance(legacy, dict):
        raise ValueError("features must be an object of boolean flags")
    translated: dict[str, dict] = {}
    for name, enabled in legacy.items():
        if name in {"permission", "auto_review"}:
            raise ValueError(f"features.{name} is retired; use permissions.approval=ask or never")
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


def _canonicalize_permissions(value: dict, notices: list[str]) -> None:
    """Translate the 0.2 ``mode``/``modes`` spelling before merging layers."""
    if "mode" in value:
        legacy_mode = value.pop("mode")
        if "preset" in value and value["preset"] != legacy_mode:
            raise ValueError("permissions.mode and permissions.preset disagree; remove the retired key")
        value["preset"] = legacy_mode
        notices.append("permissions.mode is deprecated; use permissions.preset.")
    if "modes" in value:
        legacy_modes = value.pop("modes")
        if "presets" in value:
            value["presets"] = deep_merge(legacy_modes, value["presets"])
        else:
            value["presets"] = legacy_modes
        notices.append("permissions.modes is deprecated; use permissions.presets.")


def _validate_permissions(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("permissions must be an object")
    if "readOnlyCommands" in value:
        raise ValueError("permissions.readOnlyCommands is retired; permission presets enforce Shell file and network boundaries")
    unknown = set(value) - {"preset", "presets", "approval", "allow", "deny"}
    if unknown:
        raise ValueError(f"Unknown permissions setting(s): {', '.join(sorted(unknown))}")
    if "approval" in value:
        normalize_approval_policy(value["approval"])
    configured_modes(value.get("presets", {}))
    selected = value.get("preset")
    if selected is not None and (not isinstance(selected, str) or not selected.strip()):
        raise ValueError("permissions.preset must be a permission preset ID")
    for name in ("allow", "deny"):
        rules = value.get(name, [])
        if not isinstance(rules, list) or any(not isinstance(rule, str) or not rule.strip() for rule in rules):
            raise ValueError(f"permissions.{name} must be a list of nonempty tool rules")
        for rule in rules:
            parsed = re.fullmatch(r"\s*([^()\s]+)\s*(?:\((.*)\))?\s*", rule)
            if parsed is None:
                raise ValueError(f"Invalid permissions.{name} rule: {rule!r}")
            tool, pattern = parsed.groups()
            if (name == "allow" and pattern is not None and any(char in pattern for char in "*?[")
                    and any(fnmatch.fnmatchcase(shell, tool) for shell in ("bash", "powershell"))):
                raise ValueError("Shell allow rules require an exact full command or a tool-only grant; wildcard command patterns are retired")


def _validate(data: dict) -> None:
    _validate_permissions(data.get("permissions", {}))
    modes = configured_modes(data.get("permissions", {}).get("presets", {}))
    if data.get("permissions", {}).get("preset", "workspace-edit") not in modes:
        raise ValueError("permissions.preset is unknown; the legacy key permissions.mode is retired. Choose a configured permission preset ID")
    if data.get("permissions", {}).get("mode") is not None or data.get("permissions", {}).get("modes") is not None:
        raise ValueError("permissions.mode and permissions.modes are retired; use permissions.preset and permissions.presets")
    validate_sandbox_config(data.get("sandbox", {}))
    if type(data.get("plan", False)) is not bool:
        raise ValueError("plan must be true or false")
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


def _canonical_key(dotted: str) -> str:
    """Read the 0.2 permission keys through the canonical spelling."""
    if dotted == "permissions.mode":
        return "permissions.preset"
    if dotted == "permissions.modes":
        return "permissions.presets"
    if dotted.startswith("permissions.modes."):
        return "permissions.presets." + dotted[len("permissions.modes."):]
    if dotted == "workMode":
        return "plan"
    return dotted


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
        if "sandbox" in project:
            project = {key: value for key, value in project.items() if key != "sandbox"}
            notices.append("Project sandbox settings are ignored; use user settings or an explicit startup override.")
        for layer in (project, overrides or {}):
            extension_config = layer.get("extensions", {})
            if isinstance(extension_config, dict) and "trustedProjects" in extension_config:
                notices.append("Project/runtime extensions.trustedProjects is ignored. Trust must come from user settings or --trust-project-extensions.")
        data = deep_merge(DEFAULTS, _normalize_layer(user, notices))
        project = _normalize_layer(project, notices)
        project_permissions = project.pop("permissions", {})
        if set(project_permissions) - {"deny"}:
            raise ValueError("Project permissions may only add deny rules; grants and approval policy belong in user settings or explicit startup overrides")
        # A project cannot erase user restrictions through ordinary deep merging.
        denied = list(dict.fromkeys([*data["permissions"]["deny"], *project_permissions.get("deny", [])]))
        # Workflow instructions do not confer or restrict execution authority.
        data = deep_merge(data, project)
        data["permissions"]["deny"] = denied
        if os.environ.get("VIRGO_MODEL"):
            data = deep_merge(data, {"model": os.environ["VIRGO_MODEL"]})
        normalized_overrides = _normalize_layer(overrides or {}, notices)
        data = deep_merge(data, normalized_overrides)
        if "preset" in normalized_overrides.get("permissions", {}) and "sandbox" not in normalized_overrides:
            # An explicit new preset override wins over an older user's sandbox setting.
            data["sandbox"] = {}
        _validate(data)
        settings = cls(data, paths)
        settings.overrides = normalized_overrides
        settings.migration_warnings = list(dict.fromkeys(notices))
        settings.project_extensions_trusted = paths.cwd in trusted
        return settings

    def get(self, dotted: str, default: Any = None) -> Any:
        if dotted == "workMode":
            return "plan" if self.get("plan") else "default"
        dotted = _canonical_key(dotted)
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur if cur is not None else default

    def set(self, dotted: str, value: Any) -> None:
        if dotted == "workMode":
            dotted, value = "plan", normalize_work_mode(value) == "plan"
        dotted = _canonical_key(dotted)
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
        if name in {"permission", "auto_review"}:
            raise ValueError("The legacy permission feature is retired; Core authorization is always enabled")
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
