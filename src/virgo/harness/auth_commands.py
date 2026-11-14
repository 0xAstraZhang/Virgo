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
        raise ValueError(ui.t("harness.auth.required", question=question))
    return value


def _endpoint(value, t=None):
    from ..i18n import Translator

    t = t or Translator()
    url = urlsplit(value)
    if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError(t("harness.auth.endpoint"))
    return value.rstrip("/")


def login(session, args):
    ui = session.ui
    if not ui.interactive:
        ui.print(session.t("harness.auth.interactive"), "error")
        return
    try:
        argv = shlex.split(args)
        if len(argv) > 2:
            raise ValueError(session.t("harness.auth.usage_secret"))
        mode = argv.pop(0).lower() if argv and argv[0].lower() in ("auth", "api") else None
        provider = argv.pop(0).lower() if argv else None
        if argv:
            raise ValueError(session.t("harness.auth.usage"))
        if mode is None and provider:
            mode = "api" if provider in (*API_PRESETS, "custom") and provider != "kimi" else None
            if provider in (*AUTH_PRESETS, *_ALIASES) and provider != "kimi":
                mode = "auth"
        if mode is None:
            mode = _choose(ui, session.t("harness.auth.method"), {session.t("harness.auth.auth_label"): "auth", session.t("harness.auth.api_label"): "api"})
        if mode == "auth":
            provider = "kimi-coding" if provider == "kimi" else _ALIASES.get(provider, provider)
            if provider is None:
                provider = _choose(ui, session.t("harness.auth.auth_picker"), {p["name"]: k for k, p in AUTH_PRESETS.items()})
            if provider not in AUTH_PRESETS:
                raise ValueError(session.t("harness.auth.auth_provider"))
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
                provider = _choose(ui, session.t("harness.auth.api_picker"), {
                    **{p["name"]: k for k, p in API_PRESETS.items()}, session.t("harness.auth.custom"): "custom",
                })
            if provider == "custom":
                provider = _input(ui, session.t("harness.auth.provider_id"), default="custom").lower()
                if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", provider):
                    raise ValueError(session.t("harness.auth.provider_invalid"))
                if provider == "all" or provider in BUILTIN_PROVIDERS:
                    raise ValueError(session.t("harness.auth.provider_builtin"))
                api = _choose(ui, session.t("harness.auth.api_protocol"), {session.t("harness.auth.openai_compatible"): "openai", "Anthropic Messages": "anthropic"})
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
                raise ValueError(session.t("harness.auth.api_provider"))
            config["baseUrl"] = _endpoint(_input(ui, session.t("harness.auth.base_url"), default=config.get("baseUrl", "")), session.t)
            model_id = _input(ui, session.t("harness.auth.model_id"), default=default_model)
            if any(c.isspace() for c in model_id):
                raise ValueError(session.t("harness.auth.model_invalid"))
            session.settings.check_provider_update(provider, config)
            key = _input(ui, session.t("harness.auth.api_key"), password=True)
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
        ui.print(session.t("harness.auth.success", provider=provider, model=spec), "ok")
    except _Cancelled:
        ui.print(session.t("harness.auth.cancelled"), "dim")
    except Exception as exc:  # noqa: BLE001 - OAuth errors are redacted by the auth layer.
        ui.print(session.t("harness.auth.failed", error=exc), "error")


def logout(session, args):
    ui = session.ui
    try:
        provider = args.strip().lower()
        saved = session.auth.all()
        if not saved:
            ui.print(session.t("harness.auth.empty"))
            return
        if not provider:
            if not ui.interactive:
                ui.print(session.t("harness.auth.logout_usage"), "error")
                return
            labels = {}
            for name, credential in saved.items():
                title = {**AUTH_PRESETS, **API_PRESETS}.get(name, {}).get("name", name)
                kind = "Auth" if credential.get("type") == "oauth" else "API"
                labels[f"{title} · {kind} ({name})"] = name
            labels[session.t("harness.auth.logout_all")] = "all"
            provider = _choose(ui, session.t("harness.auth.logout_picker"), labels)
        if provider not in saved:
            provider = _ALIASES.get(provider, provider)
            if provider == "kimi" and "kimi-coding" in saved:
                provider = "kimi-coding"
        removed = session.auth.remove(list(saved) if provider == "all" else [provider])
        ui.print(session.t("harness.auth.logout_success", providers=", ".join(removed)) if removed else session.t("harness.auth.logout_missing", provider=provider))
        ui.print(session.t("harness.auth.env_remains"), "dim")
    except _Cancelled:
        ui.print(session.t("harness.auth.logout_cancelled"), "dim")
    except Exception as exc:  # noqa: BLE001
        ui.print(session.t("harness.auth.logout_failed", error=exc), "error")
