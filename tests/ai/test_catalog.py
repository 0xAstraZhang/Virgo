"""Provider model directory requests, authentication, pagination and cancellation."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from virgo.ai import catalog
from virgo.ai.auth import AuthStore
from virgo.ai.catalog_versions import CodexCatalogVersion
from virgo.ai.models import resolve_model


@pytest.fixture(autouse=True)
def codex_version_sources(monkeypatch):
    monkeypatch.setattr("virgo.ai.catalog_versions.installed_codex_version", lambda: "0.167.0")
    monkeypatch.setattr(CodexCatalogVersion, "_published_version", lambda _: "0.167.0")


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


def test_catalog_reads_provider_dates_and_ignores_invalid_timestamps(transport):
    transport(lambda _: httpx.Response(200, json={"data": [
        {"id": "unix", "created": 1735689600},
        {"id": "iso", "created_at": "2025-01-01T08:00:00+08:00"},
        {"id": "release", "release_date": "2025-01-01", "created": 100},
        {"id": "invalid", "created_at": "not-a-date", "created": True},
        {"id": "undated"},
        {"id": "fallback", "released_at": "invalid", "created": 100},
    ]}))
    models = catalog.fetch_model_catalog(resolve_model("openai/current"))
    assert [model.released_at for model in models] == [1735689600, 1735689600, 1735689600, None, None, 100]
    assert [model.id for model in models] == ["unix", "iso", "release", "invalid", "undated", "fallback"]


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
    def directory(request):
        # The real account directory returns an empty list for Virgo's 0.1.0 version.
        if request.url.params.get("client_version") == "0.1.0":
            return httpx.Response(200, json={"models": []})
        assert request.url.params["client_version"] == "0.167.0"
        return httpx.Response(200, json={"models": [
            {"slug": "fresh-codex", "visibility": "list", "context_window": 700000,
             "supported_reasoning_levels": [{"effort": "high"}], "default_reasoning_level": "high"},
            {"slug": "internal", "visibility": "hide"},
        ]})

    requests = transport(directory)
    models = catalog.fetch_model_catalog(resolve_model("openai-codex/old"))
    assert [m.id for m in models] == ["fresh-codex"]
    assert models[0].metadata == {
        "context_window": 700000,
        "reasoning": True,
        "supported_reasoning_levels": ("high",),
        "default_reasoning_level": "high",
    }
    assert requests[0].url.path == "/backend-api/codex/models"
    assert "client_version" in requests[0].url.params
    assert requests[0].headers["authorization"] == "Bearer new-token"
    assert requests[0].headers["chatgpt-account-id"] == "account"


def test_directory_preserves_reasoning_capabilities_and_unknown_defaults(transport):
    transport(lambda _: httpx.Response(200, json={"data": [
        {"id": "unknown", "reasoning": True},
        {"id": "listed", "reasoning": ["low", "high"], "default_reasoning_level": "high"},
        {"id": "empty", "supported_reasoning_levels": [], "reasoning": True},
        {"id": "default-only", "default_reasoning_level": "medium"},
        {"id": "disabled", "supported_reasoning_levels": [{"effort": "low"}],
         "default_reasoning_level": "low", "capabilities": {"thinking": {"supported": False}}},
        {"id": "invalid", "supported_reasoning_levels": [None, {"effort": False}, 3],
         "default_reasoning_level": False},
    ]}))
    metadata = {model.id: model.metadata for model in catalog.fetch_model_catalog(resolve_model("openai/current"))}
    assert metadata == {
        "unknown": {"reasoning": True},
        "listed": {"reasoning": True, "supported_reasoning_levels": ("low", "high"),
                   "default_reasoning_level": "high"},
        "empty": {"reasoning": False, "supported_reasoning_levels": ()},
        "default-only": {"reasoning": True, "default_reasoning_level": "medium"},
        "disabled": {"reasoning": False, "supported_reasoning_levels": ()},
        "invalid": {},
    }


def test_codex_catalog_version_can_be_overridden_per_connection(transport):
    model = resolve_model("openai-codex/old", {"openai-codex": {
        "apiKey": "token", "authMetadata": {"accountId": "account"}, "clientVersion": "1.2.3",
    }})
    requests = transport(lambda _: httpx.Response(200, json={"models": []}))
    assert catalog.fetch_model_catalog(model) == []
    assert requests[0].url.params["client_version"] == "1.2.3"


@pytest.mark.parametrize("status", [200, 400, 426])
def test_codex_refreshes_stale_version_once_and_remembers_success(transport, monkeypatch, status):
    monkeypatch.setattr("virgo.ai.catalog_versions.installed_codex_version", lambda: None)
    updates = []
    monkeypatch.setattr(CodexCatalogVersion, "_published_version", lambda _: updates.append(True) or "0.200.0")
    model = resolve_model("openai-codex/old", {"openai-codex": {"apiKey": "token", "authMetadata": {"accountId": "account"}}})
    state = CodexCatalogVersion(model)
    state.path.parent.mkdir(parents=True)
    state.path.write_text(json.dumps({"published": "0.100.0", "checked_at": time.time()}))

    def directory(request):
        if request.url.params["client_version"] == "0.100.0":
            return httpx.Response(status, json={"models": []})
        assert request.url.params["client_version"] == "0.200.0"
        return httpx.Response(200, json={"models": [{"slug": "live-model", "visibility": "list"}]})

    requests = transport(directory)
    assert [m.id for m in catalog.fetch_model_catalog(model)] == ["live-model"]
    assert len(requests) == 2 and len(updates) == 1
    assert json.loads(state.path.read_text())["verified"] == "0.200.0"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_codex_does_not_retry_auth_or_service_failures_with_other_versions(transport, monkeypatch, status):
    checks = []
    monkeypatch.setattr(CodexCatalogVersion, "_published_version", lambda _: checks.append(True) or "0.200.0")
    model = resolve_model("openai-codex/old", {"openai-codex": {"apiKey": "token", "authMetadata": {"accountId": "account"}}})
    requests = transport(lambda _: httpx.Response(status))
    with pytest.raises(catalog.CatalogError, match=f"HTTP {status}"):
        catalog.fetch_model_catalog(model)
    assert len(requests) == 1 and len(checks) == 1


def test_codex_empty_latest_directory_has_bounded_retry_and_actionable_error(transport):
    model = resolve_model("openai-codex/old", {"openai-codex": {"apiKey": "token", "authMetadata": {"accountId": "account"}}})
    requests = transport(lambda _: httpx.Response(200, json={"models": []}))
    with pytest.raises(catalog.CatalogError, match="Automatic version refresh found no alternative"):
        catalog.fetch_model_catalog(model)
    assert len(requests) == 1
    assert "verified" not in CodexCatalogVersion(model).cache


def test_codex_keeps_last_verified_version_when_new_release_is_rejected(transport, monkeypatch):
    monkeypatch.setattr("virgo.ai.catalog_versions.installed_codex_version", lambda: None)
    monkeypatch.setattr(CodexCatalogVersion, "_published_version", lambda _: "0.300.0")
    model = resolve_model("openai-codex/old", {"openai-codex": {"apiKey": "token", "authMetadata": {"accountId": "account"}}})
    state = CodexCatalogVersion(model)
    state.path.parent.mkdir(parents=True)
    state.path.write_text(json.dumps({"verified": "0.200.0"}))

    def directory(request):
        if request.url.params["client_version"] == "0.300.0":
            return httpx.Response(400)
        return httpx.Response(200, json={"models": [{"slug": "working-model"}]})

    requests = transport(directory)
    assert catalog.fetch_model_catalog(model)[0].id == "working-model"
    assert len(requests) == 2
    assert json.loads(state.path.read_text())["verified"] == "0.200.0"


def test_anthropic_catalog_tracks_sdk_version_and_allows_explicit_override(transport, monkeypatch):
    class SDK:
        def __init__(self, **kwargs):
            assert kwargs == {"api_key": "catalog-version-only"}
            self.default_headers = {"anthropic-version": "2026-10-21"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr("anthropic.Anthropic", SDK)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    requests = transport(lambda _: httpx.Response(200, json={"data": []}))
    model = resolve_model("anthropic/current")
    catalog.fetch_model_catalog(model)
    assert requests[-1].headers["anthropic-version"] == "2026-10-21"
    model.options["apiVersion"] = "2023-06-01"
    catalog.fetch_model_catalog(model)
    assert requests[-1].headers["anthropic-version"] == "2023-06-01"


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
