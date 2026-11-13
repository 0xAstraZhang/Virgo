"""Safe diagnostics shared by provider adapters, independent of the Harness."""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

from ...diagnostics import capture_error, safe_text


def _attribute(value, name):
    """Optional SDK properties can raise when no request/response was attached."""
    try:
        return getattr(value, name, None)
    except Exception:  # diagnostics must not replace the original failure
        return None


def _header(headers, name):
    try:
        return headers.get(name)
    except Exception:  # headers are optional metadata, including in test transports
        return None


def model_secrets(model, token: str = "") -> tuple[str, ...]:
    """Collect credentials from the authenticated request model for diagnostics."""
    env = model.api_key_env
    if not env and model.provider in {"anthropic", "openai"}:
        env = model.provider.upper() + "_API_KEY"
    values = [token, model.options.get("apiKey"), os.environ.get(env) if isinstance(env, str) else None]
    if model.provider == "anthropic":
        values.append(os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    # Custom authentication headers need the same treatment as a normal API key.
    headers = model.options.get("headers")
    if isinstance(headers, dict):
        for name, value in headers.items():
            if isinstance(name, str) and name.lower() not in {"accept", "content-type", "user-agent"}:
                values.append(value)
                if isinstance(value, str) and value.lower().startswith(("bearer ", "basic ")):
                    values.append(value.partition(" ")[2])
    return tuple(value for value in values if isinstance(value, str) and value)


def _endpoint(value) -> str | None:
    """Keep a useful route without credentials, query parameters or fragments."""
    if value is None:
        return None
    try:
        parts = urlsplit(str(value))
        if not parts.hostname:
            return None
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        if parts.port is not None:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:  # malformed optional URL metadata must not mask the failure
        return None


def _http_context(error, response=None) -> dict:
    """Read only named transport fields; never dump requests, bodies or headers."""
    error_response = _attribute(error, "response")
    if error_response is not None:
        response = error_response
    headers = _attribute(response, "headers")
    request = _attribute(error, "request")
    if request is None and response is not None:
        request = _attribute(response, "request")
    values = {
        "status_code": _attribute(error, "status_code") or _attribute(response, "status_code"),
        "request_id": _attribute(error, "request_id")
        or _attribute(response, "_request_id")
        or _header(headers, "x-request-id") or _header(headers, "request-id") or _header(headers, "x-goog-request-id"),
        "retry_after": _attribute(error, "retry_after") or _header(headers, "retry-after"),
        "endpoint": _endpoint(_attribute(error, "endpoint") or _attribute(request, "url")),
    }
    body = _attribute(error, "body")
    if isinstance(body, dict):
        body = dict.get(body, "error", body)
        if isinstance(body, dict):
            for name in ("code", "type"):
                value = dict.get(body, name)
                if isinstance(value, (str, int)):
                    values["error_" + name] = value
    return {key: value for key, value in values.items() if value is not None}


def _provider_message(error) -> str | None:
    """SDK exception strings often contain the entire response JSON, not just its error."""
    body = _attribute(error, "body")
    if body is None:
        return None
    if isinstance(body, dict):
        error_body = dict.get(body, "error", body)
        if isinstance(error_body, dict) and isinstance(dict.get(error_body, "message"), str):
            message = error_body["message"]
        else:
            message = "The provider rejected the request."
    else:
        message = "The provider returned an error response."
    status = _attribute(error, "status_code")
    return f"Provider HTTP {status}: {message}" if status is not None else message


def _redact(value, secrets):
    if isinstance(value, str):
        return safe_text(value, secrets=secrets)
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    return value


def attach_error(out, error, model, *, phase="provider.stream", token="", response=None) -> None:
    """Attach the original failure before an adapter turns it into a stream event.

    The concise UI message may remain generic for network failures; the diagnostic
    retains the actual exception and its cause. Cancellation is an expected stop.
    """
    secrets = model_secrets(model, token)
    message = _provider_message(error)
    out.error_message = safe_text(message or out.error_message or error, secrets=secrets)
    if out.stop_reason == "aborted":
        out.error_kind = "aborted"
        return
    if out.stop_reason != "error":
        return
    context = {"provider": model.provider, "model": model.id, "api": model.api}
    http = _http_context(error, response)
    if http:
        context["http"] = http
    if error is not None or out.error_details is None:
        out.error_details = capture_error(
            error if error is not None else out.error_message,
            phase=phase, kind=out.error_kind or "fatal", context=context, secrets=secrets,
            message=message,
        )
    else:
        # A decoded SSE failure may have been diagnosed before its SDK transport
        # supplied the actual credential or response headers.
        details = out.error_details
        details.setdefault("context", {}).update({key: value for key, value in context.items() if key != "http"})
        if http:
            details["context"].setdefault("http", {}).update(http)
        out.error_details = _redact(details, secrets)
