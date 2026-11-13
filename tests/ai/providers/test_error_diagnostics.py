"""Provider failures keep useful, credential-free evidence at their capture point."""

from __future__ import annotations

import importlib
import json
import threading

import httpx
import pytest

from virgo.ai import compaction
from virgo.ai.providers import anthropic, codex, google, openai, responses
from virgo.ai.providers._errors import attach_error
from virgo.ai.types import AssistantMessage, Context, Model, StreamOptions, UserMessage, message_to_dict

from tests.support.anthropic import ANTHROPIC_EVENTS
from tests.support.http import sse

ADAPTERS = [
    (anthropic, "anthropic"), (openai, "openai"), (responses, "openai-responses"),
    (codex, "openai-codex"), (google, "google-gemini-cli"),
]
TOKEN = "provider-private-credential-12345"


def model_for(api, url=None):
    return Model(
        api, "test-model", api, base_url=url,
        options={"apiKey": TOKEN, "authMetadata": {"accountId": "test-account", "projectId": "test-project"}},
    )


@pytest.mark.parametrize("adapter,api", ADAPTERS)
@pytest.mark.parametrize("status", [429, 500])
def test_http_failure_has_correlation_without_response_payload(server, adapter, api, status):
    server["status"] = status
    server["response_headers"] = {"x-request-id": "req-diagnostic-123", "retry-after": "2"}
    server["body"] = json.dumps({"error": {
        "type": "server_error", "code": "server_error", "message": f"Overloaded: {TOKEN}",
        "debug_payload": "response-body-must-not-be-logged",
    }}).encode()
    context = Context("private-system-must-not-be-logged", [UserMessage("private-input-must-not-be-logged")])

    result = adapter.stream(model_for(api, server["url"]), context, StreamOptions()).result()

    assert result.stop_reason == "error" and result.error_kind == "retryable"
    details = result.error_details
    assert details["error_id"] and details["phase"] == "provider.stream"
    assert details["exception_type"] and details["traceback"]
    http = details["context"]["http"]
    assert http["status_code"] == status and http["request_id"] == "req-diagnostic-123"
    assert http["retry_after"] == "2" and http["endpoint"].startswith(server["url"])
    assert "?" not in http["endpoint"]
    encoded = json.dumps(message_to_dict(result))
    assert all(secret not in encoded for secret in (
        TOKEN, "response-body-must-not-be-logged", "private-system-must-not-be-logged",
        "private-input-must-not-be-logged",
    ))
    assert "[redacted]" in result.error_message


@pytest.mark.parametrize("adapter,api", ADAPTERS)
def test_network_failure_retains_original_cause(monkeypatch, adapter, api):
    model = model_for(api)
    request = httpx.Request("POST", f"https://provider.example/v1/messages?credential={TOKEN}")

    def fail(*args, **kwargs):
        try:
            raise OSError("DNS lookup failed at transport")
        except OSError as cause:
            if api in {"openai", "openai-responses"}:
                import openai as sdk

                raise sdk.APIConnectionError(request=request) from cause
            if api == "anthropic":
                import anthropic as sdk

                raise sdk.APIConnectionError(request=request) from cause
            raise httpx.ConnectError("Provider connection failed", request=request) from cause

    monkeypatch.setattr(adapter, "json_events" if adapter in {codex, google} else "_client", fail)
    result = adapter.stream(model, Context("", [UserMessage("hello")]), StreamOptions()).result()

    assert result.stop_reason == "error" and result.error_kind == "retryable"
    encoded = json.dumps(result.error_details)
    assert "DNS lookup failed at transport" in encoded and "fail" in result.error_details["traceback"]
    assert "ConnectionError" in result.error_details["exception_type"] or "ConnectError" in result.error_details["exception_type"]
    assert result.error_details["context"]["http"]["endpoint"] == "https://provider.example/v1/messages"
    assert TOKEN not in encoded


@pytest.mark.parametrize("adapter,api", [(openai, "openai"), (anthropic, "anthropic")])
def test_clean_eof_without_provider_terminal_is_a_failure(server, adapter, api):
    if api == "anthropic":
        server["body"] = sse(ANTHROPIC_EVENTS[:7], named=True)
    else:
        server["body"] = sse([{
            "id": "chat-1", "object": "chat.completion.chunk", "created": 1, "model": "test",
            "choices": [{"index": 0, "delta": {"content": "Partial"}, "finish_reason": None}],
        }], done=False)
    result = adapter.stream(model_for(api, server["url"]), Context("", [UserMessage("hi")]), StreamOptions()).result()
    assert result.stop_reason == "error" and result.error_kind == "retryable"
    assert result.text() and "ended before" in result.error_message
    assert result.error_details["phase"] == "provider.stream"


def test_malformed_sse_keeps_request_and_parser_cause(server):
    server["response_headers"] = {"x-request-id": "req-malformed"}
    server["body"] = b"data: {broken\n\n"
    result = codex.stream(model_for("openai-codex", server["url"]), Context("", []), StreamOptions()).result()
    assert result.stop_reason == "error"
    assert result.error_details["context"]["http"]["request_id"] == "req-malformed"
    assert "JSONDecodeError" in json.dumps(result.error_details["cause_chain"])


@pytest.mark.parametrize("adapter,api", ADAPTERS)
def test_user_cancellation_is_not_reported_as_fatal(adapter, api):
    signal = threading.Event()
    signal.set()
    result = adapter.stream(model_for(api), Context("", []), StreamOptions(signal=signal)).result()
    assert result.stop_reason == "aborted"
    assert result.error_details is None and result.error_kind != "fatal"


def test_compaction_setup_exception_keeps_diagnostics(monkeypatch):
    model = model_for("openai")

    def fail(*args, **kwargs):
        raise ValueError(f"Invalid authenticated configuration {TOKEN}")

    monkeypatch.setattr("virgo.ai.auth.authenticated_model", fail)
    result = compaction.compact(model, Context("private-system", []))
    assert result.stop_reason == "error"
    assert result.error_details["phase"] == "provider.compact.setup"
    assert "ValueError" in result.error_details["exception_type"]
    assert TOKEN not in json.dumps(message_to_dict(result))


@pytest.mark.parametrize("transport_name", ["httpx", "httpx2"])
@pytest.mark.parametrize("with_response", [False, True])
def test_timeout_without_attached_request_preserves_original_failure(transport_name, with_response):
    transport = importlib.import_module(transport_name)
    response = transport.Response(503, headers={"x-request-id": "req-no-request"}) if with_response else None
    result = AssistantMessage(stop_reason="error", error_kind="retryable")
    try:
        try:
            raise TimeoutError("upstream socket stalled")
        except TimeoutError as cause:
            raise transport.ReadTimeout("response body read timed out") from cause
    except transport.ReadTimeout as error:
        attach_error(result, error, model_for("openai"), response=response)

    assert result.error_details["exception_type"].endswith("ReadTimeout")
    assert "upstream socket stalled" in json.dumps(result.error_details["cause_chain"])
    assert "request property" not in json.dumps(result.error_details)
    assert result.error_message == "response body read timed out"
    if with_response:
        assert result.error_details["context"]["http"] == {"status_code": 503, "request_id": "req-no-request"}


@pytest.mark.parametrize("adapter,api,transport_name", [
    (anthropic, "anthropic", "httpx2"), (codex, "openai-codex", "httpx"),
    (google, "google-gemini-cli", "httpx"),
])
def test_adapter_timeout_without_request_is_not_replaced(monkeypatch, adapter, api, transport_name):
    transport = importlib.import_module(transport_name)

    def fail(*args, **kwargs):
        raise transport.ReadTimeout("socket stalled without a request")

    monkeypatch.setattr(adapter, "_client" if adapter is anthropic else "json_events", fail)
    result = adapter.stream(model_for(api), Context("", []), StreamOptions()).result()
    assert result.stop_reason == "error" and result.error_kind == "retryable"
    assert result.error_details["phase"] == "provider.stream"
    assert result.error_details["exception_type"].endswith("ReadTimeout")
    assert "socket stalled without a request" in result.error_details["message"]


def test_optional_sdk_metadata_cannot_replace_original_exception():
    class BrokenMetadata(RuntimeError):
        @property
        def request(self):
            raise RuntimeError("request metadata unavailable")

        response = body = status_code = property(lambda self: self.request)

    result = AssistantMessage(stop_reason="error", error_kind="fatal")
    try:
        raise BrokenMetadata("original provider failure")
    except BrokenMetadata as error:
        attach_error(result, error, model_for("openai"))
    assert result.error_details["exception_type"].endswith("BrokenMetadata")
    assert result.error_details["message"] == "original provider failure"
    assert "metadata unavailable" not in json.dumps(result.error_details)
