"""Model and effort choices shared by the harness and interactive clients."""

from __future__ import annotations

import ipaddress
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..ai.auth_presets import API_PRESETS, AUTH_PRESETS
from ..ai.catalog import CatalogCancelled, CatalogError, fetch_model_catalog
from ..ai.models import BUILTIN_PROVIDERS
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
    """Refresh connected providers; only their returned IDs enter the real-model picker."""
    configured = session.settings.get("providers", {})
    credentials = session.auth.all()
    signal = session.main.agent.signal
    choices: dict[str, ModelChoice] = {}
    connections = []
    for provider in sorted(set(BUILTIN_PROVIDERS) | set(configured) | set(credentials)):
        try:
            model = session.resolve(f"{provider}/_catalog")
        except (TypeError, ValueError):
            continue
        config = configured.get(provider, {})
        if model.api == "faux":
            # The offline test/demo provider has no remote service.
            ids = dict(config.get("models") or {})
            for selected in (session.model, session.small_model):
                if selected.provider == provider:
                    ids.setdefault(selected.id, {})
            for model_id in ids:
                try:
                    local = session.resolve(f"{provider}/{model_id}")
                except (TypeError, ValueError):
                    continue
                choices[local.spec] = ModelChoice(local.spec, local.id, f"{provider} · {local.context_window:,} context")
            continue
        if not _model_available(model, credentials):
            continue
        connections.append(model)
    if connections:
        session.ui.print("Fetching live model lists…", "dim")
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="virgo-discovery") as pool:
        pending = {pool.submit(fetch_model_catalog, model, signal): model for model in connections}
        for future in as_completed(pending):
            provider = pending[future].provider
            # Never retain a failed provider's previous directory or stale metadata.
            session.discovered_models = {
                spec: meta for spec, meta in session.discovered_models.items() if not spec.startswith(provider + "/")
            }
            try:
                models = future.result()
            except CatalogCancelled:
                return ()
            except CatalogError as error:
                session.ui.print(f"{provider_name(provider)}: {error}", "warn")
                continue
            if signal.is_set():
                return ()
            if not models:
                session.ui.print(f"{provider_name(provider)}: the live directory returned no selectable models.", "warn")
            for item in models:
                spec = f"{provider}/{item.id}"
                session.discovered_models[spec] = item.metadata
                context = item.metadata.get("context_window")
                description = f"{provider} · {context:,} context" if context else provider
                choices[spec] = ModelChoice(spec, item.id, description)
    return tuple(sorted(choices.values(), key=lambda choice: (choice.spec != session.model.spec, choice.spec)))


def provider_name(provider: str) -> str:
    preset = AUTH_PRESETS.get(provider) or API_PRESETS.get(provider)
    return preset["name"] if preset else {"openai": "OpenAI", "faux": "Faux"}.get(provider, provider)


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _model_available(model: Model, credentials: dict) -> bool:
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
            if model.api != "openai":
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
    if session.main.agent.signal.is_set():
        return
    if kind == "model" and not models:
        session.ui.print("No live models available. Check /login, retry /model, or use /model provider/model-id.", "warn")
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
