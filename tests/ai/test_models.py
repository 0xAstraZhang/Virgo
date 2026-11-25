"""Model resolution: custom providers and reasoning capabilities that reach provider requests."""

from __future__ import annotations

import pytest

from virgo import ai
from virgo.ai.catalog import CatalogModel
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


@pytest.mark.parametrize("spec", ["anthropic/claude-opus-5-5", "deepseek/deepseek-flash"])
def test_existing_builtin_reasoning_transports_advertise_low(spec):
    assert "low" in resolve_model(spec).supported_reasoning_levels


def test_gpt6_family_is_recognized_as_reasoning_without_inventing_levels():
    model = resolve_model("openai/gpt-6-unknown")
    assert model.reasoning and model.supported_reasoning_levels is None


@pytest.mark.parametrize("model_id,limit,reasoning", [
    ("deepseek-chat", 8_192, False), ("deepseek-reasoner", 64_000, True),
])
def test_bare_deepseek_models_resolve_with_known_limits(model_id, limit, reasoning):
    model = resolve_model(model_id)
    assert model.spec == f"deepseek/{model_id}" and model.api == "openai"
    assert model.context_window == 128_000 and model.max_tokens == limit
    assert model.reasoning is reasoning


def test_model_resolution_and_custom_providers():
    m = resolve_model("claude-sonnet-5-5")
    assert m.provider == "anthropic" and m.reasoning and m.context_window == 1_000_000
    d = resolve_model(
        "deepseek/deepseek-chat",
        {
            "deepseek": {
                "api": "openai",
                "baseUrl": "https://api.deepseek.com",
                "apiKeyEnv": "DEEPSEEK_API_KEY",
                "models": {"deepseek-chat": {"contextWindow": 64000}},
            }
        },
    )
    assert d.api == "openai" and d.base_url == "https://api.deepseek.com" and d.context_window == 64000


def test_retired_google_account_provider_has_a_clear_error():
    with pytest.raises(ValueError, match="Google account login was removed"):
        resolve_model("google-gemini-cli/gemini-2.5-pro")


def test_deepseek_preset_knows_the_default_when_medium_is_omitted():
    model = resolve_model("deepseek/deepseek-flash")
    assert model.supported_reasoning_levels == ("low", "high", "max")
    assert model.default_reasoning_level == "high"
    params = openai.build_params(model, Context("", []), StreamOptions(effort="medium"))
    assert "reasoning_effort" not in params
    assert ai.completion_request_metadata(model, "medium", None)["effective_effort"] is None


@pytest.mark.parametrize("configured_map", [False, True])
def test_live_catalog_effort_reaches_active_model_unless_user_overrides(make_session, configured_map):
    config = {"reasoningEffortMap": {"medium": None, "high": "high"}} if configured_map else {}
    session = make_session(settings={"providers": {"deepseek": config}})
    session.set_model("deepseek/deepseek-flash")
    session.remember_model_metadata(session.model, [CatalogModel("deepseek-flash", {
        "supported_reasoning_levels": ["medium", "high"], "default_reasoning_level": "high",
    })])
    model = session.main.model
    assert model.supported_reasoning_levels == (("high",) if configured_map else ("medium", "high"))
    assert model.default_reasoning_level == "high"
    params = openai.build_params(model, Context("", []), StreamOptions(effort="medium"))
    assert params.get("reasoning_effort") == (None if configured_map else "medium")
    assert ai.completion_request_metadata(model, "medium", None)["effective_effort"] == params.get("reasoning_effort")


@pytest.mark.parametrize("spec", ["deepseek/deepseek-flash", "glm/new-model"])
def test_catalog_default_alone_overrides_preset_effort_fallback(make_session, spec):
    session = make_session()
    session.set_model(spec)
    session.remember_model_metadata(session.model, [CatalogModel(session.model.id, {
        "default_reasoning_level": "medium",
    })])
    model = session.main.model
    assert model.default_reasoning_level == "medium"
    params = openai.build_params(model, Context("", []), StreamOptions(effort="medium"))
    assert params["reasoning_effort"] == "medium"


@pytest.mark.parametrize("model_id, window", [
    ("gpt-6.2-sol", 500_000), ("gpt-6.2-astra", 272_000),
    ("gpt-6.1-sol", 500_000), ("gpt-6-sol", 272_000),
])
def test_codex_known_limits_and_effort(model_id, window):
    model = resolve_model(f"openai-codex/{model_id}")
    assert (model.context_window, model.max_tokens) == (window, 128_000)
    assert model.reasoning and model.supported_reasoning_levels == ("low", "medium", "high", "xhigh")
    assert model.default_reasoning_level == "medium"
