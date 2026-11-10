"""Google account authentication and Code Assist project setup.

Protocol references: Pi v0.56.1 packages/ai/src/utils/oauth/google-gemini-cli.ts
and google-gemini/gemini-cli packages/core/src/code_assist/{oauth2,setup}.ts.
The installed-app client secret is public client metadata, not a user credential.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from . import oauth

GoogleOAuthError = oauth.AuthError
_check_cancelled = oauth._check
_wait = oauth._wait
_credential = oauth._credential

CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
CLIENT_SECRET = "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"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CODE_ASSIST_URL = "https://cloudcode-pa.googleapis.com/v1internal"
COPY_CODE_REDIRECT = "https://codeassist.google.com/authcode"
SCOPES = " ".join(
    f"https://www.googleapis.com/auth/{scope}"
    for scope in ("cloud-platform", "userinfo.email", "userinfo.profile")
)
ONBOARD_TIMEOUT = 120.0
METADATA = {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}


def _request(method: str, url: str, signal=None, **kwargs) -> dict:
    _check_cancelled(signal)
    try:
        with httpx.Client(timeout=30.0, follow_redirects=False) as client:
            response = client.request(method, url, **kwargs)
    except httpx.HTTPError:
        raise GoogleOAuthError(
            "Google authentication request failed; check your connection and retry"
        ) from None
    _check_cancelled(signal)
    if not response.is_success:
        raise GoogleOAuthError(f"Google authentication request failed (HTTP {response.status_code})")
    try:
        data = response.json()
    except ValueError:
        raise GoogleOAuthError("Google returned an invalid authentication response") from None
    if not isinstance(data, dict):
        raise GoogleOAuthError("Google returned an invalid authentication response")
    return data


def _parse_code(value: str, expected_state: str, *, require_state: bool = False) -> str:
    value = value.strip()
    if not value:
        raise GoogleOAuthError("Missing Google authorization code")
    if "://" in value or "code=" in value:
        query = urlsplit(value).query if "://" in value else value.lstrip("?")
        params = parse_qs(query)
        state = params.get("state", [""])[0]
        if not state or not secrets.compare_digest(state, expected_state):
            raise GoogleOAuthError("Google OAuth state mismatch")
        if params.get("error"):
            raise GoogleOAuthError("Google login was denied")
        value = params.get("code", [""])[0]
    elif "#" in value:
        value, state = value.split("#", 1)
        if not secrets.compare_digest(state, expected_state):
            raise GoogleOAuthError("Google OAuth state mismatch")
    elif require_state:
        raise GoogleOAuthError("Paste the full Google callback URL including state")
    if not value:
        raise GoogleOAuthError("Missing Google authorization code")
    return value


def _authorization_url(redirect_uri: str, challenge: str, state: str) -> str:
    return (
        AUTHORIZE_URL
        + "?"
        + urlencode(
            {
                "client_id": CLIENT_ID,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": SCOPES,
                "access_type": "offline",
                "prompt": "consent",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
    )


def _project_id(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _discover_project(access: str, ui, signal=None) -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
    if project and project.isdecimal():
        raise GoogleOAuthError("Google Cloud requires a project ID, not a numeric project number")
    headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
    metadata = {**METADATA, **({"duetProject": project} if project else {})}
    loaded = _request(
        "POST",
        CODE_ASSIST_URL + ":loadCodeAssist",
        signal,
        headers=headers,
        json={"metadata": metadata, **({"cloudaicompanionProject": project} if project else {})},
    )
    existing = _project_id(loaded.get("cloudaicompanionProject"))
    if loaded.get("currentTier"):
        if existing or project:
            return existing or project
        raise GoogleOAuthError(
            "Set GOOGLE_CLOUD_PROJECT to this account's Google Cloud project ID, then retry"
        )
    tiers = loaded.get("allowedTiers") or []
    if not isinstance(tiers, list):
        raise GoogleOAuthError("Google returned invalid account tier information")
    tier = next((t for t in tiers if isinstance(t, dict) and t.get("isDefault")), {})
    tier_id = tier.get("id") or "legacy-tier"
    if tier_id != "free-tier" and not project:
        project = ui.input("Google Cloud project ID (required for this account)")
        _check_cancelled(signal)
        if project is None:
            raise oauth.AuthCancelled("Login cancelled.")
        project = project.strip()
        if not project or project.isdecimal():
            raise GoogleOAuthError("Enter a Google Cloud project ID, not a numeric project number")
    body = {"tierId": tier_id, "metadata": dict(METADATA)}
    if tier_id != "free-tier" and project:
        body["cloudaicompanionProject"] = project
        body["metadata"]["duetProject"] = project
    ui.print("Preparing Google Code Assist access…")
    operation = _request("POST", CODE_ASSIST_URL + ":onboardUser", signal, headers=headers, json=body)
    deadline = time.monotonic() + ONBOARD_TIMEOUT
    while not operation.get("done"):
        name = operation.get("name")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"operations/[A-Za-z0-9_./-]+", name)
            or ".." in name.split("/")
        ):
            raise GoogleOAuthError("Google returned an invalid onboarding operation")
        if time.monotonic() >= deadline:
            raise GoogleOAuthError("Google Code Assist setup timed out; try login again")
        _wait(min(5.0, max(0, deadline - time.monotonic())), signal)
        operation = _request("GET", CODE_ASSIST_URL + "/" + name, signal, headers=headers)
    if operation.get("error"):
        raise GoogleOAuthError("Google Code Assist setup failed; check your account and project access")
    response = operation.get("response") or {}
    assigned = _project_id(response.get("cloudaicompanionProject")) if isinstance(response, dict) else None
    if assigned or project:
        return assigned or project
    raise GoogleOAuthError("Google did not return a Code Assist project; set GOOGLE_CLOUD_PROJECT and retry")


def login(ui, signal=None) -> dict:
    """Sign in through a loopback browser callback or Google's copy-code page."""
    _check_cancelled(signal)
    method = oauth._select(ui, "Google sign-in method", ["Browser login", "Copy code login"], signal)
    verifier, challenge = oauth._pkce()
    state = secrets.token_urlsafe(32)
    if method == "Copy code login":
        redirect_uri = COPY_CODE_REDIRECT
        oauth._open(ui, _authorization_url(redirect_uri, challenge, state))
        answer = ui.input("Paste the Google authorization code", password=True)
        _check_cancelled(signal)
        if answer is None:
            raise oauth.AuthCancelled("Login cancelled.")
        code = _parse_code(answer, state)
    else:
        try:
            callback = oauth._Callback(0, "/oauth2callback", state)
        except OSError:
            raise GoogleOAuthError("Cannot listen for Google sign-in; retry with Copy code login") from None
        try:
            redirect_uri = callback.uri
            oauth._open(ui, _authorization_url(redirect_uri, challenge, state))
            ui.print("Waiting for Google sign-in. Cancel to stop; use Copy code login on a remote machine.")
            code = callback.wait(signal)
        finally:
            callback.close()
    data = _request(
        "POST",
        TOKEN_URL,
        signal,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": verifier,
        },
    )
    credential = _credential(data)
    credential["projectId"] = _discover_project(credential["access"], ui, signal)
    return credential


def refresh(credential: dict, signal=None) -> dict:
    """Refresh access while retaining Google's original refresh token and project."""
    if not credential.get("refresh"):
        raise GoogleOAuthError("Missing Google refresh token; sign in again")
    if not _project_id(credential.get("projectId")):
        raise GoogleOAuthError("Missing Google Code Assist project; sign in again")
    data = _request(
        "POST",
        TOKEN_URL,
        signal,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": credential["refresh"],
        },
    )
    return _credential(data, credential)
