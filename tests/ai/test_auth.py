"""The credential store: file permissions, refresh, and request-only secrets."""

from __future__ import annotations

import threading
import time

import pytest

from virgo import ai
from virgo.ai.auth import AuthStore, authenticated_model


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
