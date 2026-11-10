from __future__ import annotations

import json
import os
import threading
import time

import pytest

from virgo import ai
from virgo.ai.auth import AuthStore, authenticated_model
from virgo.ai.auth_presets import API_PRESETS, AUTH_PRESETS
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


def test_store_permissions_merge_logout_and_malformed_preservation(tmp_path):
    path = tmp_path / "private" / "auth.json"
    store = AuthStore(path)
    store.set("one", {"type": "api_key", "key": "first-secret"})
    AuthStore(path).set("two", {"type": "api_key", "key": "second-secret"})
    assert set(store.all()) == {"one", "two"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.remove(["one", "missing"]) == ["one"]
    assert store.all()["two"]["key"] == "second-secret"
    path.write_text('{"secret": broken}')
    with pytest.raises(ValueError, match="Invalid credentials file"):
        store.set("three", {"type": "api_key", "key": "third"})
    assert path.read_text() == '{"secret": broken}'


def test_expired_credentials_refresh_once_across_store_instances(tmp_path, monkeypatch):
    from virgo.ai import oauth

    path = tmp_path / "auth.json"
    AuthStore(path).set("xai", {"type": "oauth", "access": "old", "refresh": "refresh", "expires": 0})
    calls = []

    def refresh(provider, credential, signal=None):
        calls.append(provider)
        time.sleep(0.03)
        return {**credential, "access": "new", "refresh": "rotated", "expires": time.time() + 3600}

    monkeypatch.setattr(oauth, "refresh", refresh)
    results = []
    threads = [threading.Thread(target=lambda: results.append(AuthStore(path).credential("xai"))) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == ["xai"]
    assert all(result["access"] == "new" for result in results)
    assert AuthStore(path).all()["xai"]["refresh"] == "rotated"


def test_refresh_failure_keeps_saved_credential(tmp_path, monkeypatch):
    from virgo.ai import oauth

    store = AuthStore(tmp_path / "auth.json")
    old = {"type": "oauth", "access": "old", "refresh": "refresh", "expires": 0}
    store.set("xai", old)

    def fail(*args, **kwargs):
        raise ValueError("Login expired")

    monkeypatch.setattr(oauth, "refresh", fail)
    with pytest.raises(ValueError, match="Login expired"):
        store.credential("xai")
    assert store.all()["xai"] == old


def test_cancelled_refresh_returns_aborted_stream(tmp_path):
    store = AuthStore(tmp_path / "auth.json")
    store.set("xai", {"type": "oauth", "access": "old", "refresh": "refresh", "expires": 0})
    model = ai.resolve_model("xai/grok-4.3")
    model.auth_path = str(store.path)
    signal = threading.Event()
    signal.set()
    message = ai.stream(model, ai.Context("", []), ai.StreamOptions(signal=signal)).result()
    assert message.stop_reason == "aborted"
    assert store.all()["xai"]["access"] == "old"


def test_request_copy_reads_latest_credentials_without_retaining_secrets(tmp_path):
    store = AuthStore(tmp_path / "auth.json")
    model = ai.resolve_model("deepseek/deepseek-flash")
    model.auth_path = str(store.path)
    store.set("deepseek", {"type": "api_key", "key": "key-1"})
    assert authenticated_model(model).options["apiKey"] == "key-1"
    store.set("deepseek", {"type": "api_key", "key": "key-2"})
    assert authenticated_model(model).options["apiKey"] == "key-2"
    store.remove(["deepseek"])
    assert "apiKey" not in authenticated_model(model).options
    assert "apiKey" not in model.options


def test_account_token_cannot_follow_custom_endpoint(tmp_path):
    store = AuthStore(tmp_path / "auth.json")
    store.set("xai", {"type": "oauth", "access": "secret", "refresh": "refresh", "expires": time.time() + 3600})
    model = ai.resolve_model("xai/grok-4.3", {"xai": {"baseUrl": "https://custom.invalid"}})
    model.auth_path = str(store.path)
    with pytest.raises(ValueError, match="official endpoint"):
        authenticated_model(model)


@pytest.mark.parametrize("provider", API_PRESETS)
def test_api_login_persists_switches_and_logout_removes_only_credential(project, provider):
    ui = LoginUI("", "", "private-api-key")
    s = AgentSession(project, ui=ui, session=None, extensions=False)
    try:
        s.prompt(f"/login api {provider}")
        assert s.model.spec == f"{provider}/{API_PRESETS[provider]['model']}"
        assert s.small_model.spec == s.model.spec
        assert ui.inputs[-1] == ("API key", True)
        assert s.auth.all()[provider]["key"] == "private-api-key"
        assert s.main.messages == []
        assert "private-api-key" not in "\n".join(ui.lines)
        assert "private-api-key" not in (s.paths.user / "settings.json").read_text()
        assert "private-api-key" not in repr(s.model)
        again = AgentSession(project, session=None, extensions=False)
        try:
            assert again.model.spec == s.model.spec
            assert authenticated_model(again.model).options["apiKey"] == "private-api-key"
        finally:
            again.shutdown()
        s.prompt(f"/logout {provider}")
        assert s.auth.all() == {}
        assert "apiKey" not in authenticated_model(s.model).options
    finally:
        s.shutdown()


@pytest.mark.parametrize("provider", AUTH_PRESETS)
def test_account_login_dispatches_provider_and_uses_request_only_token(project, provider, monkeypatch):
    from virgo.ai import oauth

    seen = []

    def login(name, ui, signal=None):
        seen.append(name)
        return {"type": "oauth", "access": "account-secret", "refresh": "refresh-secret", "expires": time.time() + 3600}

    monkeypatch.setattr(oauth, "login", login)
    ui = LoginUI()
    s = AgentSession(project, ui=ui, session=None, extensions=False)
    try:
        s.prompt(f"/login auth {provider}")
        assert seen == [provider]
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
    finally:
        s.shutdown()


def test_custom_api_wizard_preserves_other_settings(project):
    ui = LoginUI("API — API 接入", "自定义", "local", "OpenAI compatible", "http://localhost:1234/v1", "model/name", "secret")
    s = AgentSession(project, ui=ui, session=None, extensions=False)
    try:
        s.settings.save_user({"effort": "high", "providers": {"other": {"baseUrl": "https://other.test"}}})
        s.prompt("/login")
        assert s.model.spec == "local/model/name"
        assert s.model.base_url == "http://localhost:1234/v1"
        saved = json.loads((s.paths.user / "settings.json").read_text())
        assert saved["effort"] == "high" and "other" in saved["providers"]
    finally:
        s.shutdown()


def test_custom_anthropic_uses_main_model_and_ignores_legacy_small_model_setting(project):
    ui = LoginUI("local", "Anthropic Messages", "http://localhost:1234", "claude-test", "secret")
    s = AgentSession(project, ui=ui, session=None, extensions=False)
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
    s = AgentSession(project, ui=ui, session=None, extensions=False)
    try:
        s.auth.set("deepseek", {"type": "api_key", "key": "existing"})
        s.prompt("/login api custom")
        assert list(s.auth.all()) == ["deepseek"]
        assert any("Login failed" in line for line in ui.lines)
    finally:
        s.shutdown()


def test_failed_settings_save_rolls_back_existing_login(project, monkeypatch):
    ui = LoginUI("", "", "replacement")
    s = AgentSession(project, ui=ui, session=None, extensions=False)
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
        assert any("Login failed" in line for line in ui.lines)
    finally:
        s.shutdown()


def test_project_endpoint_login_stays_consistent_after_restart(project):
    (project / ".virgo").mkdir()
    (project / ".virgo" / "settings.json").write_text(json.dumps({
        "providers": {"deepseek": {"baseUrl": "http://localhost:1234/v1"}},
    }))
    ui = LoginUI("", "", "secret")
    s = AgentSession(project, ui=ui, session=None, extensions=False)
    try:
        s.prompt("/login api deepseek")
        assert s.model.base_url == "http://localhost:1234/v1"
        again = AgentSession(project, session=None, extensions=False)
        try:
            assert again.model.base_url == s.model.base_url
            assert authenticated_model(again.model).options["apiKey"] == "secret"
        finally:
            again.shutdown()
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
    s = AgentSession(project, ui=ui, session=None, extensions=False)
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
    s = AgentSession(project, session=None, extensions=False)
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
