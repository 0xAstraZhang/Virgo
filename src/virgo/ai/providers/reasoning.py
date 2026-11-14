"""Reasoning effort policies shared by requests and their diagnostic metadata."""

from __future__ import annotations

from ..types import Model

_CHAT_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def _declared(model: Model, effort: str) -> bool:
    return effort in (model.supported_reasoning_levels or ()) or effort == model.default_reasoning_level


def chat_effort(model: Model, effort: str) -> str | None:
    """Compatible Chat APIs may explicitly remap or omit an effort level."""
    if "reasoningEffortMap" in model.options:
        return model.options["reasoningEffortMap"].get(effort)
    return effort if _declared(model, effort) else _CHAT_EFFORT.get(effort)


def responses_effort(model: Model, effort: str) -> str:
    """Responses and Codex preserve declared levels, with the same max fallback."""
    if _declared(model, effort):
        return effort
    return "xhigh" if effort == "max" else effort
