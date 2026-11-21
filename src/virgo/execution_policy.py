"""Immutable execution permission policies and built-in execution modes.

This core module has no harness or OS-backend imports. Rules follow deny-first
precedence with a boolean network toggle.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

Access = Literal["read", "write"]
Effect = Literal["allow", "deny"]

PROTECTED_NAMES = (".virgo",)

PRIVATE_HOME_RELATIVES = (
    ".virgo",
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".azure",
    ".config/gcloud",
    ".config/gh",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".docker/config.json",
)

_BLOCKED_ENV_EXACT = frozenset({
    "VIRGO_HOME",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "GPG_AGENT_INFO",
    "BASH_ENV",
    "ENV",
    "PROMPT_COMMAND",
    "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "NETRC",
    "NPM_TOKEN",
    "NODE_AUTH_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "GLAB_TOKEN",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "PYPI_API_TOKEN",
    "UV_PUBLISH_TOKEN",
    "POETRY_PYPI_TOKEN_PYPI",
    "CARGO_REGISTRY_TOKEN",
    "DATABASE_URL",
    "REDIS_URL",
    "KUBECONFIG",
    "GOOGLE_APPLICATION_CREDENTIALS",
})

_BLOCKED_ENV_PREFIXES = (
    "VIRGO_",
    "OPENAI_",
    "ANTHROPIC_",
    "GEMINI_",
    "GOOGLE_API_",
    "VERTEX_",
    "AZURE_",
    "AWS_",
    "CLOUDFLARE_",
    "DEEPSEEK_",
    "DASHSCOPE_",
    "MOONSHOT_",
    "ZHIPUAI_",
    "MISTRAL_",
    "COHERE_",
    "GROQ_",
    "XAI_",
    "TOGETHER_",
    "FIREWORKS_",
    "OPENROUTER_",
    "REPLICATE_",
    "SUPABASE_",
    "STRIPE_",
    "SLACK_",
    "DISCORD_",
    "TELEGRAM_",
    "TWILIO_",
    "SENDGRID_",
    "DATADOG_",
    "SENTRY_AUTH_",
    "BASH_FUNC_",
)

_SECRET_ENV_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_?KEY|"
    r"CREDENTIAL|CREDENTIALS|ACCESS_?KEY|AUTH_?TOKEN|SESSION_?KEY)(?:$|_)"
)


def default_private_paths(user_dir: Path, *, home: Path | None = None) -> tuple[Path, ...]:
    """Return high-sensitivity credential directories and files to shield in sandbox modes."""
    paths: list[Path] = [Path(user_dir).resolve()]
    try:
        resolved_home = (Path(home) if home is not None else Path.home()).resolve()
    except OSError:
        return tuple(dict.fromkeys(paths))
    for rel in PRIVATE_HOME_RELATIVES:
        candidate = _normalize_path(resolved_home / rel)
        paths.append(candidate)
        if candidate.is_symlink():
            try:
                paths.append(candidate.resolve(strict=True))
            except OSError:
                pass
    return tuple(dict.fromkeys(paths))


def filter_sandbox_environment(env: dict[str, str]) -> dict[str, str]:
    """Strip ambient credentials and loader-injection variables while keeping proxies and tool vars."""
    filtered: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str) or "\x00" in key or "\x00" in value:
            continue
        if value.startswith("()"):
            continue
        upper = key.upper()
        if upper in _BLOCKED_ENV_EXACT or upper.startswith(_BLOCKED_ENV_PREFIXES):
            continue
        if _SECRET_ENV_RE.search(upper):
            continue
        filtered[key] = value
    return filtered


def is_native_windows() -> bool:
    """Return True only for native Windows (WSL2 runs Linux Python)."""
    return sys.platform == "win32"


def is_windows_sandbox_supported() -> bool:
    """Return True when native Windows supports unelevated Standard AppContainer (Win11 null.sys Feature 60692039)."""
    if not is_native_windows():
        return False
    from ._windows_probe import is_windows_appcontainer_supported

    return bool(is_windows_appcontainer_supported())


def default_execution_mode() -> str:
    """Default execution mode: Workspace-Edit on supported Tier 1 hosts, No-Limit on unsupported Windows."""
    if is_native_windows() and not is_windows_sandbox_supported():
        return "no-limit"
    return "workspace-edit"


def parse_network(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError("network_allowed must be a boolean")


def _normalize_path(path: Path) -> Path:
    text = os.path.normpath(path)
    # POSIX normpath preserves a leading '//' (implementation-defined root).
    # Collapse it so lexical comparisons cannot miss a leading '/' deny root.
    if os.name == "posix" and text.startswith("//"):
        text = "/" + text.lstrip("/")
    return Path(text)


def lexical_path(value: str | Path, cwd: Path, *, home: Path | None = None) -> Path:
    """Expand current-user ``~`` and normalize spelling without following symlinks."""
    text = str(value)
    if "\0" in text:
        raise ValueError("A resource path cannot contain a null byte")
    if text.startswith("~"):
        user_home = home if home is not None else Path.home()
        if text == "~":
            text = str(user_home)
        elif text[1:2] in {"/", "\\"}:
            suffix = text[2:].lstrip("/\\")
            if Path(suffix).drive or Path(suffix).is_absolute():
                raise ValueError("Resource path after ~ must be relative to the user home")
            text = str(user_home / suffix)
        else:
            raise ValueError("Resource paths support ~ for the current user, not ~username")
    path = Path(text)
    if not path.is_absolute():
        path = cwd / path
    path = _normalize_path(path)
    if not path.is_absolute():
        raise ValueError("Resource paths require an absolute working directory and cannot be drive-relative")
    return path


@dataclass(frozen=True)
class PathRule:
    path: Path
    access: Access
    effect: Effect = "allow"

    def __post_init__(self):
        object.__setattr__(self, "path", _normalize_path(Path(self.path)))
        if self.access not in {"read", "write"} or self.effect not in {"allow", "deny"}:
            raise ValueError("Path rules require read/write access and allow/deny effect")

    def covers(self, target: Path, access: Access) -> bool:
        if not target.is_relative_to(self.path):
            return False
        # Writable grants include reads. Denying reads also denies mutations;
        # denying writes leaves independently authorized reads available.
        return self.access == access or (self.effect, self.access) in {
            ("allow", "write"), ("deny", "read"),
        }

    def as_dict(self) -> dict:
        return {"path": str(self.path), "access": self.access, "effect": self.effect}


@dataclass(frozen=True)
class PermissionPolicy:
    filesystem: tuple[PathRule, ...]
    network_allowed: bool
    unrestricted: bool = False

    def __post_init__(self):
        if type(self.network_allowed) is not bool or type(self.unrestricted) is not bool:
            raise ValueError("Network and unrestricted permissions must be booleans")
        if self.unrestricted and not self.network_allowed:
            raise ValueError("Unrestricted execution cannot claim a network restriction")
        if any(not isinstance(rule, PathRule) for rule in self.filesystem):
            raise ValueError("Filesystem permissions must contain PathRule values")
        object.__setattr__(self, "filesystem", tuple(dict.fromkeys(self.filesystem)))

    def resolve(self, workspace: Path) -> PermissionPolicy:
        return replace(self, filesystem=tuple(
            replace(rule, path=lexical_path(rule.path, workspace)) for rule in self.filesystem
        ))

    def hard_denied(self, target: Path) -> bool:
        """Return True if ``target`` falls under an unoverridable read-deny (private credential) rule."""
        if self.unrestricted:
            return False
        normalized = _normalize_path(target)
        return any(
            rule.effect == "deny" and rule.access == "read" and rule.covers(normalized, "read")
            for rule in self.filesystem
        )

    def _matching_write_rules(self, normalized: Path) -> list[PathRule]:
        return [rule for rule in self.filesystem if rule.covers(normalized, "write")]

    def denied(self, target: Path, access: Access) -> bool:
        if self.unrestricted:
            return False
        normalized = _normalize_path(target)
        if self.hard_denied(normalized):
            return True
        if access == "read":
            return False
        matches = self._matching_write_rules(normalized)
        if not matches:
            return False
        max_depth = max(len(rule.path.parts) for rule in matches)
        return any(rule.effect == "deny" and len(rule.path.parts) == max_depth for rule in matches)

    def allows(self, target: Path, access: Access) -> bool:
        if self.unrestricted:
            return True
        normalized = _normalize_path(target)
        if self.hard_denied(normalized):
            return False
        if access == "read":
            return any(rule.effect == "allow" and rule.covers(normalized, "read") for rule in self.filesystem)
        matches = self._matching_write_rules(normalized)
        if not matches:
            return False
        max_depth = max(len(rule.path.parts) for rule in matches)
        top = [rule for rule in matches if len(rule.path.parts) == max_depth]
        return any(rule.effect == "allow" for rule in top) and not any(rule.effect == "deny" for rule in top)

    def allow_roots(self, access: Access) -> tuple[Path, ...]:
        return tuple(dict.fromkeys(rule.path for rule in self.filesystem
                                   if rule.effect == "allow" and (
                                       rule.access == access or access == "read" and rule.access == "write")))

    def deny_roots(self, access: Access) -> tuple[Path, ...]:
        return tuple(dict.fromkeys(rule.path for rule in self.filesystem
                                   if rule.effect == "deny" and (
                                       rule.access == access or access == "write" and rule.access == "read")))

    def with_rules(self, *rules: PathRule) -> PermissionPolicy:
        return replace(self, filesystem=(*self.filesystem, *rules))

    def grant(self, *rules: PathRule) -> PermissionPolicy:
        """Apply user-approved grants, overriding confirmable write-denies while preserving hard read-denies."""
        write_allows = [r.path for r in rules if r.effect == "allow" and r.access == "write"]
        kept = [
            existing for existing in self.filesystem
            if not (
                existing.effect == "deny"
                and existing.access == "write"
                and any(existing.path.is_relative_to(granted) for granted in write_allows)
            )
        ]
        return replace(self, filesystem=(*kept, *rules))

    def as_dict(self) -> dict:
        return {"filesystem": [rule.as_dict() for rule in self.filesystem],
                "network_allowed": self.network_allowed, "unrestricted": self.unrestricted}


@dataclass(frozen=True)
class ExecutionMode:
    id: str
    name: str
    policy: PermissionPolicy

    @property
    def web_allowed(self) -> bool:
        return True

    @property
    def read_paths(self) -> tuple[str, ...]:
        return tuple(str(path) for path in self.policy.allow_roots("read"))

    @property
    def write_paths(self) -> tuple[str, ...]:
        return tuple(str(path) for path in self.policy.allow_roots("write"))

    @property
    def network(self) -> bool:
        return self.policy.network_allowed

    def flags(self) -> dict:
        return {
            "read_paths": [str(path) for path in self.policy.allow_roots("read")],
            "write_paths": [str(path) for path in self.policy.allow_roots("write")],
            "read_deny_paths": [str(path) for path in self.policy.deny_roots("read")],
            "write_deny_paths": [str(path) for path in self.policy.deny_roots("write")],
            "network_allowed": self.policy.network_allowed,
            "unrestricted": self.policy.unrestricted,
            "web_allowed": self.web_allowed,
        }

    def resolve(self, workspace: Path) -> ExecutionMode:
        return replace(self, policy=self.policy.resolve(workspace))


HOST_MODE = ExecutionMode(
    "no-limit", "No-Limit",
    PermissionPolicy((), True, unrestricted=True),
)
READ_ONLY_MODE = ExecutionMode(
    "read-only", "Read-Only",
    PermissionPolicy((PathRule(Path("."), "read"),), False),
)
WORKSPACE_EDIT_MODE = ExecutionMode(
    "workspace-edit", "Workspace-Edit",
    PermissionPolicy((PathRule(Path("."), "write"),), True),
)
DEFAULT_MODES = {mode.id: mode for mode in (READ_ONLY_MODE, WORKSPACE_EDIT_MODE, HOST_MODE)}


@dataclass(frozen=True)
class PermissionSnapshot:
    mode: ExecutionMode
    approval: str
    revision: int
