"""Settings precedence with user-owned authorization and project-only restrictions."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .execution_modes import DEFAULT_MODES, migrate_preset
from .permissions import normalize_approval_policy

PLUGIN_IDS = (
    "diagnostic_get", "skills", "ask_user", "subagent", "background", "web",
)
REMOVED_FEATURES = frozenset({
    "schedule", "workflow", "team", "tasks", "memory", "goal", "plan", "code_review", "workMode", "profile",
})
RETIRED_PLUGINS = frozenset({"plan", "code_review"})
EXTENSION_ONLY = frozenset({"mcp", "todo"})
_FEATURE_ALIASES = {"diagnostics": "diagnostic_get"}

DEFAULTS: dict[str, Any] = {
    "language": "en",
    "execution": {"mode": "no-limit"},
    "sandbox": {},
    "model": "anthropic/claude-opus-5-5",
    "effort": "medium",
    "maxTokens": None,
    "providers": {},
    "permissions": {"approval": "ask"},
    "hooks": {},
    "shell": {"executable": "auto"},
    "plugins": {},
    "budget": {"maxModelCalls": None},
    "compaction": {"enabled": True, "triggerRatio": 0.8, "keepRecentTokens": 20000, "triggerTokens": None},
    "retry": {"maxRetries": 3, "fallbackModel": None, "maxWaitSeconds": 120},
    "anthropic": {"fallbacks": True, "thinkingDisplay": "summarized", "thinkingBinding": True},
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


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
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
        if name in EXTENSION_ONLY:
            notices.append(f"features.{name} is no longer built in; install the {name} extension instead.")
            continue
        if name in REMOVED_FEATURES:
            notices.append(f"features.{name} is retired and ignored; existing data is preserved.")
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
    for name in sorted(RETIRED_PLUGINS.intersection(out["plugins"])):
        out["plugins"].pop(name)
        notices.append(f"plugins.{name} is retired and ignored; existing data is preserved.")
    for name in sorted(EXTENSION_ONLY.intersection(out["plugins"])):
        out["plugins"].pop(name)
        notices.append(f"plugins.{name} is no longer built in; install the {name} extension instead.")
    for name in sorted(REMOVED_FEATURES.intersection(out)):
        out.pop(name)
        notices.append(f"The retired {name} configuration is ignored; existing data is preserved.")
    return out


def _canonicalize_permissions(value: dict, notices: list[str]) -> None:
    """Translate the 0.2 ``mode``/``modes`` spelling before merging layers."""
    if "mode" in value:
        legacy_mode = value.pop("mode")
        if "preset" in value and value["preset"] != legacy_mode:
            raise ValueError("permissions.mode and permissions.preset disagree; remove the retired key")
        value["preset"] = legacy_mode
        notices.append("permissions.mode is retired; select a fixed execution.mode.")
    if "modes" in value:
        legacy_modes = value.pop("modes")
        if not isinstance(legacy_modes, dict):
            raise ValueError("permissions.modes must be an object keyed by stable preset ID")
        if "presets" in value and not isinstance(value["presets"], dict):
            raise ValueError("permissions.presets must be an object keyed by stable preset ID")
        if "presets" in value:
            value["presets"] = deep_merge(legacy_modes, value["presets"])
        else:
            value["presets"] = legacy_modes
        notices.append("permissions.modes is retired; select a fixed execution.mode.")


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
    if not isinstance(value.get("presets", {}), dict):
        raise ValueError("permissions.presets must be an object")
    selected = value.get("preset")
    if selected is not None and (not isinstance(selected, str) or not selected.strip()):
        raise ValueError("permissions.preset must be a permission preset ID")
    for name in ("allow", "deny"):
        if value.get(name):
            raise ValueError(f"permissions.{name} is retired; migrate task intent to instructions and authority to resource paths")
        if name in value and value[name] != []:
            raise ValueError(f"permissions.{name} is retired; remove the key")


def _validate(data: dict) -> None:
    _validate_permissions(data.get("permissions", {}))
    execution = data.get("execution")
    if (not isinstance(execution, dict) or set(execution) != {"mode"}
            or not isinstance(execution["mode"], str) or execution["mode"] not in DEFAULT_MODES):
        raise ValueError("execution.mode must be read-only, workspace-edit or no-limit")
    if data.get("sandbox"):
        raise ValueError("Legacy sandbox settings are retired; migrate to execution.mode")
    if set(data.get("permissions", {})) - {"approval", "allow", "deny"}:
        raise ValueError("Custom permission presets are retired; choose execution.mode")
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


def _same_directory(left: Path, right: Path) -> bool:
    """Report whether two configuration directories are the same place.

    An unresolvable path cannot be proven shared, so it keeps the deny-only guard.
    """
    try:
        return left.resolve() == right.resolve()
    except (OSError, RuntimeError):
        return False


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


def _migrate_execution(data: dict, explicit: dict, notices: list[str], *, existing_user: bool) -> None:
    permissions = data["permissions"]
    selected = permissions.pop("preset", None)
    presets = permissions.pop("presets", {})
    execution = data["execution"]
    if not isinstance(execution, dict):
        raise ValueError("execution must be an object")
    provider = execution.pop("provider", None)
    if provider not in (None, "host", "sandbox"):
        raise ValueError("execution.provider must be host or sandbox")
    sandbox = data.pop("sandbox", {})
    if not isinstance(sandbox, dict) or set(sandbox) - {"mode"}:
        raise ValueError("Legacy sandbox configuration is retired; migrate to execution.mode")
    legacy_mode = sandbox.get("mode")
    if sandbox and legacy_mode not in ("read-only", "workspace-write", "disabled"):
        raise ValueError("Unknown retired sandbox.mode; choose execution.mode")
    if "mode" in explicit.get("execution", {}):
        if selected is not None or presets or legacy_mode or provider:
            notices.append("Explicit execution.mode overrides the legacy permission selection.")
    elif legacy_mode == "disabled" and provider != "sandbox":
        execution["mode"] = "no-limit"
        notices.append("Legacy sandbox.mode=disabled migrated to execution.mode=no-limit.")
    elif legacy_mode:
        raise ValueError("Cannot migrate the legacy sandbox mode with its original permissions; "
                         "choose execution.mode explicitly.")
    elif selected is not None or "presets" in explicit.get("permissions", {}) or provider == "sandbox":
        if provider == "host":
            raise ValueError("Legacy host and sandbox settings disagree; select execution.mode explicitly")
        # A saved preset collection without a selector used main's workspace-edit default.
        execution["mode"] = migrate_preset(selected or "workspace-edit", presets)
        notices.append(f"Legacy permission selection migrated to execution.mode={execution['mode']}.")
    elif provider == "host":
        execution["mode"] = "no-limit"
    elif existing_user:
        execution["mode"] = "workspace-edit"
        notices.append("Existing user settings retain the previous workspace-edit default. Use execution.mode to select a different mode.")
    if presets:
        notices.append("Legacy custom presets are no longer selectable. Their user settings are preserved on disk.")


class Settings:
    def __init__(self, data: dict, paths: Paths):
        self.data = data
        self.paths = paths
        self.overrides: dict = {}
        self.sources: dict[str, str] = {}
        self.migration_warnings: list[str] = []
        self.project_extensions_trusted = False

    @staticmethod
    def startup_language(paths: Paths) -> str:
        """Read the CLI language before startup arguments can supply execution settings."""
        for path in reversed(paths.both("settings.json")):
            layer = _read_json(path) or {}
            if "language" in layer:
                return DEFAULTS["language"] if layer["language"] is None else layer["language"]
        return DEFAULTS["language"]

    @classmethod
    def load(cls, paths: Paths, overrides: dict | None = None) -> Settings:
        notices: list[str] = []
        saved_user = _read_json(paths.user / "settings.json")
        user = saved_user or {}
        # When <cwd>/.virgo is the user settings directory, reading it again as an
        # untrusted project layer would reject the user's own grants. The user layer
        # already carries those settings.
        shared_settings = _same_directory(paths.project, paths.user)
        project = {} if shared_settings else (_read_json(paths.project / "settings.json") or {})
        if shared_settings and saved_user is not None:
            notices.append("The working directory resolves to the Virgo state directory; user settings are not re-read as project settings.")
        trusted = _trusted_projects(user)
        for key in ("sandbox", "execution", "permissions"):
            if project.get(key):
                raise ValueError(f"Project {key} configuration is retired; execution authority belongs in user settings or startup overrides")
            project.pop(key, None)
        for layer in (project, overrides or {}):
            extension_config = layer.get("extensions", {})
            if isinstance(extension_config, dict) and "trustedProjects" in extension_config:
                notices.append("Project/runtime extensions.trustedProjects is ignored. Trust must come from user settings or --trust-project-extensions.")
        user = _normalize_layer(user, notices)
        data = deep_merge(DEFAULTS, user)
        project = _normalize_layer(project, notices)
        data = deep_merge(data, project)
        if os.environ.get("VIRGO_MODEL"):
            data = deep_merge(data, {"model": os.environ["VIRGO_MODEL"]})
        normalized_overrides = _normalize_layer(overrides or {}, notices)
        # A legacy provider override still has startup precedence over a saved
        # modern mode. Preserve a saved sandbox mode when it already matches.
        startup_execution = normalized_overrides.get("execution", {})
        saved_execution = user.get("execution", {})
        if (isinstance(startup_execution, dict) and "mode" not in startup_execution
                and isinstance(saved_execution, dict) and isinstance(saved_execution.get("mode"), str)
                and saved_execution["mode"] in DEFAULT_MODES):
            if startup_execution.get("provider") == "sandbox":
                saved_mode = saved_execution["mode"]
                startup_execution["mode"] = "workspace-edit" if saved_mode == "no-limit" else saved_mode
            elif startup_execution.get("provider") == "host":
                startup_execution["mode"] = "no-limit"
        data = deep_merge(data, normalized_overrides)
        explicit = deep_merge(user, normalized_overrides)
        _migrate_execution(data, explicit, notices, existing_user=saved_user is not None)
        _validate(data)
        settings = cls(data, paths)
        def record_sources(layer, source, prefix=""):
            for key, value in layer.items():
                dotted = f"{prefix}.{key}" if prefix else key
                settings.sources[dotted] = source
                if isinstance(value, dict):
                    record_sources(value, source, dotted)
        record_sources(DEFAULTS, "default")
        record_sources(user, "user")
        record_sources(project, "project")
        record_sources(normalized_overrides, "startup")
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
        parts = dotted.split(".")
        checked = parts[0] in {"permissions", "execution", "sandbox"}
        data = copy.deepcopy(self.data) if checked else self.data
        cur = data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
        if checked:
            _validate(data)
        self.data = data

    def plugin_enabled(self, name: str) -> bool:
        if name not in PLUGIN_IDS:
            raise ValueError(f"Unknown plugin {name!r}")
        explicit = self.get(f"plugins.{name}.enabled")
        enabled = explicit if explicit is not None else True
        if name == "web":
            enabled = enabled and (self.get("plugins.web.searchEnabled", True) or self.get("plugins.web.fetchEnabled", True))
        return bool(enabled)

    def feature_enabled(self, name: str) -> bool:
        """Compatibility read for extensions migrating from the old feature flags."""
        if name in EXTENSION_ONLY:
            return False
        if name in {"permission", "auto_review"}:
            raise ValueError("The legacy permission feature is retired; use execution.mode")
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
        saved = _read_json(path)
        if saved is None:
            # Mark a fresh configuration without persisting the current CLI override.
            saved = {"execution": {"mode": DEFAULTS["execution"]["mode"]}}
        data = deep_merge(saved, patch)
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
        project = _read_json(self.paths.project / "settings.json") or {}
        effective = deep_merge(config, project.get("providers", {}).get(provider, {}))
        effective = deep_merge(effective, self.overrides.get("providers", {}).get(provider, {}))
        for key in ("api", "baseUrl", "apiKeyEnv"):
            if effective.get(key) != config.get(key):
                raise ValueError(
                    f"Project or runtime settings override {provider}.{key}. "
                    "Use that configured value, update the override, or choose a custom Provider ID."
                )
