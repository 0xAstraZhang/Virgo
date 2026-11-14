# Configuration reference

[Documentation](../README.md) · [简体中文](../../zh-CN/reference/configuration.md)

## Files and precedence

Virgo deep-merges built-in defaults, user settings and project settings, in that order:

1. Defaults in [`harness/settings.py`](../../../src/virgo/harness/settings.py).
2. `~/.virgo/settings.json`, or `$VIRGO_HOME/settings.json`.
3. `<working-directory>/.virgo/settings.json`.
4. Supported environment and runtime/CLI overrides.

Nested objects merge; a higher-priority value replaces the lower-priority value. CLI model selection overrides `VIRGO_MODEL`, which overrides settings. `VIRGO_SHELL` overrides the shell selected by user, project and runtime settings. Commands that save a user default preserve unrelated fields; a project or startup override can still win on the next launch.

Language follows defaults → user settings → project settings → `--language`/runtime settings. `/language` saves a user preference and applies it now; a failed save keeps the active language. No `VIRGO_LANGUAGE` variable is defined. See [Localization](../development/localization.md) for aliases and live-session behavior.

A minimal project configuration:

```json
{
  "language": "en",
  "effort": "medium",
  "shell": {"executable": "auto"},
  "permissions": {
    "mode": "auto-review",
    "allow": ["bash(uv run pytest*)"],
    "deny": ["bash(git push*)"]
  }
}
```

See [examples](../../../examples/README.md) for optional provider and extension files. Keep credentials in the login store or environment rather than in versioned settings.

## Common settings

| Key | Default / meaning |
| --- | --- |
| `profile` | `coding`; use `minimal` for no preset capabilities |
| `plugins.<id>.enabled` | Explicit boolean override of the preset |
| `budget.maxModelCalls` | `null`; optional positive shared parent/child model-request limit |
| `extensions.trustedProjects` | User settings only; canonical absolute paths authorized for project Python |
| `language` | `en`; built-in choices are `en` and `zh-CN` |
| `model` | `anthropic/claude-opus-5-5`; use `provider/model-id` |
| `effort` | `medium`; supported UI levels are `low`, `medium`, `high`, `xhigh`, `max` |
| `maxTokens` | Unset; adapter/model defaults apply |
| `shell.executable` | `auto`; adapted shell name or executable path |
| `permissions.mode` | `auto-review` |
| `permissions.allow`, `permissions.deny` | Lists of tool rules |
| `permissions.readOnlyCommands` | Additional commands considered for read-only execution |
| `compaction.enabled` | `true` |
| `compaction.triggerRatio` | `0.8` of the model's context window |
| `compaction.triggerTokens` | Unset; when supplied, takes precedence over the ratio |
| `compaction.keepRecentTokens` | `20000` for summary/Anthropic recent-context retention |
| `retry.maxRetries` | `3` |
| `retry.maxWaitSeconds` | `120` seconds of cumulative retry waiting per logical model call |
| `retry.fallbackModel` | Unset; optional overload fallback |
| `askUser.unattendedTimeout` | `300` seconds for questions in unattended runs |
| `subagents.maxTurns` | `50` |


Web-specific fields are documented in [Web search and reading](../guides/web.md). Shell-specific precedence and platform behavior are in [Command shells](../guides/shells.md).

## Profiles and plugins

`coding` is the default. It enables `skills`, `todo`, `ask_user`, `diagnostic_get`, `subagent`, `background`, `web` and `auto_review`. MCP is enabled only when server configuration exists. `minimal` starts with no official capabilities; explicit `plugins.<id>.enabled` overrides either preset. Unknown plugin names and malformed values fail configuration rather than silently enabling something.

```json
{
  "profile": "minimal",
  "plugins": {"skills": {"enabled": true}, "todo": {"enabled": true}},
  "budget": {"maxModelCalls": 40}
}
```

The example selects only Skills and Todo from the official set. Permissions, sessions and diagnostics stay in Core. Python extensions are a separate trusted-code path: use `--no-extensions` to disable them. A project cannot grant itself trust; use `--trust-project-extensions` or add its absolute directory to user `extensions.trustedProjects`.

Legacy `features` keys are translated per configuration layer with a notice; same-layer `plugins` values win, then normal user/project/runtime precedence applies. `features.permission=false` only disables the automatic reviewer, never the Core guard. Removed products no longer execute and their files are retained. See [Migration](../guides/migrating-to-core-plugins.md) for aliases, storage locations and the compatibility window.

## Models and credentials

Provider entries can declare `api`, `baseUrl`, `apiKeyEnv` and model metadata:

```json
{
  "providers": {
    "local": {
      "api": "openai",
      "baseUrl": "http://localhost:11434/v1",
      "models": {
        "your-installed-model": {"contextWindow": 32768}
      }
    }
  }
}
```

Select it with `/model local/your-installed-model`. Replace the model ID and context window with values appropriate to your endpoint. `api: "openai"` uses Chat Completions; `openai-responses` selects Responses; `anthropic` selects Messages. Account presets configure their own adapter and authentication metadata.

At startup, connected providers refresh their model catalog in the background, at most four concurrently. `/model` reads the cached snapshot. Explicit `providers.<id>.models` fields override fetched metadata, which overrides built-in metadata and protocol fallback values. Configuring a metadata entry does not itself add a model to a remote catalog. Google Code Assist currently uses direct model selection rather than an adapted live catalog. A failed refresh keeps the previous snapshot.

Catalog caches are scoped to provider, protocol and endpoint and do not store keys. Codex catalog client-version selection uses local/cached/release metadata; `providers.openai-codex.clientVersion` pins it. `providers.<id>.apiVersion` can pin the Anthropic protocol version. These values govern protocol compatibility, not the Virgo application version.

Auxiliary requests automatically choose a small model on the same connection: `claude-haiku-5-5` for official Anthropic connections, `gpt-6-luna` for official OpenAI/Codex connections, and otherwise the main model. Reasoning prefers `low`, then a supported default, otherwise omits the control. The old `smallModel` field is not consulted.

## Permissions

| Mode | Behavior |
| --- | --- |
| `auto-review` | Allows operations whose workspace boundary can be established; reviews external, network and uncertain operations with the small model |
| `accept-all` | Skips permission review and allow/deny rules |
| `plan` | Exposes read-only investigation tools and restricts command execution |

The `auto_review` plugin supplies model review. Without it, the Core retains rules and requires human confirmation for uncertain operations. Noninteractive sessions cannot approve those requests. Auto-review distinguishes authorization from risk. Its verdict is `allow`, `ask` or `deny`. Missing evidence or review failure requires the existing human confirmation; noninteractive runs cannot approve that prompt. An explicit denial blocks the operation. Execution is checked again immediately before the tool runs, after argument changes. These are application-level checks; Virgo does not provide an operating-system sandbox.

Rules use `tool(pattern)` with fnmatch-style patterns, or a tool-name pattern alone. Path tools match paths; command tools match commands; web tools match queries/URLs. Examples include `edit(src/**)`, `read(~/.ssh/*)`, `bash(npm test*)` and `mcp__server__*`. PowerShell uses separate `powershell(...)` rules. See [`harness/permission_policy.py`](../../../src/virgo/harness/permission_policy.py) for ordering and [`permission_scope.py`](../../../src/virgo/harness/permission_scope.py) for boundary detection.

## Context and retries

All providers use the configured compaction threshold. Supported official OpenAI/Anthropic connections use native compaction; other connections use a structured continuation summary. Full session history remains available. Native context is bound to compatible model/system/tool settings and is rebuilt from history when needed after a change. `keepRecentTokens` applies where the provider's compaction path retains a recent segment; it does not trim an OpenAI-returned full window.

Retries use exponential backoff with jitter and honor valid `Retry-After` as a minimum wait. The wait budget excludes request execution time. If the next wait exceeds the remaining budget, Virgo stops retrying rather than shortening the server's requested delay. See [Troubleshooting](../guides/troubleshooting.md) for recovery limits.

## Environment and stored files

| Name or path | Purpose |
| --- | --- |
| `VIRGO_HOME` | Override the user data directory, normally `~/.virgo` |
| `VIRGO_MODEL` | Override the configured model |
| `VIRGO_SHELL` | Override the configured command shell |
| `NO_COLOR` | Disable plain-terminal colors |
| `VIRGO=1` | Marker supplied to tool subprocesses |
| `<user>/auth.json` | Saved credentials |
| `<user>/sessions/` | Workspace session history |
| `<user>/model-metadata/`, `<user>/cache/` | Cached connection/model metadata |
| `<user>/logs/` | Diagnostic logs, also used with `--no-session` |
| `<project>/.virgo/outputs/` | Full tool output logs and retrieved text |

Historical Schedule/Workflow/Team/Tasks/Memory/Goal files are preserved without execution; locations and manual export are in [Migration](../guides/migrating-to-core-plugins.md#inspect-historical-data-without-running-it).

Project instructions come from user `AGENTS.md` and ancestor directories' `AGENTS.md` or `CLAUDE.md`, with `AGENTS.md` preferred at the same level. User/project `.virgo/SYSTEM.md` can replace the identity section, and `APPEND_SYSTEM.md` appends instructions. Skills, prompts, agents, extensions and MCP configuration have separate resource locations described in [Extensions](../development/extensions.md).
