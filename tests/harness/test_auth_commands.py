"""/login and /logout save, switch, and remove credentials without touching other settings."""

from __future__ import annotations

import copy
import json
import os
import time

import pytest

from virgo.ai.auth import authenticated_model
from virgo.ai.auth_presets import API_PRESETS, AUTH_PRESETS
from virgo.ai.catalog import CatalogModel
from virgo.ai.models import BUILTIN_PROVIDERS
from virgo.execution_policy import default_execution_mode
from virgo.harness.preferences import model_choices
from virgo.harness.session import AgentSession
from virgo.harness.ui import UI


class LoginUI(UI):
    interactive = True

    def __init__(self, *answers):
        self.answers = iter(answers)
        self.lines = []
        self.inputs = []

    def select(self, question, options):
        answer = next(self.answers)
        assert answer is None or answer in options
        return answer

    def input(self, question, *, password=False, default=""):
        self.inputs.append((question, password))
        answer = next(self.answers)
        return default if answer == "" else answer

    def print(self, text, style=None):
        self.lines.append(text)


@pytest.fixture(autouse=True)
def _no_live_provider_directory(request, monkeypatch):
    """Avoid real public-internet / dead-port directory fetches except in the loopback directory test."""
    if request.node.originalname != "test_login_fetches_the_provider_directory_once":
        monkeypatch.setattr("virgo.harness.auth_commands.refresh_provider_catalog", lambda session, provider: None)


def _assert_authenticated_after_restart(session, endpoint, key):
    again = AgentSession(session.cwd, session=None, extensions=False, refresh_models=False,
                         trust_project_extensions=session.trust_project_extensions,
                         shell_catalog=session.services["shell"].catalog)
    try:
        assert again.model.spec == session.model.spec
        for current in (session, again):
            request = authenticated_model(current.model)
            assert request.base_url == endpoint
            assert request.options["apiKey"] == key
    finally:
        again.shutdown()


@pytest.mark.parametrize("provider", API_PRESETS)
def test_api_login_persists_switches_and_logout_removes_only_credential(project, provider):
    ui = LoginUI("", "", "private-api-key")
    s = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
    try:
        s.prompt(f"/login api {provider}")
        assert s.model.spec == f"{provider}/{API_PRESETS[provider]['model']}"
        assert s.small_model.spec == s.model.spec
        assert ui.inputs[-1] == ("API key", True)
        assert s.auth.all()[provider]["key"] == "private-api-key"
        assert s.main.messages == []
        assert "private-api-key" not in "\n".join(ui.lines)
        assert "private-api-key" not in repr(s.model)
        s.remember_model_metadata(s.model, [CatalogModel(s.model.id, {"context_window": 120_000, "max_tokens": 4096})])
        assert (s.model.context_window, s.model.max_tokens) == (120_000, 4096)
        assert json.loads((s.paths.user / "settings.json").read_text()) == {
            "execution": {"mode": default_execution_mode()},
            "model": s.model.spec,
        }
        _assert_authenticated_after_restart(s, API_PRESETS[provider]["config"]["baseUrl"], "private-api-key")
        s.prompt(f"/logout {provider}")
        assert s.auth.all() == {}
        assert "apiKey" not in authenticated_model(s.model).options
    finally:
        s.shutdown()


@pytest.fixture
def account_login(monkeypatch):
    from virgo.ai import oauth

    seen = []

    def login(name, ui, signal=None):
        seen.append(name)
        return {"type": "oauth", "access": "account-secret", "refresh": "refresh-secret", "expires": time.time() + 3600}

    monkeypatch.setattr(oauth, "login", login)
    return seen


@pytest.mark.parametrize("provider", AUTH_PRESETS)
def test_account_login_dispatches_provider_and_uses_request_only_token(project, provider, account_login, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://environment.invalid")
    ui = LoginUI()
    s = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
    try:
        s.prompt(f"/login auth {provider}")
        assert account_login == [provider]
        assert s.model.spec == f"{provider}/{AUTH_PRESETS[provider]['model']}"
        small_id = {"openai-codex": "gpt-6-luna", "anthropic": "claude-haiku-5-5"}.get(provider)
        expected_small = f"{provider}/{small_id}" if small_id else s.model.spec
        assert s.small_model.spec == expected_small
        request = authenticated_model(s.model)
        assert request.options["apiKey"] == "account-secret"
        assert request.options["authType"] == "oauth"
        assert authenticated_model(s.small_model).options["apiKey"] == "account-secret"
        assert "account-secret" not in repr(s.model)
        assert "refresh-secret" not in repr(request)
        expected = {"execution": {"mode": default_execution_mode()}, "model": s.model.spec}
        if provider == "anthropic":
            expected["providers"] = {"anthropic": {"baseUrl": "https://api.anthropic.com"}}
        assert json.loads((s.paths.user / "settings.json").read_text()) == expected
        _assert_authenticated_after_restart(s, AUTH_PRESETS[provider]["config"]["baseUrl"], "account-secret")
    finally:
        s.shutdown()


def test_custom_api_wizard_preserves_other_settings(project):
    ui = LoginUI("API — Connect with an API key", "Custom", "local", "OpenAI compatible", "http://localhost:1234/v1", "model/name", "secret")
    s = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
    try:
        s.settings.save_user({"effort": "high", "providers": {"other": {"baseUrl": "https://other.test"}}})
        s.prompt("/login")
        assert s.model.spec == "local/model/name"
        assert s.model.base_url == "http://localhost:1234/v1"
        saved = json.loads((s.paths.user / "settings.json").read_text())
        assert saved["effort"] == "high" and "other" in saved["providers"]
        assert saved["model"] == "local/model/name"
        assert saved["providers"]["local"] == {
            "api": "openai", "baseUrl": "http://localhost:1234/v1", "apiKeyEnv": "VIRGO_LOCAL_API_KEY",
        }
    finally:
        s.shutdown()


def test_custom_anthropic_uses_main_model_and_ignores_legacy_small_model_setting(project):
    ui = LoginUI("local", "Anthropic Messages", "http://localhost:1234", "claude-test", "secret")
    s = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
    try:
        s.prompt("/login api custom")
        assert s.model.spec == "local/claude-test"
        assert s.small_model.spec == s.model.spec
        s.settings.set("smallModel", "other/summary")
        s.set_model(s.model.spec)
        assert s.small_model is s.model
        assert authenticated_model(s.small_model).options["apiKey"] == "secret"
    finally:
        s.shutdown()


def test_custom_provider_cannot_use_logout_all_keyword(project):
    ui = LoginUI("all")
    s = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
    try:
        s.auth.set("deepseek", {"type": "api_key", "key": "existing"})
        s.prompt("/login api custom")
        assert list(s.auth.all()) == ["deepseek"]
        assert any("Login failed" in line for line in ui.lines)
    finally:
        s.shutdown()


def test_failed_settings_save_rolls_back_existing_login(project, monkeypatch):
    ui = LoginUI("", "", "replacement")
    s = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
    try:
        previous_model = s.model.spec
        previous = {"type": "api_key", "key": "original"}
        s.auth.set("deepseek", previous)

        def fail(patch):
            raise OSError("Cannot save settings")

        monkeypatch.setattr(s.settings, "save_user", fail)
        s.prompt("/login api deepseek")
        assert s.auth.all()["deepseek"] == previous
        assert s.model.spec == previous_model
        assert any("Cannot save settings" in line for line in ui.lines)
    finally:
        s.shutdown()


def test_api_login_resets_custom_endpoint_and_rolls_back_a_failed_reset(project, monkeypatch):
    custom = "http://localhost:1234/v1"
    official = "https://api.deepseek.com"
    ui = LoginUI(custom, "", "original", official + "/", "", "replacement", official, "", "replacement")
    s = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
    try:
        s.prompt("/login api deepseek")
        path = s.paths.user / "settings.json"
        assert json.loads(path.read_text()) == {
            "execution": {"mode": default_execution_mode()},
            "model": s.model.spec,
            "providers": {"deepseek": {"baseUrl": custom}},
        }
        _assert_authenticated_after_restart(s, custom, "original")
        previous_auth = s.auth.all()
        previous_settings = path.read_bytes()
        previous_data = copy.deepcopy(s.settings.data)
        replace = os.replace

        def fail_settings_write(src, dst):
            if dst == path:
                raise OSError("Cannot replace settings")
            return replace(src, dst)

        with monkeypatch.context() as m:
            m.setattr(os, "replace", fail_settings_write)
            s.prompt("/login api deepseek")
        assert any("Cannot replace settings" in line for line in ui.lines)
        assert s.auth.all() == previous_auth
        assert path.read_bytes() == previous_settings
        assert s.settings.data == previous_data
        assert authenticated_model(s.model).base_url == custom

        s.prompt("/login api deepseek")
        assert json.loads(path.read_text()) == {
            "execution": {"mode": default_execution_mode()},
            "model": s.model.spec,
        }
        assert "deepseek" not in s.settings.get("providers", {})
        _assert_authenticated_after_restart(s, official, "replacement")
    finally:
        s.shutdown()


def test_login_compares_protocol_and_normalized_endpoint_to_builtin(project, monkeypatch):
    config = API_PRESETS["deepseek"]["config"]
    monkeypatch.setitem(BUILTIN_PROVIDERS, "deepseek", {
        **config, "api": "openai-responses", "baseUrl": config["baseUrl"] + "/",
    })
    ui = LoginUI("", "", "secret")
    s = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
    try:
        # A user edit made since startup must also be cleared when choosing the default.
        path = s.paths.user / "settings.json"
        path.write_text(json.dumps({"providers": {"deepseek": {"baseUrl": "https://old.invalid"}}}))
        s.prompt("/login api deepseek")
        assert json.loads(path.read_text())["providers"] == {"deepseek": {"api": "openai"}}
        assert s.auth.all()["deepseek"]["connection"] == {"api": "openai", "baseUrl": config["baseUrl"]}
        _assert_authenticated_after_restart(s, config["baseUrl"] + "/", "secret")
    finally:
        s.shutdown()


@pytest.mark.parametrize("mode,provider", [("api", "deepseek"), ("auth", "xai")])
def test_relogin_preserves_manual_settings_and_resets_conflicting_connection(project, mode, provider, account_login):
    preset = (API_PRESETS if mode == "api" else AUTH_PRESETS)[provider]
    endpoint = preset["config"]["baseUrl"]
    ui = LoginUI("", "", "original", endpoint, "", "replacement")
    s = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
    try:
        s.prompt(f"/login {mode} {provider}")
        manual = {
            "models": {preset["model"]: {"contextWindow": 55_555}},
            "reasoningEffortMap": {"medium": "medium"},
            "headers": {"X-Example": "preserve"},
            "apiKeyEnv": "MANUAL_API_KEY",
            "sendReasoning": False,
        }
        s.settings.save_user({"providers": {provider: {**manual, "api": "anthropic", "baseUrl": "https://old.invalid"}}})
        s.prompt(f"/login {mode} {provider}")
        assert json.loads((s.paths.user / "settings.json").read_text())["providers"] == {provider: manual}
        assert s.settings.get(f"providers.{provider}") == manual
        assert s.model.context_window == 55_555
        _assert_authenticated_after_restart(s, endpoint, "replacement" if mode == "api" else "account-secret")
    finally:
        s.shutdown()


def test_project_endpoint_login_stays_consistent_after_restart(project):
    (project / ".virgo").mkdir()
    (project / ".virgo" / "settings.json").write_text(json.dumps({
        "providers": {"deepseek": {"baseUrl": "http://localhost:1234/v1", "apiKeyEnv": "PROJECT_API_KEY"}},
    }))
    ui = LoginUI("", "", "secret")
    s = AgentSession(project, ui=ui, session=None, extensions=False, trust_project_extensions=True, refresh_models=False)
    try:
        s.prompt("/login api deepseek")
        assert s.model.api_key_env == "PROJECT_API_KEY"
        assert json.loads((s.paths.user / "settings.json").read_text())["providers"] == {
            "deepseek": {"baseUrl": "http://localhost:1234/v1"},
        }
        _assert_authenticated_after_restart(s, "http://localhost:1234/v1", "secret")
        # Explicitly selecting a conflicting endpoint fails before asking for a key.
        s.ui = ui = LoginUI("https://api.deepseek.com", "")
        s.commands.get("login").fn(s, "api deepseek")
        assert len(ui.inputs) == 2
        assert any("override deepseek.baseUrl" in line for line in ui.lines)
        assert s.auth.all()["deepseek"]["key"] == "secret"
        s.model.base_url = "https://another.invalid/v1"
        with pytest.raises(ValueError, match="endpoint.*changed"):
            authenticated_model(s.model)
    finally:
        s.shutdown()


def test_cancellation_and_invalid_input_do_not_save(project):
    ui = LoginUI("", "", None)
    s = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
    try:
        initial = s.model.spec
        s.prompt("/login api deepseek")
        assert s.auth.all() == {} and s.model.spec == initial
        assert "Login cancelled." in ui.lines
        s.prompt("/login api deepseek secret-on-command-line")
        assert "secret-on-command-line" not in "\n".join(ui.lines)
        assert s.auth.all() == {}
    finally:
        s.shutdown()


def test_logout_all_keeps_environment_and_headless_login_does_not_prompt(project, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "shell-secret")
    s = AgentSession(project, session=None, extensions=False, refresh_models=False)
    try:
        s.prompt("/login")
        assert s.auth.all() == {}
        s.auth.set("deepseek", {"type": "api_key", "key": "saved"})
        s.auth.set("kimi", {"type": "api_key", "key": "saved"})
        s.prompt("/logout all")
        assert s.auth.all() == {}
        assert os.environ["DEEPSEEK_API_KEY"] == "shell-secret"
        assert not s.commands.get("login").concurrent
        assert not s.commands.get("logout").concurrent
    finally:
        s.shutdown()


def _directory_server(state):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state["paths"].append(self.path)
            body = json.dumps(state["payload"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


@pytest.mark.parametrize(
    "status, expected_warning",
    [(200, None), (500, "could not read the model directory")],
    ids=["success", "failure"],
)
def test_login_fetches_the_provider_directory_once(project, monkeypatch, status, expected_warning):
    from tests.support.http import bypass_proxies, serve

    bypass_proxies(monkeypatch)
    state = {"paths": [], "status": status,
             "payload": {"data": [{"id": "fresh-model", "context_length": 64000}]}}
    with serve(_directory_server(state)) as url:
        ui = LoginUI("API — Connect with an API key", "Custom", "local", "OpenAI compatible",
                     f"{url}/v1", "seed-model", "secret")
        s = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
        try:
            s.prompt("/login")
            assert s.model.spec == "local/seed-model"
            assert state["paths"] == ["/v1/models"]
            offered = {choice.spec for choice in model_choices(s)}
            if status == 200:
                assert "local/fresh-model" in offered
                assert any("1 models available" in line for line in ui.lines)
            else:
                assert "local/fresh-model" not in offered
                assert any(expected_warning in line for line in ui.lines)
        finally:
            s.shutdown()
