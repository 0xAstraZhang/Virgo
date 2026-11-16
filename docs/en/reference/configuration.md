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
| `permissions.preset` | `workspace-edit`; select a user-owned permission preset |
| `permissions.presets` | Editable names and three flags, keyed by stable ID |
| `sandbox.mode` | Optional legacy offline compatibility setting |
| `plan` | `false`; `true` starts the optional planning task |
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

`coding` is the default. It enables `skills`, `todo`, `ask_user`, `diagnostic_get`, `subagent`, `background`, `web`, `plan` and `code_review`. MCP is enabled only when server configuration exists. `minimal` starts with no official capabilities; explicit `plugins.<id>.enabled` overrides either preset. Unknown plugin names and malformed values fail configuration rather than silently enabling something.

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

## Plan task

Plan and Code Review are optional tasks. `/plan <task>` starts planning;
`/plan off` leaves planning without implementing it. `/review` runs an independent
review using the normal tools and fixed review evidence. Neither workflow changes permissions.
Their prompts request a plan/report without making changes; use Read-Only when enforced file
protection is needed. `--plan` selects initial planning instructions, while
`--format text|json` controls output. See [Planning](../guides/planning.md) and
[Code Review](../guides/code-review.md).

## Permissions

`--permission <id>` and `/permissions <id>` select a permission preset. Shift+Tab opens the TUI picker,
where preset names and all three switches can be edited or a new preset created. Selection
is conversation-local; Save persists the preset in user settings. The current task captures
its permissions; changes during a run are shown as the next selection and do not revoke
already running processes. Children/background jobs keep their source workspace and policy.

| Default ID / name | Read only | Workspace only | Network allowed |
| --- | --- | --- | --- |
| `read-only` / Read-Only | true | true | true |
| `workspace-edit` / Workspace-Edit | false | true | true |
| `no-limit` / No-Limit | false | false | true |

Workspace-Edit is the default. Names are editable labels, never permission checks. User settings:

```json
{
  "permissions": {
    "preset": "workspace-edit",
    "presets": {
      "offline-inspection": {
        "name": "Offline inspection",
        "readOnly": true,
        "workspaceOnly": true,
        "networkAllowed": false
      },
      "workspace-edit": {"name": "Daily development"}
    },
    "approval": "ask",
    "deny": ["bash(git push*)"]
  }
}
```

The stable ID is 1–64 letters/digits/underscores/hyphens. `ask`, `never`, `revoke`, `show`, `status`, `sandbox`, `list` and `save` are reserved by `/permissions`. Custom entries require all three
boolean fields; built-in entries may override individual fields. A name is 1–100 printable
characters. `/permissions save <id> <JSON>` provides the same editing path in plain/noninteractive
clients. `/permissions list` lists actual values; `/permissions show` reports the selected/effective policy and
backend discovery separately. Unsupported backend combinations fail closed.

Read-only protects managed host file data, with private scratch and broker records allowed;
it does not prevent remote side effects. Workspace scope includes managed file reads/writes
with system/runtime exceptions. Private Virgo authority storage remains protected in POSIX
profiles and Core file adapters. Windows uses object ACLs with ambient LPAC read grants;
it is not an exact path namespace. Network policy covers managed Shell, Web/native search
and remote MCP invocation, not model transport/login. Trusted Python extensions are outside
the OS boundary. MCP services cannot enforce local file scope and need an explicit unrestricted
single-operation exception under constrained file profiles. See [Sandbox](../guides/sandbox.md).

Managed operations within the profile run without Shell syntax classification. Explicit deny
rules remain binding. `request_permissions(tool, arguments, permissions, reason)` asks for one
exact operation, then the model must call that tool with identical arguments. It does not run
or replay the command, change Mode, grant sibling tasks access, or persist authorization in
history. Approval is bound to the runtime/run, arguments, cwd, resolved paths, tool generation,
shell environment and current policy revision; changes invalidate it. A repeated denied
request does not prompt again until decisions are revoked or a new run starts.

`permissions.approval=ask` permits interactive requests; `never` refuses new requests while
allowing operations already within the profile. `/permissions revoke` clears remembered
approvals/refusals and unconsumed operation exceptions. Additional trusted extension policies
may still ask for authorization. User `allow` rules remain available for those policies but
cannot override an execution boundary or explicit deny. Shell allow patterns must match an
exact full command or a whole tool; deny and path rules support glob patterns.

Only user settings and explicit startup overrides may select/edit modes or grant permissions.
Project `permissions` accepts only additional `deny` rules; it cannot clear user denials.
Legacy explicit `sandbox.mode=read-only|workspace-write` / `--sandbox` preserve their offline
behavior; `disabled` maps to No-Limit. Remove the legacy setting to use network-enabled defaults.
Do not combine `--permission` and `--sandbox`. `sandbox.network`, model permission approval and old
approval-mode aliases remain unsupported.

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
