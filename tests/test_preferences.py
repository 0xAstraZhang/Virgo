"""Preference pickers change runtime state or explicit defaults, never model input."""

from __future__ import annotations

import copy
import json

import pytest

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


def test_catalog_includes_current_configured_and_builtin_models_without_duplicates(make_session):
    session = make_session(
        settings={
            "model": "deepseek/deepseek-flash",
            "providers": {
                "local": {
                    "api": "openai",
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
    assert "anthropic/claude-opus-5-5" in by_spec
    assert "openai-codex/gpt-6.1-terra" in by_spec
    assert by_spec["local/custom"].label == "custom"
    assert by_spec["local/custom"].description == "local · 64,000 context"
    assert "local/broken" not in by_spec


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
        settings={"providers": {"local": {"api": "openai", "models": {"custom": {"contextWindow": 64000}}}}},
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


def test_saved_model_choice_preserves_other_settings_and_applies_to_future_sessions(make_session):
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
    assert any(model.spec == "faux/test" for model in ui.requests[0].models)
    assert session.effort == "future-level"
