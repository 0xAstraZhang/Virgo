"""Provider model directory requests, authentication, pagination and cancellation."""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from virgo.ai import catalog
from virgo.ai.auth import AuthStore
from virgo.ai.models import resolve_model


@pytest.fixture
def transport(monkeypatch):
    original = httpx.Client
    requests = []

    def install(handler):
        def serve(request):
            requests.append(request)
            return handler(request)

        monkeypatch.setattr(catalog.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(serve), **kw))
        return requests

    return install


def test_compatible_directory_uses_actual_returned_ids_and_filters_non_chat(transport, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    requests = transport(lambda _: httpx.Response(200, json={"data": [
        {"id": "brand-new-chat", "context_window": 456000, "max_output_tokens": 32000},
        {"id": "brand-new-chat"}, {"id": "text-embedding-3-large"}, {"id": "gpt-image-1"},
        {"id": "hidden-chat", "available": False}, {"id": "retired-chat", "status": "retired"},
        {"id": "bad\nmodel"}, {"id": "voice-only", "output_modalities": ["audio"]},
    ]}))
    models = catalog.fetch_model_catalog(resolve_model("deepseek/old-preset"))
    assert [m.id for m in models] == ["brand-new-chat"]
    assert requests[0].method == "GET" and str(requests[0].url) == "https://api.deepseek.com/models"
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert requests[0].content == b""


def test_anthropic_pagination_saved_key_and_metadata(transport):
    AuthStore().set("anthropic", {"type": "api_key", "key": "saved-key"})

    def handler(request):
        assert request.headers["x-api-key"] == "saved-key"
        assert request.headers["anthropic-version"]
        if "after_id" not in request.url.params:
            return httpx.Response(200, json={"data": [{"id": "claude-new", "max_input_tokens": 900000}], "has_more": True, "last_id": "claude-new"})
        assert request.url.params["after_id"] == "claude-new"
        return httpx.Response(200, json={"data": [{"id": "claude-other", "capabilities": {"thinking": {"supported": True}}}], "has_more": False})

    requests = transport(handler)
    models = catalog.fetch_model_catalog(resolve_model("anthropic/old"))
    assert [m.id for m in models] == ["claude-new", "claude-other"]
    assert models[0].metadata["context_window"] == 900000
    assert models[1].metadata["reasoning"] is True
    assert len(requests) == 2 and requests[0].url.path == "/v1/models"


def test_codex_refreshes_oauth_and_uses_account_directory(transport, monkeypatch):
    AuthStore().set("openai-codex", {
        "type": "oauth", "access": "expired", "refresh": "refresh", "expires": 0,
        "accountId": "account", "connection": {"api": "openai-codex", "baseUrl": "https://chatgpt.com/backend-api/codex"},
    })
    monkeypatch.setattr("virgo.ai.oauth.refresh", lambda *a, **kw: {"access": "new-token", "expires": time.time() + 3600})
    requests = transport(lambda _: httpx.Response(200, json={"models": [
        {"slug": "fresh-codex", "visibility": "list", "context_window": 700000, "supported_reasoning_levels": [{"effort": "high"}]},
        {"slug": "internal", "visibility": "hide"},
    ]}))
    models = catalog.fetch_model_catalog(resolve_model("openai-codex/old"))
    assert [m.id for m in models] == ["fresh-codex"]
    assert models[0].metadata == {"context_window": 700000, "reasoning": True}
    assert requests[0].url.path == "/backend-api/codex/models"
    assert "client_version" in requests[0].url.params
    assert requests[0].headers["authorization"] == "Bearer new-token"
    assert requests[0].headers["chatgpt-account-id"] == "account"


def test_custom_endpoint_and_headers_do_not_use_other_providers_key(transport, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    requests = transport(lambda _: httpx.Response(200, json={"data": [{"id": "local-live"}]}))
    model = resolve_model("local/old", {"local": {"api": "openai", "baseUrl": "http://localhost:8000/v1", "headers": {"x-route": "test"}}})
    assert catalog.fetch_model_catalog(model)[0].id == "local-live"
    assert requests[0].url.path == "/v1/models" and requests[0].headers["x-route"] == "test"
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500])
def test_http_failure_never_echoes_body_or_follows_redirect(transport, status):
    requests = transport(lambda _: httpx.Response(status, json={"error": "secret-token"}, headers={"location": "https://other.example/models"}))
    with pytest.raises(catalog.CatalogError, match=f"HTTP {status}") as exc:
        catalog.fetch_model_catalog(resolve_model("openai/chat"))
    assert "secret-token" not in str(exc.value) and len(requests) == 1


@pytest.mark.parametrize("body", [{}, {"data": None}, {"data": ["bad"]}, {"data": [], "has_more": True}])
def test_bad_directory_does_not_invent_a_fallback(transport, body):
    transport(lambda _: httpx.Response(200, json=body))
    with pytest.raises(catalog.CatalogError):
        catalog.fetch_model_catalog(resolve_model("openai/chat"))


def test_empty_directory_is_valid(transport):
    transport(lambda _: httpx.Response(200, json={"data": []}))
    assert catalog.fetch_model_catalog(resolve_model("openai/chat")) == []


def test_endpoint_change_prevents_sending_saved_credentials(transport):
    AuthStore().set("custom", {"type": "api_key", "key": "secret", "connection": {"api": "openai", "baseUrl": "https://original.example/v1"}})
    requests = transport(lambda _: pytest.fail("changed endpoint must not receive a request"))
    model = resolve_model("custom/chat", {"custom": {"api": "openai", "baseUrl": "https://changed.example/v1"}})
    with pytest.raises(catalog.CatalogError):
        catalog.fetch_model_catalog(model)
    assert not requests


def test_cancel_and_deadline_return_promptly_from_stalled_provider(transport, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def stalled(_):
        entered.set()
        release.wait(2)
        return httpx.Response(200, json={"data": []})

    transport(stalled)
    signal = threading.Event()
    timer = threading.Thread(target=lambda: (entered.wait(1), signal.set()), daemon=True)
    timer.start()
    try:
        start = time.monotonic()
        with pytest.raises(catalog.CatalogCancelled):
            catalog.fetch_model_catalog(resolve_model("openai/chat"), signal)
        assert time.monotonic() - start < 0.5
        monkeypatch.setattr(catalog, "CATALOG_TIMEOUT", 0.1)
        with pytest.raises(catalog.CatalogError, match="timed out"):
            catalog.fetch_model_catalog(resolve_model("openai/chat"))
    finally:
        release.set()


def test_google_account_directory_is_explicitly_unsupported(transport):
    requests = transport(lambda _: pytest.fail("must not guess an account endpoint"))
    with pytest.raises(catalog.CatalogError, match="no supported live model directory"):
        catalog.fetch_model_catalog(resolve_model("google-gemini-cli/old"))
    assert not requests
