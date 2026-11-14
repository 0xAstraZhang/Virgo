# Tools and plugins

[Documentation](../README.md) · [简体中文](../../zh-CN/reference/tools.md)

The model receives the tools enabled for its role, configuration and current capabilities. The exact list can change with extensions, MCP connections and web routing. Tool names, argument names and enum values remain stable across interface languages.

Core always provides the file and selected-shell tools. `web`, `todo`, `ask_user`, `subagent`, `background`, `mcp` and `diagnostic_get` supply the optional tools below. Use the default `coding` preset, or select capabilities explicitly through `plugins.<id>.enabled`. `--profile minimal --no-extensions` exposes only Core tools. See [Configuration](configuration.md#profiles-and-plugins).

## Files, commands and web

| Tool | Use |
| --- | --- |
| `read` | Read text or supported images; use `offset` and `limit` for larger files |
| `write` | Create or replace a file |
| `edit` | Replace an exact text match in a file |
| `glob` | Find paths by pattern |
| `grep` | Search file content |
| `bash` / `powershell` | Execute through the selected command shell |
| `web_search` | Search through an exposed client backend |
| `web_fetch` | Read a webpage through an exposed client backend |

Reads and tool output have budgets; long results include a continuation or saved output path. File writes to the same file are serialized. Native web providers may expose server-side tools instead of the client names above. See [shells](../guides/shells.md) and [web capabilities](../guides/web.md).

## Checklists and questions

`todo_write` replaces the session checklist with the complete supplied list. Statuses are `pending`, `in_progress` and `completed`; multiple items can be in progress. Invalid input leaves the old list intact. `/todos` displays it. An incomplete checklist does not itself force another model turn.

`ask_user` lets the main agent ask a question with up to three options, plus custom input, or free-form input alone. Noninteractive, cancelled and timed-out questions report `answered=false` without inventing an answer. Unattended runs use `askUser.unattendedTimeout`; a user joining the conversation can cancel that timeout.

## Focused subagents

The `agent` tool runs a focused task with an independent context and returns its final answer. Built-in types are `general` and read-only `explore`; custom types live in `agents/*.md`. Subagents do not recursively spawn subagents and default to 50 turns. Their earlier tool calls may have had effects even if the final response fails. Children inherit the caller's permission ceiling and cancellation; `budget.maxModelCalls` can bound shared parent/child model requests. Children cannot gain authority by choosing a different role or custom agent type.

## Background jobs

Background shell commands and subagents share the job manager. `/jobs` lists them; `job_output` reads output and `job_kill` cancels a job. Completion notifications stay bound to the originating conversation. They can steer a running agent or wake an idle one, without moving results into the conversation currently selected in the UI.

## MCP and diagnostics

Without configured servers, the default preset starts no MCP connection and registers no MCP management tools. An explicitly enabled MCP plugin can expose management even before servers are added.

Configure stdio MCP servers in user or project `mcp.json`, or the `mcpServers` setting. Server arguments and environment entries can use `${VARIABLE}` interpolation. A server's tools appear as `mcp__<server>__<tool>`. Disabled entries are not started. Server and extension descriptions remain third-party content unless their author supplies localization.

`/mcp` shows connection state; `/mcp reconnect` reloads server configuration. The model's `mcp_status` reads local state, while `mcp_reconnect` performs bounded, permission-checked recovery for a specific server and known error. Reconnecting never replays a failed business operation. `diagnostic_get` exposes scoped evidence already granted to the calling runtime. See [Troubleshooting](../guides/troubleshooting.md) for the exact boundaries.

Implementation: [base tools](../../../src/virgo/tools), [official plugins](../../../src/virgo/plugins), [extension API](../../../src/virgo/harness/extensions.py).

## Removed product features

Schedule, Workflow, Team, persistent Tasks, general Memory and Goal no longer register tools or commands. Their files and historical tool results are preserved. Use [Migration](../guides/migrating-to-core-plugins.md) to inspect the old data and choose project scripts, CI or external systems where needed.
