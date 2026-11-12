"""OAuth protocol contracts: mocked issuers, real loopback callback, no accounts."""

from __future__ import annotations

import base64
import json
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from virgo.ai import oauth


class LoginUI:
    def __init__(self, method="Copy code login"):
        self.method = method
        self.lines = []
        self.answer = None

    def select(self, question, options):
        return self.method

    def print(self, text, style=None):
        self.lines.append(text)

    def input(self, question, *, password=False, default=""):
        assert password  # authorization code should not enter terminal history
        if self.answer is not None:
            return self.answer
        url = next(line.split("\n", 1)[1] for line in self.lines if line.startswith("Open this"))
        query = parse_qs(urlsplit(url).query)
        return (
            query["redirect_uri"][0]
            + "?"
            + urlencode({"code": "authorization-code", "state": query["state"][0]})
        )


def token(access="access", **kwargs):
    return {"access_token": access, "refresh_token": "refresh", "expires_in": 3600, **kwargs}


def jwt(account="account-1"):
    payload = json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account}}).encode()
    return "header." + base64.urlsafe_b64encode(payload).decode().rstrip("=") + ".signature"


@pytest.fixture
def issuer(monkeypatch):
    requests = []
    responses = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(oauth.httpx, "post", post)
    monkeypatch.setattr(oauth.webbrowser, "open", lambda _: True)
    monkeypatch.setattr(oauth, "_wait", lambda seconds, signal: oauth._check(signal))
    return requests, responses


def test_anthropic_copy_code_pkce_and_exchange(issuer):
    requests, responses = issuer
    responses.append((200, token()))
    ui = LoginUI()
    credential = oauth.login("anthropic", ui)
    assert credential["type"] == "oauth"
    assert credential["access"] == "access"
    assert credential["expires"] > oauth.time.time() + 3500
    url, kwargs = requests[0]
    assert url == oauth.ANTHROPIC_TOKEN
    fields = kwargs["json"]
    assert fields["code"] == "authorization-code"
    assert fields["state"] == fields["code_verifier"]
    auth = parse_qs(urlsplit(ui.lines[0].split("\n", 1)[1]).query)
    expected = (
        base64.urlsafe_b64encode(oauth.hashlib.sha256(fields["code_verifier"].encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert auth["code_challenge"] == [expected]
    assert kwargs["follow_redirects"] is False


def test_openai_manual_exchange_extracts_account(issuer):
    requests, responses = issuer
    responses.append((200, token(jwt())))
    credential = oauth.login("openai-codex", LoginUI("Paste redirect URL"))
    assert credential["accountId"] == "account-1"
    assert requests[0][1]["data"]["redirect_uri"] == "http://localhost:1455/auth/callback"


@pytest.mark.parametrize("value", ["bare-code", "code#wrong", "code=value&state=wrong", "code=value"])
def test_manual_code_rejects_missing_or_wrong_state(value):
    with pytest.raises(oauth.AuthError, match="state mismatch"):
        oauth._authorization_code(value, "expected")


def test_manual_url_must_match_callback():
    with pytest.raises(oauth.AuthError, match="complete redirect"):
        oauth._authorization_code(
            "https://elsewhere.test/?code=code&state=ok", "ok", "http://localhost:1455/auth/callback"
        )


def test_real_callback_rejects_cross_session_and_accepts_correct_state():
    callback = oauth._Callback(0, "/callback", "expected-state")
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.get(callback.uri + "?code=secret&state=wrong")
            assert response.status_code == 400
            assert not callback.ready.is_set()
            response = client.get(callback.uri + "?code=authorization&state=expected-state")
            assert response.status_code == 200
        assert callback.wait(None) == "authorization"
    finally:
        callback.close()


def test_callback_denial_unblocks_login():
    callback = oauth._Callback(0, "/callback", "state")
    try:
        with httpx.Client(trust_env=False) as client:
            client.get(callback.uri + "?error=access_denied&state=state")
        with pytest.raises(oauth.AuthError, match="denied"):
            callback.wait(None)
    finally:
        callback.close()


@pytest.mark.parametrize("provider,host", [("xai", "auth.x.ai"), ("kimi-coding", "auth.kimi.com")])
def test_device_login_pending_slowdown_success(issuer, provider, host):
    requests, responses = issuer
    responses.extend(
        [
            (
                200,
                {
                    "device_code": "device",
                    "user_code": "USER-CODE",
                    "verification_uri": f"https://{host}/verify",
                    "interval": 1,
                    "expires_in": 600,
                },
            ),
            (400, {"error": "authorization_pending"}),
            (400, {"error": "slow_down", "interval": 10}),
            (200, token()),
        ]
    )
    ui = LoginUI()
    credential = oauth.login(provider, ui)
    assert credential["access"] == "access"
    assert requests[-1][1]["data"]["device_code"] == "device"
    assert "USER-CODE" in ui.lines[0]
    assert all("refresh" not in line for line in ui.lines)


def test_device_login_rejects_untrusted_verification_url(issuer):
    requests, responses = issuer
    responses.append(
        (200, {"device_code": "device", "user_code": "USER-CODE", "verification_uri": "file:///private/key"})
    )
    with pytest.raises(oauth.AuthError, match="verification URL"):
        oauth.login("xai", LoginUI())
    assert len(requests) == 1


def test_openai_device_code_exchange_contract(issuer):
    requests, responses = issuer
    responses.extend(
        [
            (200, {"device_auth_id": "device", "user_code": "USER-CODE", "interval": "1"}),
            (403, {}),
            (200, {"authorization_code": "authorization", "code_verifier": "verifier"}),
            (200, token(jwt())),
        ]
    )
    credential = oauth.login("openai-codex", LoginUI("Device code login"))
    assert credential["accountId"] == "account-1"
    assert requests[-1][1]["data"]["code_verifier"] == "verifier"
    assert requests[-1][1]["data"]["redirect_uri"] == "https://auth.openai.com/deviceauth/callback"


@pytest.mark.parametrize("provider", ["anthropic", "xai", "kimi-coding", "openai-codex"])
def test_refresh_rotates_and_preserves_metadata(issuer, provider):
    requests, responses = issuer
    responses.append(
        (200, token(jwt() if provider == "openai-codex" else "new-access", refresh_token="new-refresh"))
    )
    credential = oauth.refresh(
        provider,
        {"type": "oauth", "access": "old", "refresh": "old-refresh", "expires": 1, "model": "preferred"},
    )
    assert credential["refresh"] == "new-refresh"
    assert credential["model"] == "preferred"
    assert (requests[0][1].get("data") or requests[0][1]["json"])["refresh_token"] == "old-refresh"


def test_xai_refresh_without_rotating_refresh_token(issuer):
    _, responses = issuer
    responses.append((200, {"access_token": "new"}))
    credential = oauth.refresh("xai", {"refresh": "original"})
    assert credential["refresh"] == "original"


def test_cancellation_prevents_network_request(issuer):
    requests, _ = issuer
    signal = threading.Event()
    signal.set()
    with pytest.raises(oauth.AuthCancelled):
        oauth.login("xai", LoginUI(), signal)
    assert requests == []


def test_cancelled_selection_does_not_request_token(issuer):
    requests, _ = issuer
    with pytest.raises(oauth.AuthCancelled):
        oauth.login("anthropic", LoginUI(None))
    assert requests == []


def test_token_error_body_never_leaks(issuer):
    _, responses = issuer
    responses.append((401, {"error": "sensitive-token", "error_description": "secret-refresh-token"}))
    with pytest.raises(oauth.AuthError) as error:
        oauth.refresh("xai", {"refresh": "private"})
    assert "401" in str(error.value)
    assert "sensitive" not in str(error.value)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("expiry", [float("nan"), float("inf"), -1, 0, True, "3600"])
def test_token_expiry_requires_positive_finite_seconds(expiry):
    with pytest.raises(oauth.AuthError):
        oauth._credential(token(expires_in=expiry))


def test_callback_cancellation_and_timeout(monkeypatch):
    callback = oauth._Callback(0, "/callback", "state")
    try:
        signal = threading.Event()
        signal.set()
        with pytest.raises(oauth.AuthCancelled):
            callback.wait(signal)
        monkeypatch.setattr(oauth, "LOGIN_TIMEOUT", 0)
        with pytest.raises(oauth.AuthError, match="timed out"):
            callback.wait(None)
    finally:
        callback.close()
