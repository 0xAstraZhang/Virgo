# Web search and reading

[Documentation](../README.md) · [简体中文](../../zh-CN/guides/web.md)

Virgo can search for sources and read public webpages. The two capabilities have separate settings and permissions. Routing depends on the model, protocol, endpoint and enabled features; a provider name alone does not enable a native capability on an arbitrary proxy.

## Choose a search backend

```text
/search auto
/search duckduckgo
/search deepseek
/search off
```

`/search` without arguments opens the selector. `ddg` and `ds` are aliases for the two explicit backends. The choice takes effect immediately and is saved to user settings without changing the conversation model.

| Choice | Behavior |
| --- | --- |
| `auto` | Prefer an adapted native capability; otherwise use DuckDuckGo |
| `duckduckgo` | Use the built-in DuckDuckGo HTML search backend |
| `deepseek` | Use the official DeepSeek Search API with an available key |
| `off` | Disable search |

The adapters include native search for supported Anthropic, OpenAI Responses/Codex and xAI connections, plus the separate DeepSeek Search API. Google Code Assist and unknown compatible connections use the built-in fallback by default. A compatible protocol does not guarantee a native feature.

DeepSeek search can be used with another conversation model. Configure `/login api deepseek` or `DEEPSEEK_API_KEY` first; an explicit selection fails without a usable official connection and retains the previous choice. `search.apiKeyEnv` can select a dedicated key environment variable. DuckDuckGo needs no key, but can rate-limit or require browser verification; Virgo reports that condition as an error.

```json
{
  "search": {
    "mode": "auto",
    "backend": "duckduckgo",
    "timeout": 15,
    "maxUses": null
  }
}
```

`mode` accepts `auto`, `native`, `builtin` or `off`. `native` fails when no allowed native route exists; `builtin` uses the selected backend. `plugins.web.searchEnabled=false` disables the feature entirely. Built-in search accepts `query` and optional `limit` (default 5, maximum 10). Query limits are 128 Unicode characters for DeepSeek and 2000 for DuckDuckGo. The built-in response budget is 1 MiB and timeout is at most 30 seconds. `maxUses` is `null` by default (no Virgo count cap), or a positive integer. Client calls enforce the configured per-user-request operation budget. Native `max_uses` declarations apply per provider API request, not across all model turns; omitting one means Virgo imposes no count cap, not that the provider has no limits.

## Choose a reading backend

```text
/fetch auto
/fetch http
/fetch deepseek
/fetch off
```

The selector saves user settings in the same way as `/search`. Auto mode prefers adapted native reading: a standalone fetch capability or a combined search/browsing capability. When the selected native tool already provides reading, Virgo does not also expose a redundant HTTP fetch tool.

```json
{
  "fetch": {
    "mode": "auto",
    "backend": "http",
    "timeout": 20,
    "maxBytes": 2097152,
    "maxUses": null,
    "maxContentTokens": 8000
  }
}
```

`mode` accepts `auto`, `native`, `builtin` or `off`; `backend` is `http` or `deepseek`. `plugins.web.fetchEnabled=false` disables reading. The timeout must be positive and at most 60 seconds; `maxBytes` is capped at 16 MiB. `maxUses` is `null` or a positive integer. Native providers apply supported limits; a combined tool may not expose a separate reading-count limit.

The client `web_fetch` tool accepts a `url` and returns source metadata, title, text, backend, retrieval time and truncation state. The visible body is bounded to 32 KiB / 2000 lines. When truncated, the retrieved body is saved under `.virgo/outputs/`, with an `output_path` for further reading. A retrieval timestamp does not prove that an upstream cache was refreshed.

The built-in HTTP backend reads HTML, plain text and Markdown. It does not execute JavaScript, use browser sessions or parse PDFs. It validates public addresses, binds connections to a checked IP with hostname-based TLS verification, and rechecks redirects. It does not load cookies, netrc credentials or environment proxies. Defaults allow at most five redirects, 20 seconds and 2 MiB before and after decompression.

DeepSeek Fetch uses an official saved connection, `DEEPSEEK_API_KEY`, or the separate `fetch.apiKeyEnv`. It does not reuse `search.apiKeyEnv` implicitly or forward a proxy credential to the official endpoint.

## Shared request budget

Search and fetch are parts of the `web` plugin. `plugins.web.enabled=false` disables both, including imports and registrations. Set `searchEnabled` or `fetchEnabled` independently when only one capability is needed.

```json
{
  "plugins": {"web": {"enabled": true, "maxCalls": 12}},
  "search": {"maxUses": null, "timeout": 15},
  "fetch": {"maxUses": null, "timeout": 20, "maxBytes": 2097152, "maxContentTokens": 8000}
}
```

`maxCalls` is optional (`null` by default) and must be a positive integer when set. It bounds the combined client search/fetch calls per calling runtime and user request. Timeouts and output budgets still apply to individual operations; no default three-call limit remains. A child has its own web-operation counter; Core `budget.maxModelCalls` separately limits model requests shared by parent and children.

Hosted operations do not expose a reliable shared search/fetch count. An explicit shared call budget therefore requires client tools: automatic routing selects the client path, and native-only routing reports the conflict. This preserves an enforceable configured limit instead of pretending to count hidden provider calls.

## Permissions and failures

Built-in tools expose the actual query or URL to permission checks. Native server-side tools may need approval for the user's request scope before the provider chooses individual searches. Query/URL-specific rules and custom tool hooks can require the client path. `auto` can choose that path when a native scope is unavailable.

Search and reading remain independent. Turning reading off, forcing HTTP, or requiring URL-level approval excludes combined tools that cannot honor those restrictions. Turning search off can still leave standalone reading available. `providers.<id>.nativeSearch` and `nativeFetch` can disable native capabilities for a connection; enabling them explicitly requires an actually compatible protocol and endpoint.

Fallback happens only on an explicit unsupported-capability response, and is bounded. Authentication failures, rate limits, timeouts and empty results do not silently switch providers. If a server-side operation has already begun, Virgo retains the evidence and stops automatic replay. Sources and readable citations remain in session history across model changes.

Implementation: [capability types](../../../src/virgo/ai/web.py), [routing](../../../src/virgo/plugins/web.py), [search](../../../src/virgo/plugins/search.py), [fetch](../../../src/virgo/plugins/fetch.py).
