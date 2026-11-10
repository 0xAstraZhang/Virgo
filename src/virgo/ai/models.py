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

from .auth_presets import API_PRESETS, AUTH_PRESETS
from .types import Model

BUILTIN_PROVIDERS = {
    **{k: v["config"] for k, v in {**AUTH_PRESETS, **API_PRESETS}.items()},
    "anthropic": {"api": "anthropic", "apiKeyEnv": "ANTHROPIC_API_KEY", "baseUrlEnv": "ANTHROPIC_BASE_URL"},
    "openai": {"api": "openai", "apiKeyEnv": "OPENAI_API_KEY", "baseUrlEnv": "OPENAI_BASE_URL"},
    "faux": {"api": "faux"},
}

# Known model metadata (context window, max output). Unknown ids get provider defaults.
KNOWN_MODELS = {
    "gpt-6-luna": {
        "contextWindow": 1_050_000,
        "maxTokens": 128_000,
        "reasoning": True,
        "supportedReasoningLevels": ("none", "low", "medium", "high", "xhigh", "max"),
        "defaultReasoningLevel": "medium",
    },
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
_OPENAI_REASONING = re.compile(r"^(o\d|gpt-[56])")


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
        if meta["reasoning"]:
            meta["supportedReasoningLevels"] = ("low", "medium", "high")
    elif api in ("openai", "openai-codex", "openai-responses"):
        meta = {
            "contextWindow": 128_000,
            "maxTokens": 16_384,
            "reasoning": bool(_OPENAI_REASONING.search(model_id)),
        }
    else:
        meta = {"contextWindow": 200_000, "maxTokens": 8_192}
    meta.update(KNOWN_MODELS.get(model_id, {}))
    model_config = (conf.get("models") or {}).get(model_id, {})
    meta.update(model_config)
    levels = meta.get("supportedReasoningLevels")
    if levels is not None:
        if not isinstance(levels, (list, tuple)) or any(not isinstance(level, str) or not level for level in levels):
            raise ValueError(f"invalid supportedReasoningLevels for {spec!r}")
        levels = tuple(dict.fromkeys(levels))
    default_level = meta.get("defaultReasoningLevel")
    if default_level is not None and (not isinstance(default_level, str) or not default_level):
        raise ValueError(f"invalid defaultReasoningLevel for {spec!r}")
    reasoning = bool(meta.get("reasoning", False)) or bool(levels) or default_level is not None
    if model_config.get("reasoning") is False:
        reasoning = False
    if not reasoning:
        levels, default_level = (), None
    elif api == "openai" and isinstance(effort_map := conf.get("reasoningEffortMap"), dict):
        supported = tuple(key for key, value in effort_map.items() if isinstance(key, str) and key == value)
        levels = supported if levels is None else tuple(level for level in levels if level in supported)
    elif levels is None and api == "google-gemini-cli" and model_id.startswith("gemini-"):
        levels = ("low", "medium", "high")
    if levels is not None and default_level not in levels:
        default_level = None
    return Model(
        provider=provider,
        id=model_id,
        api=api,
        base_url=conf.get("baseUrl"),
        api_key_env=conf.get("apiKeyEnv"),
        context_window=int(meta.get("contextWindow", 200_000)),
        max_tokens=int(meta.get("maxTokens", 32_000)),
        reasoning=reasoning,
        vision=bool(meta.get("vision", True)),
        options={k: v for k, v in conf.items() if k not in ("api", "models", "baseUrl", "apiKeyEnv")},
        supported_reasoning_levels=levels,
        default_reasoning_level=default_level,
    )
