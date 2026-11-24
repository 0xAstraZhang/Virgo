"""Model and effort choices shared by the harness and interactive clients."""

from __future__ import annotations

import ipaddress
import math
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..ai import resolved_effort
from ..ai.auth_presets import API_PRESETS, AUTH_PRESETS
from ..ai.catalog import CatalogCancelled, CatalogError, fetch_model_catalog
from ..ai.models import BUILTIN_PROVIDERS, base_url
from ..ai.types import Model

if TYPE_CHECKING:
    from .session import AgentSession

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def effort_levels(model: Model) -> tuple[str, ...]:
    if not model.reasoning or model.supported_reasoning_levels is None:
        return EFFORT_LEVELS
    return model.supported_reasoning_levels


def effort_label(model: Model, requested: str | None, t, *, show_requested: bool = True) -> str:
    effective = resolved_effort(model, requested).effective
    label = effective or t("harness.effort.default" if model.reasoning else "ui.modes.off")
    return f"{requested}→{label}" if show_requested and requested and requested != label else label


@dataclass(frozen=True)
class ModelChoice:
    spec: str
    label: str
    description: str
    released_at: float | None = None
    effort_levels: tuple[str, ...] = EFFORT_LEVELS
    preferred_effort: str = "medium"
    reasoning: bool = False

    @classmethod
    def from_model(cls, model: Model, description: str, requested: str, released_at: float | None = None) -> ModelChoice:
        levels = effort_levels(model)
        effective = resolved_effort(model, requested).effective
        candidates = (effective, requested, model.default_reasoning_level, "medium")
        preferred = next((level for level in candidates if level in levels), levels[0] if levels else requested)
        return cls(model.spec, model.id, description, released_at, levels, preferred, model.reasoning)


@dataclass(frozen=True)
class PreferenceRequest:
    kind: str
    models: tuple[ModelChoice, ...]
    model: str
    effort: str

    def offers(self, choice: PreferenceChoice) -> bool:
        model = next((item for item in self.models if item.spec == choice.model), None)
        if model is None or (self.kind == "effort" and choice.model != self.model):
            return False
        if not model.effort_levels:
            # Selecting the model alone must preserve the saved preference.
            return self.kind == "model" and choice.effort == self.effort
        return choice.effort in model.effort_levels


@dataclass(frozen=True)
class PreferenceChoice:
    model: str
    effort: str
    persist: bool


def _catalog_connections(session: AgentSession, providers: set[str]) -> Iterator[Model]:
    for provider in sorted(providers):
        try:
            yield session.resolve(f"{provider}/_catalog")
        except (TypeError, ValueError):
            continue


def _connected_remote_models(session: AgentSession) -> list[Model]:
    credentials = session.auth.all()
    providers = set(BUILTIN_PROVIDERS) | set(session.settings.get("providers", {})) | set(credentials)
    return [
        model for model in _catalog_connections(session, providers)
        if model.api != "faux" and _model_available(model, credentials)
    ]


def _has_uncached_remote_provider(session: AgentSession) -> bool:
    return any(not session.cached_model_catalog(connection) for connection in _connected_remote_models(session))


def model_choices(session: AgentSession) -> tuple[ModelChoice, ...]:
    """Read the latest local directory snapshot without a network request."""
    configured = session.settings.get("providers", {})
    choices: dict[str, ModelChoice] = {}
    # Offline providers offer only configured and selected models.
    for model in _catalog_connections(session, {*configured, session.model.provider, session.small_model.provider}):
        if model.api != "faux":
            continue
        provider = model.provider
        ids = dict(configured.get(provider, {}).get("models") or {})
        for selected in (session.model, session.small_model):
            if selected.provider == provider:
                ids.setdefault(selected.id, {})
        for model_id in ids:
            try:
                local = session.resolve(f"{provider}/{model_id}")
            except (TypeError, ValueError):
                continue
            description = session.t("harness.models.context", provider=provider, tokens=local.context_window)
            choices[local.spec] = ModelChoice.from_model(local, description, session.effort)
    for connection in _connected_remote_models(session):
        for item in session.cached_model_catalog(connection):
            context = item.metadata.get("context_window")
            description = session.t("harness.models.context", provider=connection.provider, tokens=context) if context else connection.provider
            spec = f"{connection.provider}/{item.id}"
            try:
                model = session.resolve(spec)
            except (TypeError, ValueError):
                continue
            choices[spec] = ModelChoice.from_model(model, description, session.effort, item.released_at)
    # Keep each connection's server order independent of parallel fetch completion.
    ordered = sorted(choices.values(), key=lambda choice: choice.spec.split("/", 1)[0])
    # Stable ties preserve server order; undated models follow dated models.
    return tuple(sorted(ordered, key=lambda choice: (choice.released_at is None, -(choice.released_at or 0))))


def start_model_refresh(session: AgentSession) -> None:
    """One bounded, background lookup per connected provider at process startup."""
    connections = _connected_remote_models(session)
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
                with session._lock:
                    revision = session._catalog_revisions.get(connection.provider, 0)
                models = fetch_model_catalog(connection, session._catalog_stop)
                _publish_catalog(session, connection, models, owner, revision=revision)
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


def _publish_catalog(session: AgentSession, connection: Model, models: list, owner, *, revision: int | None = None) -> int | None:
    """Publish one connection's directory snapshot after the endpoint still matches."""
    with session._lock:
        if session._catalog_stop.is_set():
            return None
        # A login on the same endpoint may have already published the new account's models.
        if revision is not None and session._catalog_revisions.get(connection.provider, 0) != revision:
            return None
        # A settings/login change during the request must not publish into a different endpoint.
        current = session.resolve(f"{connection.provider}/_catalog")
        if current.api != connection.api or base_url(current) != base_url(connection):
            return None
        session.remember_model_metadata(connection, models, diagnostic_store=owner)
        if not models:
            session.ui.print(session.t("harness.models.empty_live", provider=provider_name(connection.provider)), "warn")
    return len(models)


def refresh_provider_catalog(session: AgentSession, provider: str) -> int | None:
    """Fetch one provider's live directory exactly once and publish it for /model."""
    try:
        connection = session.resolve(f"{provider}/_catalog")
    except (TypeError, ValueError):
        return None
    if connection.api == "faux" or not _model_available(connection, session.auth.all()):
        return None
    try:
        models = fetch_model_catalog(connection, session.main.agent.signal)
    except CatalogCancelled:
        return None
    except Exception as error:
        if not session._catalog_stop.is_set():
            if not isinstance(error, CatalogError):
                error = CatalogError("Could not read the model directory. Check the connection and login, then retry /model.")
            session.report_error(error, phase="catalog.login", store=session.store,
                                 context={"provider": connection.provider, "api": connection.api}, notify=False)
            session.ui.print(session.t("harness.models.login_failed", provider=provider_name(connection.provider)), "warn")
        return None
    return _publish_catalog(session, connection, models, session.store)


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
    if model.api not in ("anthropic", "openai", "openai-codex", "openai-responses"):
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
        if model.api != "openai-codex":
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
    return True


def configure_preference(session: AgentSession, kind: str) -> None:
    """Apply an explicit picker result; cancellation and previews do not mutate state."""
    if kind not in ("model", "effort"):
        raise ValueError(f"unknown preference kind: {kind}")
    # A missing provider can publish during model_choices, so check cache coverage first.
    refreshing = kind == "model" and not session._catalog_refresh_done.is_set()
    uncached = refreshing and _has_uncached_remote_provider(session)
    models = model_choices(session) if kind == "model" else (
        ModelChoice.from_model(session.model, session.model.provider, session.effort),
    )
    if refreshing and (not models or uncached):
        deadline = time.monotonic() + 5
        while not session._catalog_refresh_done.is_set():
            if session.main.agent.signal.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            session._catalog_refresh_done.wait(min(0.05, remaining))
        if session.main.agent.signal.is_set():
            return
        models = model_choices(session)
    if session.main.agent.signal.is_set():
        return
    if kind == "model" and not models:
        session.ui.print(session.t("harness.preferences.none"), "warn")
        return
    if kind == "effort" and not models[0].effort_levels:
        session.ui.print(session.t("harness.effort.no_levels"), "warn")
        return
    effort = models[0].preferred_effort if kind == "effort" else session.effort
    request = PreferenceRequest(kind, models, session.model.spec, effort)
    choice = session.ui.choose_preferences(request)
    if choice is None:
        kept = session.model.id if kind == "model" else effort_label(session.model, session.effort, session.t)
        session.ui.print(session.t("harness.models.kept", kind=session.t("harness.preferences." + kind), value=kept))
        return
    if not isinstance(choice, PreferenceChoice):
        raise ValueError(session.t("harness.preferences.invalid"))
    if kind == "model" and choice.model not in {model.spec for model in request.models}:
        raise ValueError(session.t("harness.preferences.not_offered"))
    if kind == "effort" and choice.model != request.model:
        raise ValueError(session.t("harness.preferences.effort_only"))
    if not request.offers(choice):
        raise ValueError(session.t("harness.preferences.invalid"))
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
    label = effort_label(session.model, session.effort, session.t)
    if kind == "model":
        session.ui.print(session.t("harness.models.selected", model=session.model.id, effort=label, scope=scope), "ok")
    else:
        session.ui.print(session.t("harness.models.effort", effort=label, scope=scope), "ok")
