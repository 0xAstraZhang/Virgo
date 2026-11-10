"""Model and effort choices shared by the harness and interactive clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..ai.auth_presets import API_PRESETS, AUTH_PRESETS
from ..ai.models import KNOWN_MODELS

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
    """Build an offline catalog from models Virgo already knows how to resolve."""
    candidates = [session.model.spec, session.settings.get("model"), session.small_model.spec]
    configured = session.settings.get("providers", {})
    for provider, config in sorted(configured.items()):
        if isinstance(config, dict) and isinstance(config.get("models"), dict):
            candidates.extend(f"{provider}/{model_id}" for model_id in sorted(config["models"]))
    for provider, preset in {**AUTH_PRESETS, **API_PRESETS}.items():
        candidates.append(f"{provider}/{preset['model']}")
        candidates.extend(f"{provider}/{model_id}" for model_id in preset["config"].get("models", {}))
    candidates.extend(f"anthropic/{model_id}" for model_id in KNOWN_MODELS)
    choices: dict[str, ModelChoice] = {}
    for spec in candidates:
        if not isinstance(spec, str) or not spec.strip():
            continue
        try:
            model = session.resolve(spec)
        except (TypeError, ValueError):
            continue
        if model.spec not in choices:
            choices[model.spec] = ModelChoice(
                model.spec,
                model.id,
                f"{model.provider} · {model.context_window:,} context",
            )
    return tuple(choices.values())


def configure_preference(session: AgentSession, kind: str) -> None:
    """Apply an explicit picker result; cancellation and previews do not mutate state."""
    if kind not in ("model", "effort"):
        raise ValueError(f"unknown preference kind: {kind}")
    effort = session.effort if session.effort in EFFORT_LEVELS else "medium"
    request = PreferenceRequest(kind, model_choices(session), session.model.spec, effort)
    choice = session.ui.choose_preferences(request)
    if choice is None:
        kept = session.model.id if kind == "model" else session.effort
        session.ui.print(f"Kept {kind} as {kept}")
        return
    if not isinstance(choice, PreferenceChoice) or choice.effort not in EFFORT_LEVELS:
        raise ValueError("invalid model or effort selection")
    if choice.model not in {model.spec for model in request.models}:
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
