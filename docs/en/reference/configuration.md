# Configuration reference

[Documentation](../README.md) · [简体中文](../../zh-CN/reference/configuration.md)

## Files and precedence

Virgo deep-merges built-in defaults, user settings and project settings, in that order:

1. Defaults in [`harness/settings.py`](../../../src/virgo/harness/settings.py).
2. `~/.virgo/settings.json`, or `$VIRGO_HOME/settings.json`.
3. `<working-directory>/.virgo/settings.json`.
4. Supported environment and runtime/CLI overrides.

Nested objects merge; a higher-priority value replaces the lower-priority value, except that project settings cannot supply execution authority or extension trust. CLI model selection overrides `VIRGO_MODEL`, which overrides settings. `VIRGO_SHELL` overrides the shell selected by user, project and runtime settings. Commands that save a user default preserve unrelated fields; a project or startup override can still win on the next launch. When `<working-directory>/.virgo` is the same directory as the user settings directory (`~/.virgo` or `$VIRGO_HOME`), that file is not read a second time as a project settings layer.

Language follows defaults → user settings → project settings → `--language`/runtime settings. `/language` saves a user preference and applies it now; a failed save keeps the active language. No `VIRGO_LANGUAGE` variable is defined. See [Localization](../development/localization.md) for aliases and live-session behavior.

A minimal project configuration:

```json
{
  "language": "en",
  "effort": "medium",
  "shell": {"executable": "auto"}
}
```

See [examples](../../../examples/README.md) for optional provider and extension files. Keep credentials in the login store or environment rather than in versioned settings.

## Common settings

| Key | Default / meaning |
| --- | --- |
| `plugins.<id>.enabled` | Explicit boolean override of the plugin's default |
| `budget.maxModelCalls` | `null`; optional positive shared parent/child model-request limit |
| `extensions.trustedProjects` | User settings only; canonical absolute paths authorized for project Python |
| `language` | `en`; built-in choices are `en` and `zh-CN` |
| `model` | `anthropic/claude-opus-5-5`; use `provider/model-id` |
| `effort` | `medium`; supported UI levels are `low`, `medium`, `high`, `xhigh`, `max` |
| `maxTokens` | Unset; adapter/model defaults apply |
| `shell.executable` | `auto`; adapted shell name or executable path |
| `permissions.preset` | Sandbox: `workspace-edit` |
| `permissions.presets` | `name/readPaths/writePaths/networkAllowed` |
| `execution.provider` | `host`; `host` or `sandbox` |
| `permissions.approval` | `ask`; use `never` to reject operations needing a new approval |
| `sandbox.executable` | Explicit installed MXC v1.1.0 executor path |
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

## Plugins

The official plugins `skills`, `todo`, `ask_user`, `diagnostic_get`, `subagent`, `background` and `web` are enabled by default. An explicit `plugins.<id>.enabled` value overrides that default. Unknown plugin names and malformed values fail configuration rather than silently enabling something. The [MCP extension](../../../extensions/README.md) is a separate opt-in path, not a plugin ID.

```json
{
  "plugins": {"todo": {"enabled": true}, "web": {"enabled": false}},
  "budget": {"maxModelCalls": 40}
}
```

The example keeps Todo enabled explicitly and turns Web off; every other official plugin keeps its default. Permissions, sessions and diagnostics stay in Core. Python extensions are a separate trusted-code path: use `--no-extensions` to disable them. A project cannot grant itself trust; use `--trust-project-extensions` or add its absolute directory to user `extensions.trustedProjects`.

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

At startup, connected providers refresh their model catalog in the background, at most four concurrently. A successful `/login` also fetches that provider's model directory once, so `/model` is immediately populated; if that fetch fails, Virgo prints a warning. `/model` reads the cached snapshot. Explicit `providers.<id>.models` fields override fetched metadata, which overrides built-in metadata and protocol fallback values. Configuring a metadata entry does not itself add a model to a remote catalog. A failed refresh keeps the previous snapshot.

Catalog caches are scoped to provider, protocol and endpoint and do not store keys. Codex catalog client-version selection reads local npm package manifests (without executing Codex), cached versions and official release metadata; standalone installations without a manifest use the cache or release source; `providers.openai-codex.clientVersion` pins it. `providers.<id>.apiVersion` can pin the Anthropic protocol version. These values govern protocol compatibility, not the Virgo application version.

Auxiliary requests automatically choose a small model on the same connection: `claude-haiku-5-5` for official Anthropic connections, `gpt-6-luna` for official OpenAI/Codex connections, and otherwise the main model. Reasoning prefers `low`, then a supported default, otherwise omits the control. The old `smallModel` field is not consulted.

## Execution and resources

Fresh settings use `execution.provider=host`. Host performs direct file and process operations with the host account's OS rights; it does not load or probe Sandbox and does not review commands. `--execution host|sandbox` explicitly selects the provider. `/permissions show` distinguishes requested and effective scope.

Sandbox is an explicitly selected required extension. Configure an installed MXC v1.1.0 executor with `sandbox.executable`. A missing extension, invalid executable or unsupported policy refuses execution without Host fallback. **Native shared-path execution is currently blocked** by an observed macOS hardlink alias bypass; backend discovery is not isolation evidence. See [acceptance](../development/execution-acceptance.md).

`permissions.preset/presets` belong to Sandbox. Each preset has `name`, `readPaths`, `writePaths` and `networkAllowed`; paths resolve against the source workspace and writable paths are also readable. Built-ins `read-only` and `workspace-edit` grant workspace read and workspace write respectively, both with network allowed. `no-limit` is removed. System runtime dependencies are separate from user grants.

For networking, `networkAllowed: true` requests egress, ingress and host loopback. `false` requests none. An array requests exactly the listed directions. For example, `"networkAllowed": ["egress"]` requests outbound traffic only. Accepted values are `egress`, `ingress` and `hostLoopback`. Managed public Web tools require egress. Native policy support is reported separately. The shared-path native Shell remains unavailable.

```json
{"execution":{"provider":"sandbox"},"sandbox":{"executable":"/absolute/path/to/mxc-exec-mac"},"permissions":{"preset":"offline","approval":"ask","presets":{"offline":{"name":"Offline","readPaths":["."],"writePaths":["."],"networkAllowed":false}}}}
```

`--permission <id>`, `/permissions <id>` and the Sandbox TUI picker select a preset for subsequent runs. `/permissions save <id> <JSON>` saves a user preset. Running, queued and child tasks retain their captured context. `ask|never` controls only explicit resource-increment requests; Host has no resource approval controls. Grants are consumed once for the exact operation and expire after five minutes or run end, whichever comes first. Expiration does not revoke an already running process.

User settings and explicit startup overrides supply authority. Project settings cannot set execution, permissions or extension trust. Explicit legacy isolation settings without an explicit Sandbox selection are a configuration conflict. Nonempty `allow/deny`, old boolean preset fields and child `read_only` arguments produce migration errors. There is no command whitelist or intent classifier. See [migration](../guides/migrating-execution-policy.md) and [resource presets](../development/permission-profiles.md).

Trusted Python extensions, hooks, MCP server startup and model transport remain outside the managed boundary. Sandbox refuses external operations whose requested file scope cannot be enforced; a grant cannot switch to Host. Managed Web and native search check the captured network policy.

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
