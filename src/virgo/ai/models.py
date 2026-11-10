"""Model resolution: "provider/model-id" -> Model.

Built-in providers: anthropic, openai, faux. Any OpenAI-compatible endpoint can be added in
settings, e.g.::

    "providers": {
      "deepseek": {"api": "openai", "baseUrl": "https://api.deepseek.com",
                    "apiKeyEnv": "DEEPSEEK_API_KEY",
                    "models": {"deepseek-chat": {"contextWindow": 128000}}}
    }
"""

from __future__ import annotations

import re

from .types import Model

BUILTIN_PROVIDERS = {
    "anthropic": {"api": "anthropic", "apiKeyEnv": "ANTHROPIC_API_KEY", "baseUrlEnv": "ANTHROPIC_BASE_URL"},
    "openai": {"api": "openai", "apiKeyEnv": "OPENAI_API_KEY", "baseUrlEnv": "OPENAI_BASE_URL"},
    "faux": {"api": "faux"},
}

# Known model metadata (context window, max output). Unknown ids get provider defaults.
KNOWN_MODELS = {
    "claude-fable-5-2": {"contextWindow": 1_000_000, "maxTokens": 128_000},
    "claude-fable-5-1": {"contextWindow": 1_000_000, "maxTokens": 128_000},
    "claude-opus-5-5": {"contextWindow": 1_000_000, "maxTokens": 128_000},
    "claude-opus-5": {"contextWindow": 1_000_000, "maxTokens": 128_000},
    "claude-opus-4-8": {"contextWindow": 1_000_000, "maxTokens": 128_000},
    "claude-sonnet-5-5": {"contextWindow": 1_000_000, "maxTokens": 128_000},
    "claude-sonnet-5": {"contextWindow": 1_000_000, "maxTokens": 128_000},
    "claude-sonnet-4-6": {"contextWindow": 1_000_000, "maxTokens": 128_000},
    "claude-haiku-5-5": {"contextWindow": 1_000_000, "maxTokens": 128_000},
    "claude-haiku-4-5": {"contextWindow": 200_000, "maxTokens": 64_000, "reasoning": False},
}

_ADAPTIVE_CLAUDE = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(4-[6-9]|5)")
_OPENAI_REASONING = re.compile(r"^(o\d|gpt-5)")


def _guess_provider(model_id: str) -> str:
    if model_id.startswith("claude"):
        return "anthropic"
    if model_id.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    return "anthropic"


def resolve_model(spec: str, providers: dict | None = None) -> Model:
    providers = providers or {}
    if "/" in spec:
        provider, model_id = spec.split("/", 1)
    else:
        provider, model_id = _guess_provider(spec), spec
    conf = dict(BUILTIN_PROVIDERS.get(provider, {}))
    conf.update(providers.get(provider, {}))
    if not conf:
        raise ValueError(f"unknown provider {provider!r} (configure it under settings.providers)")
    api = conf.get("api", "openai")
    meta: dict = {}
    if api == "anthropic":
        meta = {
            "contextWindow": 200_000,
            "maxTokens": 64_000,
            "reasoning": bool(_ADAPTIVE_CLAUDE.search(model_id)),
        }
    elif api == "openai":
        meta = {
            "contextWindow": 128_000,
            "maxTokens": 16_384,
            "reasoning": bool(_OPENAI_REASONING.search(model_id)),
        }
    else:
        meta = {"contextWindow": 200_000, "maxTokens": 8_192}
    meta.update(KNOWN_MODELS.get(model_id, {}))
    meta.update((conf.get("models") or {}).get(model_id, {}))
    return Model(
        provider=provider,
        id=model_id,
        api=api,
        base_url=conf.get("baseUrl"),
        api_key_env=conf.get("apiKeyEnv"),
        context_window=int(meta.get("contextWindow", 200_000)),
        max_tokens=int(meta.get("maxTokens", 32_000)),
        reasoning=bool(meta.get("reasoning", False)),
        vision=bool(meta.get("vision", True)),
        options={k: v for k, v in conf.items() if k not in ("api", "models", "baseUrl", "apiKeyEnv")},
    )
