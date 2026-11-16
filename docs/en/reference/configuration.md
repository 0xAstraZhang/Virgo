# Configuration reference

[Documentation](../README.md) · [简体中文](../../zh-CN/reference/configuration.md)

## Files and precedence

Virgo deep-merges built-in defaults, user settings and project settings, in that order:

1. Defaults in [`harness/settings.py`](../../../src/virgo/harness/settings.py).
2. `~/.virgo/settings.json`, or `$VIRGO_HOME/settings.json`.
3. `<working-directory>/.virgo/settings.json`.
4. Supported environment and runtime/CLI overrides.

Nested objects merge; a higher-priority value replaces the lower-priority value, except that project authorization settings may only add deny rules. User deny rules cannot be removed by a project. CLI model selection overrides `VIRGO_MODEL`, which overrides settings. `VIRGO_SHELL` overrides the shell selected by user, project and runtime settings. Commands that save a user default preserve unrelated fields; a project or startup override can still win on the next launch.

Language follows defaults → user settings → project settings → `--language`/runtime settings. `/language` saves a user preference and applies it now; a failed save keeps the active language. No `VIRGO_LANGUAGE` variable is defined. See [Localization](../development/localization.md) for aliases and live-session behavior.

A minimal project configuration:

```json
{
  "language": "en",
  "effort": "medium",
  "shell": {"executable": "auto"},
  "permissions": {
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
| `sandbox.mode` | `workspace-write`; choose `read-only` or explicitly select `disabled`, user/startup configuration only |
| `workMode` | `default`; `plan` adds an independent read-only ceiling |
| `permissions.approval` | `ask`; use `never` to reject operations needing a new approval |
| `permissions.allow`, `permissions.deny` | Lists of tool rules |
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

`coding` is the default. It enables `skills`, `todo`, `ask_user`, `diagnostic_get`, `subagent`, `background`, `web` and `code_review`. MCP is enabled only when server configuration exists. `minimal` starts with no official capabilities; explicit `plugins.<id>.enabled` overrides either preset. Unknown plugin names and malformed values fail configuration rather than silently enabling something.

```json
{
  "profile": "minimal",
  "plugins": {"skills": {"enabled": true}, "todo": {"enabled": true}},
  "budget": {"maxModelCalls": 40}
}
```

The example selects only Skills and Todo from the official set. Permissions, sessions and diagnostics stay in Core. Python extensions are a separate trusted-code path: use `--no-extensions` to disable them. A project cannot grant itself trust; use `--trust-project-extensions` or add its absolute directory to user `extensions.trustedProjects`.

Legacy `features` keys are translated per configuration layer with a notice; same-layer `plugins` values win, then normal user/project/runtime precedence applies. `features.permission`, `plugins.auto_review` and the old permission modes are retired and report a migration error. Removed products no longer execute and their files are retained. See [Migration](../guides/migrating-to-core-plugins.md) for aliases, storage locations and the compatibility window.

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

## Work mode

`/mode plan` enters planning and `/mode default` returns to ordinary work without executing a proposal. `--work-mode default|plan` selects the initial conversation's intent; `--format text|json` controls output formatting. These are separate from `--approval ask|never` and `--sandbox`. See [Planning a change](../guides/planning.md).

```json
{
  "workMode": "plan",
  "permissions": {"approval": "ask"},
  "sandbox": {"mode": "workspace-write"}
}
```

The example belongs in user settings. Project settings may request Plan but cannot relax a user-configured Plan ceiling. A user may explicitly select a different startup mode. Existing conversations keep their own saved mode; new conversations use the configured default. Mode changes require that originating conversation and its queued/running descendants to be idle. Notifications observe committed state and cannot reenter mode, plan or runtime admission changes.

Plan and read-only children expose read-only tools, including controlled `git_read`, instead of arbitrary Bash/PowerShell. A child's read-only capability is independent of planning intent: a read-only investigation or Code Review is not automatically given Plan instructions. Children inherit their originating workspace and can only narrow their parent's capability and approval ceiling.

Plans keep versioned Markdown in SessionStore. `/plan execute <id>:<revision>` selects the exact version for an execution request; saving or reopening a proposal does not execute it. Execution preserves approval and Sandbox restrictions. Returning to Default cannot make a configured read-only sandbox writable. See [the migration guide](../guides/migrating-execution-policy.md) for retired mode APIs.

## Permissions

| Approval policy | Behavior |
| --- | --- |
| `ask` | Execute already authorized operations; ask a person for uncertain or ungranted operations |
| `never` | Execute already authorized operations; reject anything needing a new approval |

Core authorization is always installed. No model infers a grant from conversation history, tool output or summaries. Both choices retain explicit deny rules, read-only ceilings and configured Sandbox boundaries. `/permissions ask` and `/permissions never` select the policy; `/permissions revoke` clears remembered grants and denials. These commands do not alter WorkMode or Sandbox.

Workspace file operations use the deterministic path policy. Simple local Shell forms can be allowed automatically only with an active native sandbox; classification is a convenience, not proof of confinement. Unisolated host Shell, network, outside-workspace and unknown operations need an explicit rule or human approval. A failed sandbox does not authorize a host retry. Without an interactive approver, an unresolved request fails closed.

Only user settings and explicit startup overrides can grant permissions or select the approval policy. Project `permissions` accepts only `deny`, merged with user denials. Project `allow`, `approval`, old `mode` or `readOnlyCommands` entries report a configuration error. A project cannot erase the user's deny list with an empty list.

Rules use `tool(pattern)` or a tool-name pattern alone. Path and web rules use fnmatch patterns. Shell allow rules match an **exact complete command**, and wildcard command patterns are rejected; use a tool-only `bash` or `powershell` grant only when that entire capability is intentionally authorized. Shell deny patterns may remain broad. Examples for user settings:

```json
{
  "permissions": {
    "approval": "ask",
    "allow": ["bash(uv run --no-sync pytest -q)", "read(/opt/reference/**)"],
    "deny": ["bash(git push*)", "read(~/.ssh/*)"]
  }
}
```

Deny and capability checks precede grants and questions. Approval is checked again immediately before execution after argument changes. A remembered approval binds the originating task, exact arguments, cwd, statically resolved path targets, shell environment and explicit policy state. Dynamic Shell paths and PowerShell scripts receive only one-shot approval. Policy changes or `/permissions revoke` invalidate remembered decisions; a new natural-language message alone is not an authorization-system mutation.

Audit records retain the source task, work mode, read-only ceiling, approval policy, decision and argument digest. Permission-model risk and evidence records are retired. These application checks are separate from the [native Shell sandbox](../guides/sandbox.md), which does not isolate trusted Python extensions or remote tools.

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
