"""Account authentication, ported from Pi's OAuth provider contracts.

Tokens stay in the caller's credential store. Expiry timestamps use seconds.
Source: @earendil-works/pi-ai 1.5.0, dist/auth/oauth (MIT).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

ANTHROPIC_CLIENT = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_TOKEN = "https://platform.claude.com/v1/oauth/token"
OPENAI_CLIENT = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_TOKEN = "https://auth.openai.com/oauth/token"
XAI_CLIENT = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_TOKEN = "https://auth.x.ai/oauth2/token"
KIMI_CLIENT = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_TOKEN = "https://auth.kimi.com/api/oauth/token"
LOGIN_TIMEOUT = 15 * 60


class AuthError(ValueError):
    """A safe, user-facing authentication failure (never contains token bodies)."""


class AuthCancelled(AuthError):
    pass


def _check(signal) -> None:
    if signal is not None and signal.is_set():
        raise AuthCancelled("Login cancelled.")


def _wait(seconds: float, signal) -> None:
    if signal is None:
        time.sleep(seconds)
    elif signal.wait(seconds):
        raise AuthCancelled("Login cancelled.")


def _required(body: dict, name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AuthError(f"OAuth response is missing {name}.")
    return value


def _positive(value, default=None) -> float:
    if value is None and default is not None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise AuthError("OAuth response contains an invalid expiry or polling interval.")
    return float(value)


def _request(url: str, fields: dict, signal=None, *, json_body=False) -> tuple[int, dict]:
    _check(signal)
    try:
        response = httpx.post(
            url,
            **({"json": fields} if json_body else {"data": fields}),
            headers={"Accept": "application/json"},
            timeout=30,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        _check(signal)
        raise AuthError("OAuth request failed. Check your connection and try again.") from None
    _check(signal)
    try:
        body = response.json()
    except (ValueError, TypeError):
        raise AuthError("OAuth server returned an invalid response.") from None
    if not isinstance(body, dict):
        raise AuthError("OAuth server returned an invalid response.")
    return response.status_code, body


def _success(status: int) -> None:
    if not 200 <= status < 300:
        raise AuthError(f"OAuth request failed (HTTP {status}). Please try logging in again.")


def _credential(body: dict, previous: dict | None = None, *, default_expiry=None) -> dict:
    previous = previous or {}
    refresh_token = body.get("refresh_token", previous.get("refresh"))
    return {
        **previous,
        "type": "oauth",
        "access": _required(body, "access_token"),
        "refresh": _required({"refresh_token": refresh_token}, "refresh_token"),
        "expires": time.time() + _positive(body.get("expires_in"), default_expiry),
    }


def _t(ui, key: str, /, **values) -> str:
    from ..i18n import Translator

    return (getattr(ui, "t", None) or Translator())(key, **values)


def _open(ui, url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AuthError("OAuth server returned an invalid verification URL.")
    ui.print(_t(ui, "harness.oauth.open", url=url))
    try:
        webbrowser.open(url)
    except (OSError, webbrowser.Error):
        pass  # the displayed link also works in remote terminals


def _select(ui, question: str, choices: list[str], signal) -> str:
    _check(signal)
    keys = {'Browser login': 'harness.oauth.browser',
     'Choose a login method': 'harness.oauth.method',
     'Copy code login': 'harness.oauth.copy_code',
     'Device code login': 'harness.oauth.device',
     'Google sign-in method': 'harness.oauth.google_method',
     'Paste redirect URL': 'harness.oauth.redirect'}
    labels = {(_t(ui, keys[value]) if value in keys else value): value for value in choices}
    question = _t(ui, keys[question]) if question in keys else question
    choice = ui.select(question, list(labels))
    _check(signal)
    if choice is None:
        raise AuthCancelled(_t(ui, "harness.auth.cancelled"))
    if choice not in labels:
        raise AuthError(_t(ui, "harness.oauth.invalid_method"))
    return labels[choice]


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _authorization_code(value: str, expected_state: str, redirect_uri: str | None = None) -> str:
    value = value.strip()
    if "://" in value:
        url = urlsplit(value)
        if redirect_uri:
            expected = urlsplit(redirect_uri)
            if (url.scheme, url.netloc, url.path) != (expected.scheme, expected.netloc, expected.path):
                raise AuthError("Paste the complete redirect URL shown after this login.")
        params = parse_qs(url.query)
    elif value.startswith("/") and "?" in value:
        params = parse_qs(urlsplit(value).query)
    elif "#" in value:
        code, state = value.split("#", 1)
        params = {"code": [code], "state": [state]}
    else:
        params = parse_qs(value)
    state = (params.get("state") or [""])[0]
    if not state or not secrets.compare_digest(state, expected_state):
        raise AuthError("OAuth state mismatch. Restart login and use the new authorization link.")
    if params.get("error"):
        raise AuthError("Account authorization was denied.")
    return _required({"code": (params.get("code") or [""])[0]}, "code")


class _Callback:
    """One loopback callback; invalid state cannot complete another login."""

    def __init__(self, port: int, path: str, state: str, *, dynamic_port=False):
        self.result: str | None = None
        self.error: AuthError | None = None
        self.ready = threading.Event()
        callback = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(2)
                super().setup()

            def do_GET(self):  # noqa: N802
                parsed = urlsplit(self.path)
                if parsed.path != path:
                    self.send_error(404)
                    return
                params = parse_qs(parsed.query)
                if params.get("error") and secrets.compare_digest((params.get("state") or [""])[0], state):
                    callback.error = AuthError("Account authorization was denied.")
                    callback.ready.set()
                    self.send_error(400, "Authorization denied")
                    return
                try:
                    code = _authorization_code(self.path, state)
                except AuthError:
                    self.send_error(400, "Invalid OAuth callback")
                    return
                body = b"Authentication completed. You can return to Virgo and close this window."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                callback.result = code
                callback.ready.set()

            def log_message(self, *args):
                pass  # callback URLs contain credentials

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            if not dynamic_port:
                raise
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.uri = f"http://localhost:{self.server.server_port}{path}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )
        self.thread.start()

    def wait(self, signal) -> str:
        deadline = time.monotonic() + LOGIN_TIMEOUT
        while not self.ready.wait(0.1):
            _check(signal)
            if time.monotonic() >= deadline:
                raise AuthError("Login timed out. Please try again.")
        _check(signal)
        if self.error:
            raise self.error
        return self.result or ""

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _input_code(ui, state: str, signal, redirect_uri=None) -> str:
    text = ui.input(_t(ui, "harness.oauth.paste"), password=True)
    _check(signal)
    if text is None:
        raise AuthCancelled("Login cancelled.")
    return _authorization_code(text, state, redirect_uri)


def _browser_login(provider: str, ui, signal) -> dict:
    anthropic = provider == "anthropic"
    choices = (
        ["Browser login", "Copy code login"]
        if anthropic
        else ["Browser login", "Paste redirect URL", "Device code login"]
    )
    method = _select(ui, "Choose a login method", choices, signal)
    if method == "Device code login":
        return _openai_device(ui, signal)
    verifier, challenge = _pkce()
    state = verifier if anthropic else secrets.token_urlsafe(32)
    callback = None
    redirect_uri = (
        "https://platform.claude.com/oauth/code/callback"
        if anthropic
        else "http://localhost:1455/auth/callback"
    )
    if method == "Browser login":
        try:
            callback = _Callback(
                53692 if anthropic else 1455,
                "/callback" if anthropic else "/auth/callback",
                state,
                dynamic_port=anthropic,
            )
            redirect_uri = callback.uri
        except OSError:
            ui.print(_t(ui, "harness.oauth.port"))
    params = {
        "client_id": ANTHROPIC_CLIENT if anthropic else OPENAI_CLIENT,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
        if anthropic
        else "openid profile email offline_access",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if anthropic:
        params["code"] = "true"
        authorize_url = "https://claude.ai/oauth/authorize"
    else:
        params.update(id_token_add_organizations="true", codex_cli_simplified_flow="true", originator="virgo")
        authorize_url = "https://auth.openai.com/oauth/authorize"
    try:
        _open(ui, authorize_url + "?" + urlencode(params))
        code = callback.wait(signal) if callback else _input_code(ui, state, signal, redirect_uri)
        fields = {
            "grant_type": "authorization_code",
            "client_id": params["client_id"],
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        }
        if anthropic:
            fields["state"] = state
        status, body = _request(
            ANTHROPIC_TOKEN if anthropic else OPENAI_TOKEN, fields, signal, json_body=anthropic
        )
        _success(status)
        credential = _credential(body)
        return credential if anthropic else _openai_account(credential)
    finally:
        if callback:
            callback.close()


def _openai_account(credential: dict) -> dict:
    try:
        payload = credential["access"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        account_id = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
        return {**credential, "accountId": _required({"accountId": account_id}, "accountId")}
    except (KeyError, IndexError, ValueError, TypeError):
        raise AuthError("OpenAI token is missing the ChatGPT account ID. Please log in again.") from None


def _poll(url, fields, signal, *, interval, expires, json_body=False, openai_device=False) -> dict:
    interval = max(1.0, float(interval))
    deadline = time.monotonic() + min(_positive(expires), LOGIN_TIMEOUT)
    while time.monotonic() < deadline:
        _wait(min(interval, max(0, deadline - time.monotonic())), signal)
        if time.monotonic() >= deadline:
            break
        status, body = _request(url, fields, signal, json_body=json_body)
        if 200 <= status < 300:
            return body
        error = body.get("error")
        if isinstance(error, dict):
            error = error.get("code")
        if error == "slow_down":
            interval += 5
            if isinstance(body.get("interval"), (int, float)):
                interval = max(interval, _positive(body["interval"]))
        elif error in ("authorization_pending", "deviceauth_authorization_pending") or (
            openai_device and status in (403, 404)
        ):
            continue
        elif error in ("access_denied", "authorization_denied"):
            raise AuthError("Account authorization was denied.")
        elif error == "expired_token":
            break
        else:
            _success(status)
    raise AuthError("Login timed out or the device code expired. Please try again.")


def _device_login(provider: str, ui, signal) -> dict:
    xai = provider == "xai"
    client = XAI_CLIENT if xai else KIMI_CLIENT
    fields = {"client_id": client}
    if xai:
        fields.update(scope="openid profile email offline_access grok-cli:access api:access", referrer="pi")
    status, body = _request(
        "https://auth.x.ai/oauth2/device/code"
        if xai
        else "https://auth.kimi.com/api/oauth/device_authorization",
        fields,
        signal,
    )
    _success(status)
    code = _required(body, "device_code")
    user_code = _required(body, "user_code")
    url = body.get("verification_uri_complete") or _required(body, "verification_uri")
    ui.print(_t(ui, "harness.oauth.device_code", code=user_code))
    _open(ui, url)
    interval = body.get("interval", 5)
    if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
        interval = 5
    token = _poll(
        XAI_TOKEN if xai else KIMI_TOKEN,
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client,
            "device_code": code,
        },
        signal,
        interval=interval,
        expires=body.get("expires_in", LOGIN_TIMEOUT),
    )
    return _credential(token, default_expiry=3600 if xai else None)


def _openai_device(ui, signal) -> dict:
    status, body = _request(
        "https://auth.openai.com/api/accounts/deviceauth/usercode",
        {"client_id": OPENAI_CLIENT},
        signal,
        json_body=True,
    )
    _success(status)
    device_id, user_code = _required(body, "device_auth_id"), _required(body, "user_code")
    try:
        interval = float(body.get("interval", 5))
    except (TypeError, ValueError):
        raise AuthError("OpenAI returned an invalid polling interval.") from None
    if not math.isfinite(interval) or interval < 0:
        raise AuthError("OpenAI returned an invalid polling interval.")
    ui.print(_t(ui, "harness.oauth.device_code", code=user_code))
    _open(ui, "https://auth.openai.com/codex/device")
    authorization = _poll(
        "https://auth.openai.com/api/accounts/deviceauth/token",
        {
            "device_auth_id": device_id,
            "user_code": user_code,
        },
        signal,
        interval=interval,
        expires=LOGIN_TIMEOUT,
        json_body=True,
        openai_device=True,
    )
    status, token = _request(
        OPENAI_TOKEN,
        {
            "grant_type": "authorization_code",
            "client_id": OPENAI_CLIENT,
            "code": _required(authorization, "authorization_code"),
            "code_verifier": _required(authorization, "code_verifier"),
            "redirect_uri": "https://auth.openai.com/deviceauth/callback",
        },
        signal,
    )
    _success(status)
    return _openai_account(_credential(token))


def login(provider_id: str, ui, signal=None) -> dict:
    """Interactively obtain account credentials for one of the login presets."""
    _check(signal)
    if provider_id in ("anthropic", "openai-codex"):
        return _browser_login(provider_id, ui, signal)
    if provider_id in ("xai", "kimi-coding"):
        return _device_login(provider_id, ui, signal)
    if provider_id == "google-gemini-cli":
        from .oauth_google import login as google_login

        return google_login(ui, signal)
    raise AuthError("This provider does not support account login.")


def refresh(provider_id: str, credential: dict, signal=None) -> dict:
    """Refresh an existing account credential, retaining provider metadata."""
    _check(signal)
    if provider_id == "google-gemini-cli":
        from .oauth_google import refresh as google_refresh

        return google_refresh(credential, signal)
    providers = {
        "anthropic": (ANTHROPIC_CLIENT, ANTHROPIC_TOKEN),
        "openai-codex": (OPENAI_CLIENT, OPENAI_TOKEN),
        "xai": (XAI_CLIENT, XAI_TOKEN),
        "kimi-coding": (KIMI_CLIENT, KIMI_TOKEN),
    }
    if provider_id not in providers:
        raise AuthError("This provider does not support account login.")
    client, url = providers[provider_id]
    status, body = _request(
        url,
        {
            "grant_type": "refresh_token",
            "client_id": client,
            "refresh_token": _required(credential, "refresh"),
        },
        signal,
        json_body=provider_id == "anthropic",
    )
    _success(status)
    result = _credential(body, credential, default_expiry=3600 if provider_id == "xai" else None)
    return _openai_account(result) if provider_id == "openai-codex" else result
