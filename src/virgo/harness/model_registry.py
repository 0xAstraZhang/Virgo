"""Model resolution, cached directories, and live capability metadata for AgentSession."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from .. import ai
from ..ai.model_metadata import cache_path, clean_metadata, read_catalog, write_metadata
from ..ai.models import base_url


def small_model_effort_for(model: ai.Model) -> str | None:
    if not model.reasoning:
        return None
    if "low" in (model.supported_reasoning_levels or ()):
        return "low"
    default = model.default_reasoning_level
    if model.supported_reasoning_levels is None or default in model.supported_reasoning_levels:
        return default
    return None


def small_model_effort_fallback_reason_for(model: ai.Model) -> str | None:
    if not model.reasoning:
        return "model does not support reasoning controls"
    selected = small_model_effort_for(model)
    if selected == "low":
        return None
    if selected is not None:
        return "low is unavailable; using the declared model default"
    if model.supported_reasoning_levels is None:
        return "reasoning capabilities are unknown; using the server default"
    return "low is unavailable and no valid model default is declared; using the server default"


class ModelRegistry:
    """Owns provider model resolution, cached catalogs, and live metadata refresh state."""

    def __init__(self, *, lock: threading.RLock, user_dir: Path, get_auth_path, settings, on_save_error):
        self._lock = lock
        self._user_dir = user_dir
        self._get_auth_path = get_auth_path
        self._settings = settings
        self._on_save_error = on_save_error
        self.discovered_models: dict[str, dict] = {}
        self._metadata_paths: dict[str, Path] = {}
        self._catalogs: dict[Path, list] = {}
        self._revisions: dict[str, int] = {}
        self.stop_event = threading.Event()
        self.refresh_done = threading.Event()
        self._metadata_pending = False

    def resolve(self, spec: str) -> ai.Model:
        with self._lock:
            return self._resolve(spec)

    def _resolve(self, spec: str) -> ai.Model:
        providers = self._settings.get("providers", {})
        candidate = ai.resolve_model(spec, providers)
        path = cache_path(self._user_dir, candidate)
        if self._metadata_paths.get(candidate.provider) != path:
            self._replace_metadata(
                candidate.provider, {item.id: item.metadata for item in self.cached_catalog(candidate)}
            )
            self._metadata_paths[candidate.provider] = path
        if metadata := self.discovered_models.get(candidate.spec):
            provider, model_id = candidate.provider, candidate.id
            config = providers.get(provider, {})
            models = config.get("models") or {}
            names = {
                "context_window": "contextWindow",
                "max_tokens": "maxTokens",
                "reasoning": "reasoning",
                "vision": "vision",
                "supported_reasoning_levels": "supportedReasoningLevels",
                "default_reasoning_level": "defaultReasoningLevel",
            }
            live = {names[key]: value for key, value in metadata.items() if key in names}
            providers = {
                **providers,
                provider: {**config, "models": {**models, model_id: {**live, **models.get(model_id, {})}}},
            }
        model = ai.resolve_model(spec, providers)
        model.auth_path = str(self._get_auth_path())
        if model.api == "anthropic":
            for key in ("fallbacks", "thinkingDisplay", "thinkingBinding"):
                model.options.setdefault(key, self._settings.get(f"anthropic.{key}"))
        return model

    def _replace_metadata(self, provider: str, models: dict) -> None:
        self.discovered_models = {
            spec: meta for spec, meta in self.discovered_models.items() if not spec.startswith(provider + "/")
        }
        self.discovered_models.update({f"{provider}/{key}": clean_metadata(value) for key, value in models.items()})

    def cached_catalog(self, connection: ai.Model) -> list:
        with self._lock:
            path = cache_path(self._user_dir, connection)
            if path not in self._catalogs:
                self._catalogs[path] = read_catalog(path)
            return list(self._catalogs[path])

    def catalog_revision(self, provider: str) -> int:
        with self._lock:
            return self._revisions.get(provider, 0)

    def remember_metadata(self, connection: ai.Model, models: list, *, diagnostic_store=None) -> None:
        with self._lock:
            path = cache_path(self._user_dir, connection)
            self._catalogs[path] = list(models)
            provider = connection.provider
            self._revisions[provider] = self._revisions.get(provider, 0) + 1
            self._replace_metadata(provider, {item.id: item.metadata for item in models})
            self._metadata_paths[provider] = path
            try:
                write_metadata(path, models)
            except OSError as error:
                self._on_save_error(error, path, diagnostic_store)
            self._metadata_pending = True

    def apply_pending(self, current_model: ai.Model, current_small: ai.Model) -> tuple[ai.Model, ai.Model] | None:
        with self._lock:
            if not self._metadata_pending:
                return None

            def refreshed(current: ai.Model) -> ai.Model:
                resolved = self._resolve(current.spec)
                # A directory refresh owns capacity/capability metadata, not the
                # runtime's transport, credentials or explicit provider options.
                model = replace(
                    current,
                    context_window=resolved.context_window,
                    max_tokens=resolved.max_tokens,
                    reasoning=resolved.reasoning,
                    vision=resolved.vision,
                    supported_reasoning_levels=resolved.supported_reasoning_levels,
                    default_reasoning_level=resolved.default_reasoning_level,
                    automatic_effort=resolved.automatic_effort,
                )
                return current if model == current else model

            shared_model = current_small is current_model
            model = refreshed(current_model)
            small = model if shared_model else refreshed(current_small)
            self._metadata_pending = False
            return model, small

    def small_model_for(self, model: ai.Model) -> ai.Model:
        host = urlsplit(base_url(model)).hostname
        if model.api == "anthropic" and (model.provider == "anthropic" or host == "api.anthropic.com"):
            return self.resolve(f"{model.provider}/claude-haiku-5-5")
        is_openai = model.provider in {"openai", "openai-codex"} or model.api == "openai-codex"
        if model.api in {"openai", "openai-responses", "openai-codex"} and (
            is_openai or host == "api.openai.com"
        ):
            return self.resolve(f"{model.provider}/gpt-6-luna")
        return model
