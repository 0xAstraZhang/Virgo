"""Reasoning capabilities survive model resolution and reach provider requests."""

from __future__ import annotations

import pytest

from virgo.ai.models import resolve_model
from virgo.ai.providers import codex, openai, responses
from virgo.ai.types import Context, StreamOptions, UserMessage


@pytest.mark.parametrize("provider, api, builder", [
    ("openai", "openai", openai.build_params),
    ("openai-codex", "openai-codex", codex.build_params),
    ("custom-openai", "openai-responses", responses.build_params),
])
def test_luna_can_send_low_effort_through_each_openai_transport(provider, api, builder):
    model = resolve_model(f"{provider}/gpt-6-luna", {provider: {"api": api}})
    assert model.reasoning
    assert model.supported_reasoning_levels == ("none", "low", "medium", "high", "xhigh", "max")
    assert model.default_reasoning_level == "medium"
    assert model.context_window == 1_050_000 and model.max_tokens == 128_000
    request = builder(model, Context("review", [UserMessage("check")]), StreamOptions(effort="low"))
    assert request["model"] == "gpt-6-luna"
    assert request["reasoning_effort"] == "low" if api == "openai" else request["reasoning"]["effort"] == "low"


def test_configured_capabilities_enable_reasoning_and_override_known_levels():
    model = resolve_model("openai/gpt-6-luna", {"openai": {"models": {"gpt-6-luna": {
        "supportedReasoningLevels": ["high"], "defaultReasoningLevel": "high",
    }}}})
    assert model.supported_reasoning_levels == ("high",)
    assert model.default_reasoning_level == "high"
    unknown = resolve_model("openai/new-model", {"openai": {"models": {"new-model": {
        "supportedReasoningLevels": ["medium", "high"], "defaultReasoningLevel": "medium",
    }}}})
    assert unknown.reasoning and unknown.supported_reasoning_levels == ("medium", "high")
    assert unknown.default_reasoning_level == "medium"


def test_default_only_capability_enables_reasoning_without_inventing_support():
    model = resolve_model("openai/new-model", {"openai": {"models": {"new-model": {
        "defaultReasoningLevel": "high",
    }}}})
    assert model.reasoning and model.supported_reasoning_levels is None
    assert model.default_reasoning_level == "high"


@pytest.mark.parametrize("metadata", [
    {"supportedReasoningLevels": ["high"]},
    {"supportedReasoningLevels": ["high"], "defaultReasoningLevel": "medium"},
    {"supportedReasoningLevels": []},
])
def test_restricted_capabilities_do_not_retain_an_unsupported_default(metadata):
    model = resolve_model("openai/gpt-6-luna", {"openai": {"models": {"gpt-6-luna": metadata}}})
    assert model.default_reasoning_level is None


def test_effort_mapping_does_not_retain_a_disabled_default():
    model = resolve_model("openai/gpt-6-luna", {"openai": {
        "reasoningEffortMap": {"low": None, "medium": None, "high": "high"},
    }})
    assert model.supported_reasoning_levels == ("high",)
    assert model.default_reasoning_level is None


def test_explicit_reasoning_disable_wins_over_known_capabilities():
    model = resolve_model("openai/gpt-6-luna", {"openai": {"models": {"gpt-6-luna": {"reasoning": False}}}})
    assert not model.reasoning
    assert model.supported_reasoning_levels == () and model.default_reasoning_level is None


def test_unknown_and_explicitly_empty_reasoning_levels_remain_distinct():
    unknown = resolve_model("openai/new-model", {"openai": {"models": {"new-model": {"reasoning": True}}}})
    empty = resolve_model("openai/new-model", {"openai": {"models": {"new-model": {
        "reasoning": True, "supportedReasoningLevels": [],
    }}}})
    assert unknown.supported_reasoning_levels is None
    assert empty.supported_reasoning_levels == ()


def test_provider_effort_mapping_excludes_disabled_or_remapped_levels():
    providers = {"deepseek": {"reasoningEffortMap": {"low": None, "medium": "high", "high": "high"}}}
    model = resolve_model("deepseek/deepseek-flash", providers)
    assert model.reasoning and model.supported_reasoning_levels == ("high",)
    model = resolve_model("deepseek/deepseek-flash", {"deepseek": {
        **providers["deepseek"], "models": {"deepseek-flash": {"supportedReasoningLevels": ["low", "high"]}},
    }})
    assert model.supported_reasoning_levels == ("high",)


@pytest.mark.parametrize("spec", ["anthropic/claude-opus-5-5", "google-gemini-cli/gemini-2.5-pro", "deepseek/deepseek-flash"])
def test_existing_builtin_reasoning_transports_advertise_low(spec):
    assert "low" in resolve_model(spec).supported_reasoning_levels


def test_gpt6_family_is_recognized_as_reasoning_without_inventing_levels():
    model = resolve_model("openai/gpt-6.1-sol")
    assert model.reasoning and model.supported_reasoning_levels is None
