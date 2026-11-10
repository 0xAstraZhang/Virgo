"""Model and effort choices shared by the harness and interactive clients."""

from __future__ import annotations

import ipaddress
import math
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..ai.auth_presets import API_PRESETS, AUTH_PRESETS
from ..ai.types import Model

if TYPE_CHECKING:
    from .session import AgentSession

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ModelChoice:
    spec: str
    label: str
    description: str


@dataclass(frozen=True)
class PreferenceRequest:
    kind: str
    models: tuple[ModelChoice, ...]
    model: str
    effort: str


@dataclass(frozen=True)
class PreferenceChoice:
    model: str
    effort: str
    persist: bool


def model_choices(session: AgentSession) -> tuple[ModelChoice, ...]:
    """List known models with configured authentication, without making API calls."""
    candidates = [session.model.spec, session.settings.get("model"), session.small_model.spec]
    configured = session.settings.get("providers", {})
    credentials = session.auth.all()
    for provider, config in sorted(configured.items()):
        if isinstance(config, dict) and isinstance(config.get("models"), dict):
            candidates.extend(f"{provider}/{model_id}" for model_id in sorted(config["models"]))
    for provider, preset in {**AUTH_PRESETS, **API_PRESETS}.items():
        candidates.append(f"{provider}/{preset['model']}")
        candidates.extend(f"{provider}/{model_id}" for model_id in preset["config"].get("models", {}))
    choices: dict[str, ModelChoice] = {}
    for spec in candidates:
        if not isinstance(spec, str) or not spec.strip():
            continue
        try:
            model = session.resolve(spec)
        except (TypeError, ValueError):
            continue
        config = configured.get(model.provider, {})
        catalog = config.get("models") if isinstance(config, dict) else None
        if isinstance(catalog, dict) and model.id not in catalog:
            continue
        if not _model_available(model, credentials, catalog):
            continue
        if model.spec not in choices:
            choices[model.spec] = ModelChoice(
                model.spec,
                model.id,
                f"{model.provider} · {model.context_window:,} context",
            )
    return tuple(choices.values())


def provider_name(provider: str) -> str:
    preset = AUTH_PRESETS.get(provider) or API_PRESETS.get(provider)
    return preset["name"] if preset else {"openai": "OpenAI", "faux": "Faux"}.get(provider, provider)


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _model_available(model: Model, credentials: dict, catalog) -> bool:
    if not model.id or any(character.isspace() for character in model.id):
        return False
    if model.api == "faux":
        return True
    if model.api not in ("anthropic", "openai", "openai-codex", "openai-responses", "google-gemini-cli"):
        return False
    if not model.base_url and model.provider not in ("anthropic", "openai", "openai-codex"):
        return False
    base = model.base_url or os.environ.get(model.options.get("baseUrlEnv", ""), "")
    if base:
        try:
            endpoint = urlparse(base)
            if endpoint.scheme not in ("http", "https") or not endpoint.hostname:
                return False
        except ValueError:
            return False
    credential = credentials.get(model.provider)
    if credential:
        kind = credential.get("type")
        if kind not in ("oauth", "api_key"):
            return False
        if not _nonempty(credential.get("access" if kind == "oauth" else "key")):
            return False
        connection = credential.get("connection")
        if connection is not None and (
            not isinstance(connection, dict)
            or connection.get("api") != model.api
            or str(connection.get("baseUrl", "")).rstrip("/") != base.rstrip("/")
        ):
            return False
        if kind == "oauth":
            preset = AUTH_PRESETS.get(model.provider)
            if preset is None or model.api != preset["config"]["api"]:
                return False
            expected = preset["config"].get("baseUrl", "")
            if base and expected and base.rstrip("/") != expected.rstrip("/"):
                return False
            try:
                expires = float(credential.get("expires", 0))
            except (TypeError, ValueError):
                return False
            if not math.isfinite(expires):
                return False
            if expires <= time.time() + 60 and not _nonempty(credential.get("refresh")):
                return False
        metadata = credential
    else:
        key = model.options.get("apiKey")
        if model.api not in ("openai-codex", "google-gemini-cli"):
            key = key or os.environ.get(model.api_key_env or "")
        if model.provider == "anthropic":
            key = key or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if not _nonempty(key):
            # The OpenAI-compatible transport supports explicitly configured,
            # unauthenticated local servers; a remote URL alone is insufficient.
            if model.api != "openai" or not isinstance(catalog, dict):
                return False
            try:
                parsed = urlparse(base)
                host = parsed.hostname
                local = parsed.scheme in ("http", "https") and (
                    host == "localhost" or bool(host and ipaddress.ip_address(host).is_loopback)
                )
            except ValueError:
                return False
            if not local:
                return False
        metadata = model.options.get("authMetadata") or {}
    if not isinstance(metadata, dict):
        return False
    if model.api == "openai-codex":
        return bool(metadata.get("accountId"))
    if model.api == "google-gemini-cli":
        return bool(metadata.get("projectId"))
    return True


def configure_preference(session: AgentSession, kind: str) -> None:
    """Apply an explicit picker result; cancellation and previews do not mutate state."""
    if kind not in ("model", "effort"):
        raise ValueError(f"unknown preference kind: {kind}")
    effort = session.effort if session.effort in EFFORT_LEVELS else "medium"
    models = model_choices(session) if kind == "model" else (
        ModelChoice(session.model.spec, session.model.id, session.model.provider),
    )
    if kind == "model" and not models:
        session.ui.print("No available models. Use /login to connect a provider.", "warn")
        return
    request = PreferenceRequest(kind, models, session.model.spec, effort)
    choice = session.ui.choose_preferences(request)
    if choice is None:
        kept = session.model.id if kind == "model" else session.effort
        session.ui.print(f"Kept {kind} as {kept}")
        return
    if not isinstance(choice, PreferenceChoice) or choice.effort not in EFFORT_LEVELS:
        raise ValueError("invalid model or effort selection")
    if kind == "model" and choice.model not in {model.spec for model in request.models}:
        raise ValueError("selected model was not offered")
    if kind == "effort" and choice.model != request.model:
        raise ValueError("the effort picker cannot change the model")
    if kind == "model":
        # Validate before saving defaults so a bad model cannot corrupt settings.
        session.resolve(choice.model)
    patch = {"effort": choice.effort}
    if kind == "model":
        patch["model"] = choice.model
    if choice.persist:
        session.settings.save_user(patch)
    if kind == "model" and choice.model != session.model.spec:
        session.set_model(choice.model)
        session.small_model = session._small_model(None)
    session.effort = choice.effort
    scope = "saved as default" if choice.persist else "this session only"
    if kind == "model":
        session.ui.print(f"Model: {session.model.id} · {session.effort} effort · {scope}", "ok")
    else:
        session.ui.print(f"Effort: {session.effort} · {scope}", "ok")
