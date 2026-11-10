"""Login presets, adapted from Pi's provider catalog and provider API documentation.

Account credentials use separate provider IDs when their API differs from the
public API. In particular, Kimi Code subscriptions and Moonshot API keys must
never share an endpoint or credential entry.
"""

AUTH_PRESETS = {
    "openai-codex": {
        "name": "OpenAI",
        "model": "gpt-6.1-terra",
        "config": {
            "api": "openai-codex",
            "baseUrl": "https://chatgpt.com/backend-api/codex",
            "models": {"gpt-6.1-terra": {"contextWindow": 500_000, "maxTokens": 128_000, "reasoning": True}},
        },
    },
    "anthropic": {
        "name": "Anthropic",
        "model": "claude-opus-5-5",
        "config": {
            "api": "anthropic",
            "baseUrl": "https://api.anthropic.com",
            "apiKeyEnv": "ANTHROPIC_API_KEY",
        },
    },
    "xai": {
        "name": "xAI",
        "model": "grok-4.3",
        "config": {
            "api": "openai-responses",
            "baseUrl": "https://api.x.ai/v1",
            "apiKeyEnv": "XAI_API_KEY",
            "models": {"grok-4.3": {"contextWindow": 1_000_000, "maxTokens": 30_000, "reasoning": True}},
        },
    },
    "google-gemini-cli": {
        "name": "Google",
        "model": "gemini-2.5-pro",
        "config": {
            "api": "google-gemini-cli",
            "baseUrl": "https://cloudcode-pa.googleapis.com",
            "models": {
                "gemini-2.5-pro": {"contextWindow": 1_048_576, "maxTokens": 65_536, "reasoning": True}
            },
        },
    },
    "kimi-coding": {
        "name": "Kimi",
        "model": "kimi-for-coding",
        "config": {
            "api": "openai",
            "baseUrl": "https://api.kimi.com/coding/v1",
            "apiKeyEnv": "KIMI_API_KEY",
            "sendReasoning": True,
            "reasoningEffortMap": {"low": "low", "medium": None, "high": "high", "xhigh": None, "max": "max"},
            "models": {
                "kimi-for-coding": {"contextWindow": 1_048_576, "maxTokens": 32_768, "reasoning": True}
            },
        },
    },
}

API_PRESETS = {
    "deepseek": {
        "name": "DeepSeek",
        "model": "deepseek-flash",
        "config": {
            "api": "openai",
            "baseUrl": "https://api.deepseek.com",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "sendReasoning": True,
            "reasoningEffortMap": {"low": "low", "medium": None, "high": "high", "xhigh": None, "max": "max"},
            "models": {
                "deepseek-flash": {"contextWindow": 1_000_000, "maxTokens": 32_768, "reasoning": True}
            },
        },
    },
    "glm": {
        "name": "GLM",
        "model": "glm-5.3",
        "config": {
            "api": "openai",
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
            "apiKeyEnv": "ZAI_API_KEY",
            "sendReasoning": True,
            "reasoningEffortMap": {"low": "low", "medium": None, "high": "high", "xhigh": None, "max": "max"},
            "models": {
                "glm-5.3": {
                    "contextWindow": 1_000_000,
                    "maxTokens": 32_768,
                    "reasoning": True,
                    "vision": False,
                }
            },
        },
    },
    "kimi": {
        "name": "Kimi",
        "model": "kimi-k3.1-pro",
        "config": {
            "api": "openai",
            "baseUrl": "https://api.moonshot.cn/v1",
            "apiKeyEnv": "MOONSHOT_API_KEY",
            "sendReasoning": True,
            "reasoningEffortMap": {"low": "low", "medium": None, "high": "high", "xhigh": None, "max": "max"},
            "models": {"kimi-k3.1-pro": {"contextWindow": 1_048_576, "maxTokens": 32_768, "reasoning": True}},
        },
    },
}
