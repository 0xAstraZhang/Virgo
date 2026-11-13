"""Verified web offers for the built-in API adapters."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from urllib.parse import urlsplit

from ..web import WebOffer


def endpoint(model) -> str:
    if model.base_url:
        return model.base_url.rstrip("/")
    # Match each transport's actual environment override, not just the provider label.
    if model.api == "anthropic":
        return os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    if model.api == "openai-codex":
        return "https://chatgpt.com/backend-api/codex"
    if model.api in {"openai", "openai-responses"}:
        if model.provider == "openai":
            return os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        return "https://api.openai.com/v1"
    return ""


def native_tool(model, max_uses: int = 3) -> dict | None:
    """Conservative protocol/model allowlist; compatible proxies opt in explicitly."""
    override = model.options.get("nativeSearch")
    if override is False:
        return None
    address = urlsplit(endpoint(model))
    official = address.scheme == "https" and address.port in (None, 443) and not address.username
    host = address.hostname if official else None
    name = model.id
    if model.api == "anthropic" and (
        override is True or host == "api.anthropic.com" and re.match(
            r"^claude-(?:(?:opus|sonnet|haiku|fable|mythos)-[4-9]|3-[57]-sonnet)", name
        )
    ):
        # Basic version avoids provisioning code execution just to filter results.
        return {"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}
    if model.api in {"openai", "openai-responses", "openai-codex"}:
        supported = (
            model.api != "openai-codex" and host == "api.openai.com"
            and re.match(r"^(?:gpt-[56](?:[.-]|$)|gpt-4[.](?:1|o)|gpt-4o|o3(?:-|$))", name)
            and not any(part in name for part in ("audio", "realtime", "search", "chat-latest"))
        ) or (
            model.api == "openai-codex" and host == "chatgpt.com" and name.startswith(("gpt-5", "gpt-6"))
        ) or (
            model.api == "openai-responses" and host == "api.x.ai" and name.startswith("grok-4")
        )
        if supported or override is True and model.api != "openai":
            return {"type": "web_search"}
    return None


def deepseek_endpoint(model) -> bool:
    """Only official connections may reuse model credentials for DeepSeek search."""
    try:
        address = urlsplit(endpoint(model))
        return (
            address.scheme == "https" and address.hostname == "api.deepseek.com"
            and address.port in (None, 443) and not address.username and not address.password
            and not address.query and not address.fragment
            and address.path.rstrip("/") in {"", "/v1", "/anthropic", "/anthropic/v1"}
            and model.api in {"openai", "openai-responses", "anthropic"}
        )
    except ValueError:
        return False


def offers(model) -> list[WebOffer]:
    result = []
    search = native_tool(replace(model, options={k: v for k, v in model.options.items() if k != "nativeSearch" or v is not False}))
    if search:
        covers = frozenset({"search"})
        controls = {}
        if model.api == "anthropic":
            controls = {"max_uses": ("search", "maxUses")}
        elif model.id.startswith(("gpt-5", "gpt-6", "o3", "grok-4")):
            covers = frozenset({"search", "fetch", "find"})
        result.append(WebOffer("search:" + search["type"], covers, search, controls=controls))
    address = urlsplit(endpoint(model))
    official = (address.scheme == "https" and address.hostname == "api.anthropic.com"
                and address.port in (None, 443) and not address.username and not address.password)
    if model.api == "anthropic" and (model.options.get("nativeFetch") is True or official and re.match(
        r"^claude-(?:opus|sonnet|haiku|fable|mythos)-[4-9]", model.id
    )):
        result.append(WebOffer("fetch:web_fetch_20250910", frozenset({"fetch"}), {
            "type": "web_fetch_20250910", "name": "web_fetch", "citations": {"enabled": True},
        }, controls={"max_uses": ("fetch", "maxUses"), "max_content_tokens": ("fetch", "maxContentTokens")},
            evidence="document_content"))
    if deepseek_endpoint(model):
        result.extend(WebOffer("deepseek:" + capability, frozenset({capability}), backend="deepseek",
                               evidence="document_content" if capability == "fetch" else "actions_and_citations")
                      for capability in ("search", "fetch"))
    return result
