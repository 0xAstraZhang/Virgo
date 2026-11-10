"""Preference pickers change runtime state or explicit defaults, never model input."""

from __future__ import annotations

import copy
import json
import time

import pytest

from virgo.ai.catalog import CatalogError, CatalogModel
from virgo.ai.providers import faux
from virgo.harness.preferences import PreferenceChoice, configure_preference, model_choices
from virgo.harness.settings import Settings
from virgo.harness.ui import UI


class PickerUI(UI):
    supports_preferences = True

    def __init__(self, answer=None):
        self.answer = answer
        self.requests = []
        self.lines = []

    def choose_preferences(self, request):
        self.requests.append(request)
        return self.answer(request) if callable(self.answer) else self.answer

    def print(self, text, style=None):
        self.lines.append(text)


@pytest.fixture(autouse=True)
def clean_provider_environment(monkeypatch):
    for name in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
        "XAI_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY", "ZAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def provider_catalogs(monkeypatch):
    responses = {
        "deepseek": [CatalogModel("deepseek-flash", {"context_window": 1000000})],
        "openai-codex": [CatalogModel("gpt-6.1-terra", {"context_window": 500000})],
        "openai": [CatalogModel("chat")],
        "local": [CatalogModel("custom", {"context_window": 64000})],
    }
    monkeypatch.setattr("virgo.harness.preferences.fetch_model_catalog", lambda model, signal=None: responses[model.provider])
    return responses


def test_catalog_only_offers_authenticated_or_explicit_local_models(make_session, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    session = make_session(
        settings={
            "model": "deepseek/deepseek-flash",
            "providers": {
                "local": {
                    "api": "openai",
                    "baseUrl": "http://localhost:8000/v1",
                    "models": {"custom": {"contextWindow": 64000}, "broken": {"contextWindow": "invalid"}},
                },
                "deepseek": {"models": {"deepseek-flash": {"contextWindow": 1000000}}},
            },
        }
    )
    choices = model_choices(session)
    by_spec = {choice.spec: choice for choice in choices}
    assert choices[0].spec == "faux/test"
    assert len(by_spec) == len(choices)
    assert "deepseek/deepseek-flash" in by_spec
    assert "anthropic/claude-opus-5-5" not in by_spec
    assert "openai-codex/gpt-6.1-terra" not in by_spec
    assert by_spec["local/custom"].label == "custom"
    assert by_spec["local/custom"].description == "local · 64,000 context"
    assert "local/broken" not in by_spec


def test_catalog_uses_live_ids_instead_of_current_or_configured_models(make_session, monkeypatch, provider_catalogs):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    provider_catalogs["deepseek"] = [CatalogModel("brand-new-model")]
    session = make_session(settings={"providers": {"deepseek": {"models": {"allowed": {}}}}})
    session.set_model("deepseek/unlisted")
    choices = {model.spec for model in model_choices(session)}
    assert "deepseek/brand-new-model" in choices
    assert "deepseek/allowed" not in choices
    assert "deepseek/unlisted" not in choices
    assert "deepseek/deepseek-flash" not in choices


def test_catalog_prechecks_saved_login_endpoint_expiry_and_metadata(make_session):
    session = make_session()
    credential = {
        "type": "oauth", "access": "test-access", "refresh": "test-refresh",
        "expires": 0, "accountId": "test-account",
        "connection": {"api": "openai-codex", "baseUrl": "https://chatgpt.com/backend-api/codex"},
    }
    session.auth.set("openai-codex", credential)
    assert "openai-codex/gpt-6.1-terra" in {model.spec for model in model_choices(session)}
    for patch in (
        {"refresh": ""},
        {"accountId": ""},
        {"connection": {"api": "openai-codex", "baseUrl": "https://other.example"}},
    ):
        session.auth.set("openai-codex", {**credential, **patch})
        assert "openai-codex/gpt-6.1-terra" not in {model.spec for model in model_choices(session)}
    session.auth.set("openai-codex", {**credential, "refresh": "", "expires": time.time() + 3600})
    assert "openai-codex/gpt-6.1-terra" in {model.spec for model in model_choices(session)}


def test_catalog_refreshes_on_each_open_and_does_not_fallback_after_failure(make_session, monkeypatch, provider_catalogs):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    session = make_session(settings={"providers": {"deepseek": {"models": {"new-model": {"contextWindow": "outdated-invalid"}}}}})
    first = {item.spec for item in model_choices(session)}
    assert "deepseek/deepseek-flash" in first
    provider_catalogs["deepseek"] = [CatalogModel("new-model", {"context_window": 234567, "reasoning": True})]
    second = {item.spec for item in model_choices(session)}
    assert "deepseek/new-model" in second and "deepseek/deepseek-flash" not in second
    resolved = session.resolve("deepseek/new-model")
    assert resolved.context_window == 234567 and resolved.reasoning

    def fail(*_):
        raise CatalogError("Model directory returned HTTP 503.")

    monkeypatch.setattr("virgo.harness.preferences.fetch_model_catalog", fail)
    assert not any(item.spec.startswith("deepseek/") for item in model_choices(session))
    assert not session.discovered_models
    assert "HTTP 503" in session.ui.text


def test_one_provider_failure_does_not_hide_another_live_directory(make_session, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fetch(model, signal=None):
        if model.provider == "deepseek":
            raise CatalogError("Model directory returned HTTP 401.")
        return [CatalogModel("live-chat")]

    monkeypatch.setattr("virgo.harness.preferences.fetch_model_catalog", fetch)
    session = make_session()
    offered = {item.spec for item in model_choices(session)}
    assert "openai/live-chat" in offered
    assert not any(spec.startswith("deepseek/") for spec in offered)


def test_empty_live_directory_explains_missing_provider(make_session, monkeypatch, provider_catalogs):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    provider_catalogs["deepseek"] = []
    session = make_session()
    assert not any(item.spec.startswith("deepseek/") for item in model_choices(session))
    assert "DeepSeek: the live directory returned no selectable models." in session.ui.text


def test_invalid_saved_key_does_not_fall_back_to_environment(make_session, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    session = make_session()
    session.auth.set("deepseek", {"type": "api_key", "key": ""})
    assert not any(model.spec.startswith("deepseek/") for model in model_choices(session))


def test_remote_custom_provider_cannot_borrow_openai_credentials(make_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    session = make_session(settings={"providers": {"custom": {
        "api": "openai", "baseUrl": "https://custom.example/v1", "models": {"chat": {}},
    }}})
    assert not any(model.spec.startswith("custom/") for model in model_choices(session))


def test_catalog_rejects_unsupported_api_and_missing_custom_endpoint(make_session):
    session = make_session(settings={"providers": {
        "unsupported": {"api": "unknown", "apiKey": "test-key", "baseUrl": "https://custom.example", "models": {"chat": {}}},
        "missing-endpoint": {"api": "openai", "apiKey": "test-key", "models": {"chat": {}}},
        "openai-codex": {"api": "openai"},
    }})
    session.auth.set("openai-codex", {
        "type": "oauth", "access": "test-access", "expires": time.time() + 3600, "accountId": "test-account",
    })
    assert {model.spec for model in model_choices(session)} == {"faux/test"}


@pytest.mark.parametrize("config", [
    {"apiKeyEnv": "TEST_CODEX_KEY", "authMetadata": {"accountId": "test-account"}},
    {"apiKey": "test-key", "authMetadata": "invalid"},
])
def test_account_catalog_requires_credentials_the_transport_can_use(make_session, monkeypatch, config):
    monkeypatch.setenv("TEST_CODEX_KEY", "test-key")
    session = make_session(settings={"providers": {"openai-codex": config}})
    assert not any(model.spec.startswith("openai-codex/") for model in model_choices(session))


def test_unavailable_current_model_offers_login_and_effort_remains_editable(make_session):
    ui = PickerUI()
    session = make_session(ui=ui)
    session.set_model("anthropic/claude-opus-5-5")
    session.small_model = session.model
    session.prompt("/model")
    assert not ui.requests and "/login" in ui.lines[-1]
    ui.answer = PreferenceChoice(session.model.spec, "high", False)
    session.prompt("/effort")
    assert session.effort == "high"


@pytest.mark.parametrize("kind", ["model", "effort"])
def test_cancelling_picker_preserves_session_settings_and_history(make_session, kind):
    ui = PickerUI()
    session = make_session(ui=ui)
    settings_before = copy.deepcopy(session.settings.data)
    branch_before = copy.deepcopy(session.store.branch())
    session.prompt(f"/{kind}")
    assert ui.requests[0].kind == kind
    assert ui.requests[0].model == "faux/test"
    assert session.model.spec == "faux/test"
    assert session.effort == "medium"
    assert session.settings.data == settings_before
    assert session.store.branch() == branch_before
    assert not (session.paths.user / "settings.json").exists()
    assert ui.lines[-1].startswith(f"Kept {kind} as ")
    assert not faux.CALLS["test"]


def test_model_session_choice_keeps_defaults_and_custom_provider(make_session):
    ui = PickerUI(PreferenceChoice("local/custom", "high", False))
    session = make_session(
        ui=ui,
        settings={"providers": {"local": {"api": "openai", "baseUrl": "http://localhost:8000/v1", "models": {"custom": {"contextWindow": 64000}}}}},
    )
    settings_before = copy.deepcopy(session.settings.data)
    session.prompt("/model")
    assert session.model.spec == session.main.model.spec == "local/custom"
    assert session.model.context_window == 64000
    assert session.effort == "high"
    assert session.settings.data == settings_before
    assert not (session.paths.user / "settings.json").exists()
    assert not session.main.messages
    assert "this session only" in ui.lines[-1]


def test_saved_model_choice_preserves_other_settings_and_applies_to_future_sessions(make_session, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    ui = PickerUI(PreferenceChoice("deepseek/deepseek-flash", "max", True))
    session = make_session(ui=ui)
    session.settings.save_user({"permissions": {"mode": "plan"}, "custom": {"keep": True}})
    session.prompt("/model")
    data = json.loads((session.paths.user / "settings.json").read_text())
    assert data == {
        "permissions": {"mode": "plan"},
        "custom": {"keep": True},
        "model": "deepseek/deepseek-flash",
        "effort": "max",
    }
    loaded = Settings.load(session.paths)
    assert loaded.get("model") == session.model.spec == "deepseek/deepseek-flash"
    assert loaded.get("effort") == session.effort == "max"
    assert "saved as default" in ui.lines[-1]
    assert not session.main.messages


@pytest.mark.parametrize("persist", [False, True])
def test_effort_choice_only_changes_effort(make_session, persist):
    ui = PickerUI(PreferenceChoice("faux/test", "xhigh", persist))
    session = make_session(ui=ui)
    original_model = session.model
    session.prompt("/effort")
    assert session.effort == "xhigh"
    assert session.model is original_model
    path = session.paths.user / "settings.json"
    if persist:
        assert json.loads(path.read_text()) == {"effort": "xhigh"}
    else:
        assert not path.exists()
        assert session.settings.get("effort") == "medium"
    assert not session.main.messages


@pytest.mark.parametrize(
    "kind,choice",
    [
        ("model", PreferenceChoice("faux/not-offered", "high", True)),
        ("model", PreferenceChoice("faux/test", "ultracode", True)),
        ("effort", PreferenceChoice("deepseek/deepseek-flash", "high", True)),
    ],
)
def test_invalid_picker_choice_cannot_change_settings(make_session, kind, choice):
    session = make_session(ui=PickerUI(choice))
    with pytest.raises(ValueError):
        configure_preference(session, kind)
    assert session.model.spec == "faux/test"
    assert session.effort == "medium"
    assert not (session.paths.user / "settings.json").exists()


def test_failed_default_write_leaves_runtime_unchanged(make_session, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    session = make_session(ui=PickerUI(PreferenceChoice("deepseek/deepseek-flash", "high", True)))

    def fail(_patch):
        raise OSError("read-only settings")

    monkeypatch.setattr(session.settings, "save_user", fail)
    with pytest.raises(OSError, match="read-only"):
        configure_preference(session, "model")
    assert session.model.spec == "faux/test"
    assert session.effort == "medium"


def test_explicit_arguments_and_nonpicker_clients_keep_command_behavior(make_session):
    ui = PickerUI()
    session = make_session(ui=ui)
    session.prompt("/model faux/other")
    session.prompt("/effort high")
    assert session.model.spec == "faux/other"
    assert session.effort == "high"
    assert not ui.requests
    assert not (session.paths.user / "settings.json").exists()
    ui.supports_preferences = False
    session.prompt("/model")
    session.prompt("/effort")
    assert not ui.requests
    assert ui.lines[-1] == "effort: high"
    assert ui.lines[-2].startswith("model: faux/other")


def test_picker_normalizes_unknown_effort_without_changing_cancelled_session(make_session):
    ui = PickerUI()
    session = make_session(ui=ui, settings={"effort": "future-level"})
    session.prompt("/effort")
    assert ui.requests[0].effort == "medium"
    assert ui.requests[0].model == "faux/test"
    assert session.effort == "future-level"
