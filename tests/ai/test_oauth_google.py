"""Google OAuth: PKCE login, project discovery, onboarding, and refresh, with mocked endpoints."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from virgo.ai import oauth
from virgo.ai import oauth_google as google


class LoginUI:
    def __init__(self, method="Copy code login", answer="google-code"):
        self.method = method
        self.answer = answer
        self.lines = []
        self.questions = []

    def select(self, question, choices):
        return self.method

    def input(self, question, *, password=False, default=""):
        self.questions.append((question, password))
        return self.answer

    def print(self, text, style=None):
        self.lines.append(text)


@pytest.fixture(autouse=True)
def google_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_ID", raising=False)


def test_google_copy_code_login_uses_pkce_and_discovers_project(monkeypatch):
    urls, requests = [], []
    monkeypatch.setattr(oauth, "_open", lambda ui, url: urls.append(url))

    def request(method, url, signal=None, **kwargs):
        requests.append((method, url, kwargs))
        if url == google.TOKEN_URL:
            return {"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": 3600}
        return {"currentTier": {"id": "free-tier"}, "cloudaicompanionProject": "managed-project"}

    monkeypatch.setattr(google, "_request", request)
    ui = LoginUI()
    credential = google.login(ui)
    assert credential["projectId"] == "managed-project"
    assert credential["type"] == "oauth"
    assert credential["access"] == "access-secret"
    query = parse_qs(urlsplit(urls[0]).query)
    fields = requests[0][2]["data"]
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(fields["code_verifier"].encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == [expected]
    assert query["state"][0]
    assert query["redirect_uri"] == [google.COPY_CODE_REDIRECT]
    assert fields["redirect_uri"] == google.COPY_CODE_REDIRECT
    assert fields["code"] == "google-code"
    assert ui.questions[0][1] is True
    assert requests[1][2]["headers"]["Authorization"] == "Bearer access-secret"
    assert "secret" not in "\n".join(ui.lines)


def test_google_browser_callback_is_closed_on_token_failure(monkeypatch):
    callbacks = []

    class Callback:
        uri = "http://localhost:1234/oauth2callback"

        def __init__(self, port, path, state):
            assert port == 0
            assert path == "/oauth2callback"
            assert state
            self.closed = False
            callbacks.append(self)

        def wait(self, signal):
            return "browser-code"

        def close(self):
            self.closed = True

    monkeypatch.setattr(oauth, "_Callback", Callback)
    monkeypatch.setattr(oauth, "_open", lambda ui, url: None)

    def failed(*args, **kwargs):
        assert callbacks[0].closed
        raise oauth.AuthError("Token request failed")

    monkeypatch.setattr(google, "_request", failed)
    with pytest.raises(oauth.AuthError, match="Token request failed"):
        google.login(LoginUI(method="Browser login"))
    assert callbacks[0].closed


def test_google_refresh_preserves_project_and_original_refresh_token(monkeypatch):
    requests = []

    def request(*args, **kwargs):
        requests.append(kwargs["data"])
        return {"access_token": "new-access", "expires_in": 3600}

    monkeypatch.setattr(google, "_request", request)
    original = {
        "type": "oauth",
        "access": "old",
        "refresh": "original-refresh",
        "expires": 0,
        "projectId": "p",
    }
    refreshed = google.refresh(original)
    assert refreshed["projectId"] == "p"
    assert refreshed["refresh"] == "original-refresh"
    assert refreshed["access"] == "new-access"
    assert refreshed["expires"] > original["expires"]
    assert original["access"] == "old"
    assert requests[0]["grant_type"] == "refresh_token"


def test_free_tier_onboarding_omits_configured_project_and_polls(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "user-project")
    requests = []
    responses = iter(
        [
            {"allowedTiers": [{"id": "free-tier", "isDefault": True}]},
            {"name": "operations/new-user", "done": False},
            {"done": True, "response": {"cloudaicompanionProject": {"id": "managed-project"}}},
        ]
    )

    def request(method, url, signal=None, **kwargs):
        requests.append((method, url, kwargs))
        return next(responses)

    monkeypatch.setattr(google, "_request", request)
    monkeypatch.setattr(google, "_wait", lambda *args: None)
    assert google._discover_project("access", LoginUI()) == "managed-project"
    body = requests[1][2]["json"]
    assert "cloudaicompanionProject" not in body
    assert "duetProject" not in body["metadata"]
    assert requests[2][0:2] == ("GET", google.CODE_ASSIST_URL + "/operations/new-user")


def test_project_required_account_reports_environment_setting(monkeypatch):
    monkeypatch.setattr(google, "_request", lambda *a, **kw: {"currentTier": {"id": "standard-tier"}})
    with pytest.raises(oauth.AuthError, match="GOOGLE_CLOUD_PROJECT"):
        google._discover_project("access", LoginUI())


@pytest.mark.parametrize(
    "operation", ["https://attacker.invalid/secret", "operations/../secret", "//attacker.invalid/x"]
)
def test_onboarding_never_forwards_token_to_untrusted_operation(monkeypatch, operation):
    responses = iter(
        [
            {"allowedTiers": [{"id": "free-tier", "isDefault": True}]},
            {"name": operation, "done": False},
        ]
    )
    monkeypatch.setattr(google, "_request", lambda *a, **kw: next(responses))
    with pytest.raises(oauth.AuthError, match="invalid onboarding"):
        google._discover_project("access-secret", LoginUI())


def test_onboarding_has_a_deadline(monkeypatch):
    responses = iter(
        [
            {"allowedTiers": [{"id": "free-tier", "isDefault": True}]},
            {"name": "operations/test", "done": False},
        ]
    )
    monkeypatch.setattr(google, "_request", lambda *a, **kw: next(responses))
    monkeypatch.setattr(google, "ONBOARD_TIMEOUT", 0)
    with pytest.raises(oauth.AuthError, match="timed out"):
        google._discover_project("access", LoginUI())


def test_google_cancel_does_not_make_http_requests(monkeypatch):
    monkeypatch.setattr(google, "_request", lambda *a, **kw: pytest.fail("cancelled login made HTTP request"))
    signal = threading.Event()
    signal.set()
    with pytest.raises(oauth.AuthCancelled):
        google.login(LoginUI(), signal)
    with pytest.raises(oauth.AuthCancelled):
        google.login(LoginUI(method=None))


def test_google_manual_redirect_rejects_missing_or_wrong_state():
    for value in ["http://localhost:1/oauth2callback?code=secret", "code=secret&state=wrong"]:
        with pytest.raises(oauth.AuthError, match="state mismatch"):
            google._parse_code(value, "expected")
    assert (
        google._parse_code("http://localhost:1/oauth2callback?code=secret&state=expected", "expected")
        == "secret"
    )


@pytest.mark.parametrize("status,body", [(401, {"access_token": "secret-value"}), (200, ["secret-value"])])
def test_google_http_errors_never_expose_response_body(monkeypatch, status, body):
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda req: httpx.Response(status, content=json.dumps(body)))
    monkeypatch.setattr(
        google.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs)
    )
    with pytest.raises(oauth.AuthError) as error:
        google._request("POST", google.TOKEN_URL, data={"refresh_token": "secret-value"})
    assert "secret-value" not in str(error.value)


def test_google_refresh_rejects_missing_project():
    with pytest.raises(oauth.AuthError, match="project"):
        google.refresh({"refresh": "r"})
