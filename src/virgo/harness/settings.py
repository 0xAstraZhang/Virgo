"""Settings precedence with user-owned authorization and untrusted-project stripping."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..execution import default_execution_mode
from ..execution_policy import DEFAULT_MODES, normalize_approval_policy
from ..file_access import same_directory

PLUGIN_IDS = (
    "diagnostic_get", "skills", "ask_user", "subagent", "background", "web",
)
BUILTIN_EXTENSIONS: tuple[str, ...] = ("mcp", "todo")
REMOVED_FEATURES = frozenset({
    "schedule", "workflow", "team", "tasks", "memory", "goal", "plan", "code_review", "workMode", "profile",
})
RETIRED_PLUGINS = frozenset({"plan", "code_review"})
UNTRUSTED_PROJECT_KEYS = ("hooks", "mcpServers", "providers", "extensions", "shell", "search", "fetch")

DEFAULTS: dict[str, Any] = {
    "language": "en",
    "execution": {"mode": "workspace-edit"},
    "model": "anthropic/claude-opus-5-5",
    "effort": None,
    "maxTokens": None,
    "providers": {},
    "permissions": {"approval": "ask", "confirmHighRisk": False},
    "hooks": {},
    "shell": {"executable": "auto"},
    "plugins": {},
    "budget": {"maxModelCalls": None},
    "compaction": {"enabled": True, "pruneRatio": 0.6, "triggerRatio": 0.8, "keepRecentTokens": 20000, "triggerTokens": None},
    "retry": {"maxRetries": 3, "fallbackModel": None, "maxWaitSeconds": 120},
    "anthropic": {"fallbacks": True, "thinkingDisplay": "summarized", "thinkingBinding": True},
    "fetch": {"mode": "auto", "backend": "http", "timeout": 20, "maxBytes": 2097152, "maxUses": None, "maxContentTokens": 8000},
    "search": {"mode": "auto", "backend": "duckduckgo", "timeout": 15, "maxUses": None},
    "subagents": {"maxTurns": 20, "maxTotalTurns": 40, "effort": None},
    "tui": {"sessionGrouping": "workspace"},
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class UserSettingsPatch:
    """User-layer writes and removals to apply in one settings file replacement."""

    values: dict
    remove_provider_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _remove_provider_keys(data: dict, provider: str, keys: tuple[str, ...], *, prune_root: bool = True) -> None:
    providers = data.get("providers")
    config = providers.get(provider) if isinstance(providers, dict) else None
    if not isinstance(config, dict) or config.keys().isdisjoint(keys):
        return
    for key in keys:
        config.pop(key, None)
    if not config:
        del providers[provider]
        if prune_root and not providers:
            del data["providers"]


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


def _reject_retired_execution(layer: dict) -> None:
    """Fail loudly on retired execution/sandbox/permission keys (no silent migration)."""
    if "sandbox" in layer:
        raise ValueError(
            "Legacy 'sandbox' settings are retired. Configure execution.mode "
            "('workspace-edit', 'read-only', or 'no-limit') instead."
        )
    execution = layer.get("execution")
    if execution is not None:
        if not isinstance(execution, dict):
            raise ValueError("execution must be an object with 'mode' ('workspace-edit', 'read-only', or 'no-limit')")
        unknown = set(execution) - {"mode"}
        if unknown:
            raise ValueError(
                f"Unsupported execution setting(s): {', '.join(sorted(unknown))}. "
                "Use execution.mode ('workspace-edit', 'read-only', or 'no-limit') instead."
            )
    permissions = layer.get("permissions")
    if permissions is not None:
        _validate_permissions(permissions)


def _normalize_layer(layer: dict, notices: list[str]) -> dict:
    """Validate and normalize non-execution settings for one configuration source."""
    if not isinstance(layer, dict):
        raise ValueError("settings must be a JSON object")
    out = copy.deepcopy(layer)
    _reject_retired_execution(out)
    if isinstance(out.get("plugins"), dict) and "auto_review" in out["plugins"]:
        raise ValueError("plugins.auto_review is retired; remove it and select permissions.approval=ask or never")
    if "features" in out:
        raise ValueError(
            "Legacy 'features' settings are retired. Configure 'plugins.<id>.enabled' "
            "(or 'plugins.web.searchEnabled'/'fetchEnabled') instead."
        )
    if "plugins" in out:
        plugins = out["plugins"]
        if not isinstance(plugins, dict):
            raise ValueError("plugins must be an object keyed by plugin ID")
        for name in sorted(RETIRED_PLUGINS.intersection(plugins)):
            plugins.pop(name)
            notices.append(f"plugins.{name} is retired and ignored; existing data is preserved.")
    for name in sorted(REMOVED_FEATURES.intersection(out)):
        out.pop(name)
        notices.append(f"The retired {name} configuration is ignored; existing data is preserved.")
    return out


def _validate_permissions(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("permissions must be an object")
    retired = {"mode", "modes", "preset", "presets", "readOnlyCommands", "allow", "deny"}
    hit = sorted(retired.intersection(value))
    if hit:
        names = ", ".join(f"permissions.{key}" for key in hit)
        verb = "is" if len(hit) == 1 else "are"
        raise ValueError(
            f"Retired permissions setting(s): {names} {verb} retired. "
            "Configure execution.mode ('workspace-edit', 'read-only', or 'no-limit') and "
            "permissions.approval ('ask' or 'never') instead."
        )
    unknown = set(value) - {"approval", "confirmHighRisk"}
    if unknown:
        raise ValueError(f"Unknown permissions setting(s): {', '.join(sorted(unknown))}")
    if "approval" in value:
        normalize_approval_policy(value["approval"])
    if "confirmHighRisk" in value and not isinstance(value["confirmHighRisk"], bool):
        raise ValueError("permissions.confirmHighRisk must be a boolean")


def _validate_extensions(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("extensions must be an object")
    unknown = set(value) - {"trustedProjects", "disabled"}
    if unknown:
        raise ValueError(f"Unknown extensions setting(s): {', '.join(sorted(unknown))}")
    if "disabled" in value:
        disabled = value["disabled"]
        if not isinstance(disabled, list) or any(
            not isinstance(item, str)
            or "/" in item
            or "\\" in item
            or not item.strip().removesuffix(".py")
            for item in disabled
        ):
            raise ValueError("extensions.disabled must be a list of nonempty extension names")


def _validate(data: dict) -> None:
    _reject_retired_execution(data)
    execution = data.get("execution")
    if (not isinstance(execution, dict) or set(execution) != {"mode"}
            or not isinstance(execution["mode"], str) or execution["mode"] not in DEFAULT_MODES):
        raise ValueError("execution.mode must be read-only, workspace-edit or no-limit")
    if "extensions" in data:
        _validate_extensions(data["extensions"])
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
    subagents = data["subagents"]
    if not isinstance(subagents, dict):
        raise ValueError("subagents must be an object")
    total_turns = subagents.get("maxTotalTurns")
    if type(total_turns) is not int or total_turns < 0:
        raise ValueError("subagents.maxTotalTurns must be a nonnegative integer")
    effort = subagents.get("effort")
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise ValueError("subagents.effort must be null or a reasoning level")
    tui = data.get("tui", {})
    if not isinstance(tui, dict):
        raise ValueError("tui must be an object")
    grouping = tui.get("sessionGrouping")
    if not isinstance(grouping, str) or grouping not in {"workspace", "none"}:
        raise ValueError("tui.sessionGrouping must be 'workspace' or 'none'")


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
    def load(cls, paths: Paths, overrides: dict | None = None, *,
             trust_project_extensions: bool = False) -> Settings:
        notices: list[str] = []
        user = _read_json(paths.user / "settings.json") or {}
        shared_settings = same_directory(paths.project, paths.user)
        project = {} if shared_settings else (_read_json(paths.project / "settings.json") or {})
        trusted = _trusted_projects(user)
        project_trusted = bool(trust_project_extensions or (paths.cwd in trusted))
        for key in ("sandbox", "execution", "permissions"):
            if project.get(key):
                raise ValueError(f"Project {key} configuration is not permitted; execution authority belongs in user settings or startup overrides")
            project.pop(key, None)
        # R5: Strip untrusted project hooks, mcpServers, providers, extensions,
        # shell, search, and fetch BEFORE merging onto user settings so an
        # untrusted repo cannot wipe user config, hijack shell.executable or
        # providers.baseUrl, exfiltrate env vars via apiKeyEnv, or override web caps.
        if project and not project_trusted:
            stripped = [key for key in UNTRUSTED_PROJECT_KEYS if key in project]
            for key in stripped:
                project.pop(key, None)
            if stripped:
                notices.append(
                    f"Ignored untrusted project settings ({', '.join(stripped)}); "
                    "add this directory to extensions.trustedProjects or pass --trust-project-extensions to enable them."
                )
        for layer in (project, overrides or {}):
            extension_config = layer.get("extensions", {})
            if isinstance(extension_config, dict) and "trustedProjects" in extension_config:
                extension_config.pop("trustedProjects", None)
                notices.append("Project/runtime extensions.trustedProjects is ignored. Trust must come from user settings or --trust-project-extensions.")
        user = _normalize_layer(user, notices)
        project = _normalize_layer(project, notices)
        normalized_overrides = _normalize_layer(overrides or {}, notices)
        explicit_mode = (
            (isinstance(user.get("execution"), dict) and "mode" in user["execution"])
            or (
                isinstance(normalized_overrides.get("execution"), dict)
                and "mode" in normalized_overrides["execution"]
            )
        )
        default_mode = DEFAULTS["execution"]["mode"] if explicit_mode else default_execution_mode()
        defaults = deep_merge(DEFAULTS, {"execution": {"mode": default_mode}})
        data = deep_merge(defaults, user)
        data = deep_merge(data, project)
        if os.environ.get("VIRGO_MODEL"):
            data = deep_merge(data, {"model": os.environ["VIRGO_MODEL"]})
        data = deep_merge(data, normalized_overrides)
        _validate(data)
        settings = cls(data, paths)

        def record_sources(layer, source, prefix=""):
            for key, value in layer.items():
                dotted = f"{prefix}.{key}" if prefix else key
                settings.sources[dotted] = source
                if isinstance(value, dict):
                    record_sources(value, source, dotted)

        record_sources(defaults, "default")
        record_sources(user, "user")
        record_sources(project, "project")
        record_sources(normalized_overrides, "startup")
        settings.overrides = normalized_overrides
        settings.migration_warnings = list(dict.fromkeys(notices))
        settings.project_extensions_trusted = project_trusted
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
        checked = parts[0] in {"permissions", "execution", "sandbox", "extensions", "subagents", "tui"}
        data = copy.deepcopy(self.data) if checked else self.data
        cur = data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
        if checked:
            _validate(data)
        self.data = data

    def disabled_extensions(self) -> frozenset[str]:
        raw = self.get("extensions.disabled")
        if not raw:
            return frozenset()
        return frozenset(item.strip().removesuffix(".py") for item in raw)

    def plugin_enabled(self, name: str) -> bool:
        if name not in PLUGIN_IDS:
            raise ValueError(f"Unknown plugin {name!r}")
        explicit = self.get(f"plugins.{name}.enabled")
        enabled = explicit if explicit is not None else True
        if name == "web":
            enabled = enabled and (self.get("plugins.web.searchEnabled", True) or self.get("plugins.web.fetchEnabled", True))
        return bool(enabled)

    def save_user(self, patch: dict | UserSettingsPatch) -> None:
        """Persist only explicit changes, preserving unrelated user configuration."""
        if not isinstance(patch, UserSettingsPatch):
            patch = UserSettingsPatch(patch)
        notices: list[str] = []
        normalized = _normalize_layer(patch.values, notices)
        candidate = deep_merge(self.data, normalized)
        path = self.paths.user / "settings.json"
        saved = _read_json(path)
        if saved is None:
            saved = {"execution": {"mode": default_execution_mode()}}
        data = deep_merge(saved, patch.values)
        for provider, keys in patch.remove_provider_keys.items():
            _remove_provider_keys(data, provider, keys)
            _remove_provider_keys(candidate, provider, keys, prune_root=False)
            # Removing a user key reveals the trusted project or startup value.
            higher = self._provider_overrides(provider)
            inherited = {key: higher[key] for key in keys if key in higher}
            if inherited:
                candidate.setdefault("providers", {}).setdefault(provider, {}).update(inherited)
        _validate(candidate)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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

    def _provider_overrides(self, provider: str) -> dict:
        effective = {}
        if self.project_extensions_trusted and not same_directory(self.paths.project, self.paths.user):
            project = _read_json(self.paths.project / "settings.json") or {}
            project_providers = project.get("providers")
            if isinstance(project_providers, dict):
                effective = deep_merge(effective, project_providers.get(provider, {}))
        override_providers = self.overrides.get("providers")
        if isinstance(override_providers, dict):
            effective = deep_merge(effective, override_providers.get(provider, {}))
        return effective

    def check_provider_update(self, provider: str, config: dict) -> None:
        """Reject a login that would use a different endpoint on the next startup."""
        effective = deep_merge(config, self._provider_overrides(provider))
        for key in ("api", "baseUrl", "apiKeyEnv"):
            if effective.get(key) != config.get(key):
                raise ValueError(
                    f"Project or runtime settings override {provider}.{key}. "
                    "Use that configured value, update the override, or choose a custom Provider ID."
                )
