"""Automatic auxiliary-model selection and model-specific reasoning defaults."""

from __future__ import annotations

import pytest

from virgo.harness.preferences import PreferenceChoice
from virgo.harness.session import AgentSession

from .test_preferences import PickerUI


@pytest.mark.parametrize("main", ["openai/gpt-6-sol", "openai-codex/gpt-6.1-terra"])
def test_openai_uses_luna_on_the_same_connection(project, main):
    session = AgentSession(project, model=main, session=None, extensions=False)
    try:
        assert session.small_model.spec == f"{session.model.provider}/gpt-6-luna"
        assert session.small_model.api == session.model.api
        assert session.small_model.auth_path == session.model.auth_path
        assert session.small_model.base_url == session.model.base_url
        assert session.small_model_effort == "low"
    finally:
        session.shutdown()


@pytest.mark.parametrize("main", [
    "anthropic/claude-opus-5-5", "deepseek/deepseek-flash", "kimi/kimi-k3.1-pro",
    "xai/grok-4.3", "google-gemini-cli/gemini-2.5-pro", "faux/test",
])
def test_other_providers_reuse_main_model(project, main):
    session = AgentSession(
        project, model=main, session=None, extensions=False,
        settings={"smallModel": "unconfigured/legacy-reviewer"},
    )
    try:
        assert session.small_model is session.model
    finally:
        session.shutdown()


@pytest.mark.parametrize(("endpoint", "expected"), [
    ("https://api.openai.com/v1", "gpt-6-luna"),
    ("https://api.example.com/v1", "vendor-chat"),
    ("https://api.openai.com.example.com/v1", "vendor-chat"),
])
def test_custom_connection_identity_not_openai_compatible_protocol(make_session, endpoint, expected):
    session = make_session(settings={"providers": {"custom": {
        "api": "openai", "baseUrl": endpoint, "apiKeyEnv": "CUSTOM_KEY", "headers": {"X-Project": "test"},
    }}})
    session.set_model("custom/vendor-chat")
    assert session.small_model.spec == f"custom/{expected}"
    assert session.small_model.base_url == endpoint
    assert session.small_model.api_key_env == "CUSTOM_KEY"
    assert session.small_model.options["headers"] == {"X-Project": "test"}


def test_direct_model_command_updates_small_model_and_keeps_main_effort(make_session):
    session = make_session()
    session.prompt("/effort max")
    session.prompt("/model openai-codex/gpt-6.1-terra")
    assert session.small_model.spec == "openai-codex/gpt-6-luna"
    assert session.small_model_effort == "low"
    session.prompt("/model anthropic/claude-opus-5-5")
    assert session.small_model is session.model
    assert session.small_model_effort == "low"
    assert session.effort == "max"


@pytest.mark.parametrize("default", ["max", None])
def test_live_luna_reasoning_metadata_overrides_builtin_and_configured_defaults(make_session, default):
    session = make_session(settings={"providers": {"openai": {"models": {"gpt-6-luna": {
        "supportedReasoningLevels": ["low", "medium"], "defaultReasoningLevel": "medium",
    }}}}})
    session.discovered_models["openai/gpt-6-luna"] = {
        "supported_reasoning_levels": ("high", "max"),
    }
    if default is not None:
        session.discovered_models["openai/gpt-6-luna"]["default_reasoning_level"] = default
    session.set_model("openai/gpt-6-sol")
    assert session.small_model.supported_reasoning_levels == ("high", "max")
    assert session.small_model_effort == default


def test_selecting_current_model_refreshes_small_model_metadata(make_session, monkeypatch):
    from virgo.harness import preferences

    ui = PickerUI(PreferenceChoice("faux/test", "max", False))
    session = make_session(ui=ui)

    def refresh(_session):
        session.discovered_models["faux/test"] = {
            "reasoning": True, "supported_reasoning_levels": ("high",), "default_reasoning_level": "high",
        }
        return (preferences.ModelChoice("faux/test", "test", "offline"),)

    monkeypatch.setattr(preferences, "model_choices", refresh)
    session.prompt("/model")
    assert session.small_model is session.model
    assert session.small_model_effort == "high"
    assert session.effort == "max"
