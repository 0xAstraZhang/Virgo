# Migrating to Core + plugins

[Documentation](../README.md) · [Configuration](../reference/configuration.md) · [Boundaries](../development/core-plugin-boundaries.md) · [简体中文](../../zh-CN/guides/migrating-to-core-plugins.md)

## What changes

The base harness keeps model requests, file/shell tools, permissions, context, sessions and diagnostics. Coding-specific capabilities are plugins. Schedule, Workflow, Team, durable Tasks, general Memory and Goal are no longer official features.

Upgrading does not delete or rewrite their data. Saved schedules no longer execute; workflow scripts and memory files are not loaded automatically. Virgo reports retired configuration and the presence of old schedule data. Inspect the paths below before moving any recurring work to an external scheduler.

Official plugins load by default. `--no-extensions` skips user/project Python extensions; it does not disable official plugins or load them.

## Translate settings

```json
{
  "plugins": {
    "web": {"enabled": true},
    "todo": {"enabled": true}
  }
}
```

Only known plugin IDs are accepted. Defaults merge with user settings, then project settings, then runtime/CLI overrides. Legacy `features` values are translated in each input layer before merging; a `plugins` value wins over its legacy equivalent within the same layer. A higher-priority project setting still wins over a user setting, even when written using the legacy spelling. Translation emits a migration notice without rewriting your settings files. MCP is no longer a plugin ID: install the [MCP extension](../../../extensions/README.md); `plugins.mcp` and `features.mcp` are ignored with a notice.

| Legacy setting | New setting / behavior |
| --- | --- |
| `features.skills`, `todo`, `ask_user`, `subagent`, `background` | `plugins.<id>.enabled` |
| `features.diagnostics` | `plugins.diagnostic_get.enabled`; Core diagnostics remain available |
| `features.search`, `features.fetch` | `plugins.web.searchEnabled`, `plugins.web.fetchEnabled`; the `web` plugin groups both capabilities |
| `features.permission`, `plugins.auto_review` | Retired; configure Core `permissions.approval` and explicit grants |
| Removed feature flags and `memory`, `workflow`, `goal` settings | Notice only; no automatic work or storage migration |

Legacy `features.permission` and `plugins.auto_review` are retired. Follow [execution-policy migration](migrating-execution-policy.md) for `permissions.approval` and explicit grants.

`search.maxUses` and `fetch.maxUses` default to `null` rather than three. Configure a positive explicit limit when required. `plugins.web.maxCalls` can bound client search and reading together per calling runtime/user request; operation time and output limits remain. See [Web](web.md).

## Subagent capability migration

The `agent` tool accepts `description`, `prompt` and optional `background`. Removed `type` and `read_only` arguments are rejected. Runtime and ExtensionAPI factories also remove `read_only`. Children inherit the source task execution context at submission. Express inspection goals in the task prompt; select resource limits on the source Sandbox task for technical enforcement. See [execution migration](migrating-execution-policy.md).

## Trust project extensions

Project Python can execute arbitrary code as the user. Neither the Core tool permission gate nor plugin failure isolation is a Python sandbox. Review an unfamiliar project's `.virgo/extensions/*.py` before granting trust.

Use `--trust-project-extensions` to trust the selected project for the session. To persist trust, add its canonical absolute directory to `extensions.trustedProjects` in your **user** settings:

```json
{
  "extensions": {
    "trustedProjects": ["/absolute/path/to/reviewed-project"]
  }
}
```

A project's own settings cannot authorize its extensions. User-level extensions under `<user>/extensions/` are already under the user's control. `--no-extensions` disables both locations even when trust was granted. Do not commit personal trust lists or credentials to a project.

## Inspect historical data without running it

`<user>` means `$VIRGO_HOME` or `~/.virgo`. `<project>` is the working directory; session JSONL files are under `<user>/sessions/<workspace-slug>/`.

| Retired capability | Retained paths / records | Manual replacement |
| --- | --- | --- |
| Schedule | `<project>/.virgo/schedules.json` | Review each prompt, time zone and schedule before configuring cron, a system scheduler or CI |
| Workflow | `<user>/workflows/*.py`, `<project>/.virgo/workflows/*.py`; `<project>/.virgo/runs/*.jsonl` | Move reviewed orchestration to project scripts or CI; successful old agent-call records do not make arbitrary Python side effects safe to replay |
| Tasks | `<project>/.virgo/tasks/*.json` and lock files | Export required records to an issue tracker or maintained project document |
| Memory | `<user>/memory/`, `<project>/.virgo/memory/`, including indexes | Manually select relevant project knowledge for `AGENTS.md`, `docs/` or Skills |
| Team | `<project>/.virgo/worktrees/`; `<session-directory>/<lead-id>-team/*.jsonl` | Use focused subagents; inspect worktrees with Git before deciding whether to keep or remove them |
| Goal | `custom` records with `kind=goal` and auxiliary calls in session JSONL | Keep completion criteria in the task prompt or project acceptance instructions |

Read JSON/JSONL/Markdown as plain files or copy them to an export directory. No migration command executes Python workflows, activates schedules, deletes data or uploads content. Memory may contain personal information: review it before placing anything in Git. Stopping an old Virgo process before upgrading also prevents that old process from continuing its scheduler.

## Existing conversations and removed commands

Session deserialization does not require tools to remain installed. Old tool results can still be rendered and included in context; a new call to an unavailable tool reports that it is unavailable. Historical records never replay an operation automatically.

`/schedule`, `/schedules`, `/workflow`, `/team`, `/tasks`, `/memory` and `/goal` no longer provide those capabilities. `/todos`, `/jobs`, Skills and `agent` remain available when their plugin is enabled. An incomplete Todo does not force a continuation loop; normal agent stopping remains intact.

## Extension authors

Use `virgo.plugins` for official implementation imports and the documented `ExtensionAPI` for integration. The 0.3 release removes the `virgo.features` import aliases; use `virgo.plugins` or the documented `ExtensionAPI`. Removed product subsystems have no compatibility implementation.

Tool and command collisions now fail instead of replacing an existing registration. Register cleanup through `api.on_cleanup`; publish dynamic tool sets through `api.replace_tools`. Capture the caller's runtime/store for asynchronous work, use the runtime factory for children, and do not mutate `session._tools`, `session._lock` or selected UI state. The [extension guide](../development/extensions.md) lists the supported surface.

## Rollback

This change makes no destructive storage migration. To roll back, first stop the newer process, return to the previous Git revision or installed package version, and review scheduled work before starting it: old code can resume the old scheduler. Preserve current session and data files. No automatic reset, clean, migration replay or deletion is required.
