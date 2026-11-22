"""Adapter wire values and the effective reasoning level shown to users."""

from __future__ import annotations

from typing import NamedTuple

from ..models import BUILTIN_PROVIDERS
from ..types import Model

_CHAT_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def _declared(model: Model, effort: str) -> bool:
    return effort in (model.supported_reasoning_levels or ()) or effort == model.default_reasoning_level


def chat_effort(model: Model, effort: str) -> str | None:
    """Compatible Chat APIs may explicitly remap or omit an effort level."""
    if "reasoningEffortMap" in model.options:
        return model.options["reasoningEffortMap"].get(effort)
    if _declared(model, effort):
        return effort
    fallback = BUILTIN_PROVIDERS.get(model.provider, {}).get("reasoningEffortMap", _CHAT_EFFORT)
    return fallback.get(effort)


def responses_effort(model: Model, effort: str) -> str:
    """Responses and Codex preserve declared levels, with the same max fallback."""
    if _declared(model, effort):
        return effort
    return "xhigh" if effort == "max" else effort


class ResolvedEffort(NamedTuple):
    wire: str | None
    effective: str | None


def resolved_effort(model: Model, requested: str | None) -> ResolvedEffort:
    if not model.reasoning or not requested:
        return ResolvedEffort(None, None)
    if model.api == "openai":
        wire = chat_effort(model, requested)
        return ResolvedEffort(wire, model.default_reasoning_level if wire is None else wire)
    if model.api in {"openai-responses", "openai-codex"}:
        wire = responses_effort(model, requested)
    else:
        # Anthropic's output_config.effort and the offline adapter take the raw preference.
        wire = requested
    return ResolvedEffort(wire, wire)
