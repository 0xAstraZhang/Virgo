"""Interactive account and API login. Secrets are collected outside chat/history."""

from __future__ import annotations

import re
import shlex
from urllib.parse import urlsplit

from ..ai.auth_presets import API_PRESETS, AUTH_PRESETS
from ..ai.models import BUILTIN_PROVIDERS

_ALIASES = {"openai": "openai-codex", "google": "google-gemini-cli", "anthropic": "anthropic"}


class _Cancelled(Exception):
    pass


def _choose(ui, question, values):
    labels = list(values)
    selected = ui.select(question, labels)
    if selected is None:
        raise _Cancelled
    return values[selected]


def _input(ui, question, *, password=False, default=""):
    value = ui.input(question, password=password, default=default)
    if value is None:
        raise _Cancelled
    value = value.strip()
    if not value:
        raise ValueError(f"{question} is required")
    return value


def _endpoint(value):
    url = urlsplit(value)
    if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Base URL must be an HTTP(S) endpoint without credentials, query or fragment")
    return value.rstrip("/")


def login(session, args):
    ui = session.ui
    if not ui.interactive:
        ui.print("/login requires an interactive terminal; use an API key environment variable in print mode.", "error")
        return
    try:
        argv = shlex.split(args)
        if len(argv) > 2:
            raise ValueError("Usage: /login [auth|api] [provider]. Enter secrets only in the password prompt.")
        mode = argv.pop(0).lower() if argv and argv[0].lower() in ("auth", "api") else None
        provider = argv.pop(0).lower() if argv else None
        if argv:
            raise ValueError("Usage: /login [auth|api] [provider]")
        if mode is None and provider:
            mode = "api" if provider in (*API_PRESETS, "custom") and provider != "kimi" else None
            if provider in (*AUTH_PRESETS, *_ALIASES) and provider != "kimi":
                mode = "auth"
        if mode is None:
            mode = _choose(ui, "Login method", {"Auth — 登录账号": "auth", "API — API 接入": "api"})
        if mode == "auth":
            provider = "kimi-coding" if provider == "kimi" else _ALIASES.get(provider, provider)
            if provider is None:
                provider = _choose(ui, "Auth — 选择账号", {p["name"]: k for k, p in AUTH_PRESETS.items()})
            if provider not in AUTH_PRESETS:
                raise ValueError("Auth provider must be OpenAI, Anthropic, xAI, Google or Kimi")
            preset = AUTH_PRESETS[provider]
            config = dict(preset["config"])
            model_id = preset["model"]
            session.settings.check_provider_update(provider, config)
            from ..ai import oauth

            credential = oauth.login(provider, ui, signal=session.main.agent.signal)
            if not credential:
                raise _Cancelled
        else:
            if provider is None:
                provider = _choose(ui, "API — 选择服务商", {
                    **{p["name"]: k for k, p in API_PRESETS.items()}, "自定义": "custom",
                })
            if provider == "custom":
                provider = _input(ui, "Provider ID", default="custom").lower()
                if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", provider):
                    raise ValueError("Provider ID must contain only letters, numbers, underscores or hyphens")
                if provider == "all" or provider in BUILTIN_PROVIDERS:
                    raise ValueError("Choose a custom Provider ID that differs from the built-in providers")
                api = _choose(ui, "API protocol", {"OpenAI compatible": "openai", "Anthropic Messages": "anthropic"})
                config = {"api": api, "apiKeyEnv": f"VIRGO_{provider.upper().replace('-', '_')}_API_KEY"}
                default_model = ""
            elif provider in API_PRESETS:
                preset = API_PRESETS[provider]
                config = dict(preset["config"])
                configured = session.settings.get("providers", {}).get(provider, {})
                for key in ("baseUrl", "apiKeyEnv"):
                    if configured.get(key):
                        config[key] = configured[key]
                default_model = preset["model"]
            else:
                raise ValueError("API provider must be DeepSeek, GLM, Kimi or custom")
            config["baseUrl"] = _endpoint(_input(ui, "Base URL", default=config.get("baseUrl", "")))
            model_id = _input(ui, "Model ID", default=default_model)
            if any(c.isspace() for c in model_id):
                raise ValueError("Model ID cannot contain whitespace")
            session.settings.check_provider_update(provider, config)
            key = _input(ui, "API key", password=True)
            credential = {"type": "api_key", "key": key}
        if session.main.agent.signal.is_set():
            raise _Cancelled
        spec = f"{provider}/{model_id}"
        credential["connection"] = {"api": config["api"], "baseUrl": config.get("baseUrl", "")}
        session.auth.save_login(
            provider, credential,
            lambda: session.settings.save_user({"providers": {provider: config}, "model": spec}),
        )
        session.set_model(spec)
        ui.print(f"Logged in to {provider}. Model: {spec}", "ok")
    except _Cancelled:
        ui.print("Login cancelled.", "dim")
    except Exception as exc:  # noqa: BLE001 - OAuth errors are redacted by the auth layer.
        ui.print(f"Login failed: {exc}", "error")


def logout(session, args):
    ui = session.ui
    try:
        provider = args.strip().lower()
        saved = session.auth.all()
        if not saved:
            ui.print("No saved logins. Environment variable credentials are managed by your shell.")
            return
        if not provider:
            if not ui.interactive:
                ui.print("Usage: /logout <provider|all>", "error")
                return
            labels = {}
            for name, credential in saved.items():
                title = {**AUTH_PRESETS, **API_PRESETS}.get(name, {}).get("name", name)
                kind = "Auth" if credential.get("type") == "oauth" else "API"
                labels[f"{title} · {kind} ({name})"] = name
            labels["All saved logins"] = "all"
            provider = _choose(ui, "Logout — 选择已保存的登录", labels)
        if provider not in saved:
            provider = _ALIASES.get(provider, provider)
            if provider == "kimi" and "kimi-coding" in saved:
                provider = "kimi-coding"
        removed = session.auth.remove(list(saved) if provider == "all" else [provider])
        ui.print(f"Logged out: {', '.join(removed)}." if removed else f"No saved login for {provider}.")
        ui.print("Environment variable credentials, if set, still apply.", "dim")
    except _Cancelled:
        ui.print("Logout cancelled.", "dim")
    except Exception as exc:  # noqa: BLE001
        ui.print(f"Logout failed: {exc}", "error")
