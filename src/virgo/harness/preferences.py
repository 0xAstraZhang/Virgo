"""Model and effort choices shared by the harness and interactive clients."""

from __future__ import annotations

import ipaddress
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..ai.auth_presets import API_PRESETS, AUTH_PRESETS
from ..ai.catalog import CatalogCancelled, CatalogError, fetch_model_catalog
from ..ai.models import BUILTIN_PROVIDERS, base_url
from ..ai.types import Model

if TYPE_CHECKING:
    from .session import AgentSession

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ModelChoice:
    spec: str
    label: str
    description: str
    released_at: float | None = None


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
    """Read the latest local directory snapshot without a network request."""
    configured = session.settings.get("providers", {})
    credentials = session.auth.all()
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
                choices[local.spec] = ModelChoice(local.spec, local.id, session.t("harness.models.context", provider=provider, tokens=local.context_window))
            continue
        if not _model_available(model, credentials):
            continue
        connections.append(model)
    for connection in connections:
        for item in session.cached_model_catalog(connection):
            context = item.metadata.get("context_window")
            description = session.t("harness.models.context", provider=connection.provider, tokens=context) if context else connection.provider
            spec = f"{connection.provider}/{item.id}"
            choices[spec] = ModelChoice(spec, item.id, description, item.released_at)
    # Keep each connection's server order independent of parallel fetch completion.
    ordered = sorted(choices.values(), key=lambda choice: choice.spec.split("/", 1)[0])
    # Stable ties preserve server order; undated models follow dated models.
    return tuple(sorted(ordered, key=lambda choice: (choice.released_at is None, -(choice.released_at or 0))))


def start_model_refresh(session: AgentSession) -> None:
    """One bounded, background lookup per connected provider at process startup."""
    configured = session.settings.get("providers", {})
    credentials = session.auth.all()
    connections = []
    for provider in sorted(set(BUILTIN_PROVIDERS) | set(configured) | set(credentials)):
        try:
            model = session.resolve(f"{provider}/_catalog")
        except (TypeError, ValueError):
            continue
        if model.api not in {"faux", "google-gemini-cli"} and _model_available(model, credentials):
            connections.append(model)
    if not connections:
        session._catalog_refresh_done.set()
        return
    slots = threading.Semaphore(4)
    owner = session.store

    def refresh(connection):
        with slots:
            if session._catalog_stop.is_set():
                return
            try:
                models = fetch_model_catalog(connection, session._catalog_stop)
                with session._lock:
                    if session._catalog_stop.is_set():
                        return
                    # A settings/login change during the request must not publish into a different endpoint.
                    current = session.resolve(f"{connection.provider}/_catalog")
                    if current.api != connection.api or base_url(current) != base_url(connection):
                        return
                    session.remember_model_metadata(connection, models, diagnostic_store=owner)
                    if not models:
                        session.ui.print(session.t("harness.models.empty_live", provider=provider_name(connection.provider)), "warn")
            except CatalogCancelled:
                pass
            except CatalogError as error:
                if not session._catalog_stop.is_set():
                    session.report_error(error, phase="catalog.refresh", store=owner,
                                         context={"provider": connection.provider, "api": connection.api}, notify=False)
                    session.ui.print(session.t("harness.models.keep", provider=provider_name(connection.provider), error=error), "warn")
            except Exception as error:
                if not session._catalog_stop.is_set():
                    session.report_error(error, phase="catalog.refresh", store=owner,
                                         context={"provider": connection.provider, "api": connection.api}, notify=False)
                    session.ui.print(session.t("harness.models.refresh_failed", provider=provider_name(connection.provider)), "warn")

    def run():
        try:
            workers = [threading.Thread(target=refresh, args=(model,), daemon=True, name="virgo-startup-provider") for model in connections]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        finally:
            session._catalog_refresh_done.set()

    threading.Thread(target=run, daemon=True, name="virgo-startup-catalog").start()


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
    base = base_url(model)
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
        session.ui.print(session.t("harness.preferences.none"), "warn")
        return
    request = PreferenceRequest(kind, models, session.model.spec, effort)
    choice = session.ui.choose_preferences(request)
    if choice is None:
        kept = session.model.id if kind == "model" else session.effort
        session.ui.print(session.t("harness.models.kept", kind=session.t("harness.preferences." + kind), value=kept))
        return
    if not isinstance(choice, PreferenceChoice) or choice.effort not in EFFORT_LEVELS:
        raise ValueError(session.t("harness.preferences.invalid"))
    if kind == "model" and choice.model not in {model.spec for model in request.models}:
        raise ValueError(session.t("harness.preferences.not_offered"))
    if kind == "effort" and choice.model != request.model:
        raise ValueError(session.t("harness.preferences.effort_only"))
    if kind == "model":
        # Validate before saving defaults so a bad model cannot corrupt settings.
        session.resolve(choice.model)
    patch = {"effort": choice.effort}
    if kind == "model":
        patch["model"] = choice.model
    if choice.persist:
        session.settings.save_user(patch)
    if kind == "model":
        session.set_model(choice.model)
    session.effort = choice.effort
    scope = session.t("harness.preferences.persisted") if choice.persist else session.t("harness.preferences.session_only")
    if kind == "model":
        session.ui.print(session.t("harness.models.selected", model=session.model.id, effort=session.effort, scope=scope), "ok")
    else:
        session.ui.print(session.t("harness.models.effort", effort=session.effort, scope=scope), "ok")
