# Tools and plugins

[Documentation](../README.md) · [简体中文](../../zh-CN/reference/tools.md)

The model receives the tools enabled for its role, configuration and current capabilities. The exact list can change with extensions, MCP connections and web routing. Tool names, argument names and enum values remain stable across interface languages.

Core owns file, selected-shell and permission-request tools. The optional Plan plugin owns the committed Git reader and ProposedPlan tools. `web`, `todo`, `ask_user`, `subagent`, `background`, `mcp` and `diagnostic_get` supply the optional tools below. Use the default `coding` preset, or select capabilities explicitly through `plugins.<id>.enabled`. `--profile minimal --no-extensions` exposes only Core tools, with full schemas independent of workflow instructions. See [Configuration](configuration.md#profiles-and-plugins).

## Files, commands and web

| Tool | Use |
| --- | --- |
| `read` | Read text or supported images; use `offset` and `limit` for larger files |
| `write` | Create or replace a file |
| `edit` | Replace an exact text match in a file |
| `glob` | Find paths by pattern |
| `grep` | Search file content |
| `git_read` | Read local committed log, patches or differences between two commits without repository helpers |
| `bash` / `powershell` | Execute through the selected command shell |
| `web_search` | Search through an exposed client backend |
| `web_fetch` | Read a webpage through an exposed client backend |

Reads and tool output have budgets; long results include a continuation or saved output path. File writes to the same file are serialized. Native web providers may expose server-side tools instead of the client names above. See [shells](../guides/shells.md) and [web capabilities](../guides/web.md).

## Permission requests

`request_permissions(tool, arguments, permissions, reason)` requests a single-operation exception.
The three optional flags are `read_only`, `workspace_only`, `network_allowed`. Approval does not
execute or replay the operation; the next identical call consumes the grant in its originating run.
Supported targets are managed file, Shell, Web and MCP operations. Child tasks and workflows
keep their originating permissions and can request exact operations inside their own run.
Explicit deny rules and explicit read-only child ceilings remain binding. Mode is unchanged.
Structured refusals expose the failed dimension, target, payload-start and side-effect facts in
model-visible content and the TUI. Backend-unavailable errors are distinct from permission denial.

## Checklists and questions

`propose_plan(markdown)` publishes or replaces a complete Markdown ProposedPlan. It belongs to the optional Plan plugin and is available to the main Runtime regardless of active workflow; it saves versioned conversation metadata and does not write a project file or authorize execution. Revisions invalidate an older execution selection. `/plan` displays the proposal and `/plan execute <plan-id>:<revision>` is the user's explicit execution action. See [Planning a change](../guides/planning.md) for editing, restoration and permission boundaries.

`todo_write` replaces the existing execution checklist with the complete supplied list. Statuses are `pending`, `in_progress` and `completed`; multiple items can be in progress. Invalid input leaves the old list intact. `/todos` displays it. An incomplete checklist does not itself force another model turn. The tool remains available in Plan: ProposedPlan describes the solution, while Todo tracks execution progress in Default. Confirming a plan does not create Todo items automatically or introduce a second task store.

`ask_user` lets the main agent ask a question with up to three options, plus custom input, or free-form input alone. Plan instructions require repository investigation first and reserve this tool for unresolved decisions that materially change the plan. Answering a clarification does not confirm execution. Noninteractive, cancelled and timed-out questions report `answered=false` without inventing an answer. Unattended runs use `askUser.unattendedTimeout`; a user joining the conversation can cancel that timeout.

## Focused subagents

The `agent(description, prompt, read_only=false, background=false)` tool delegates a focused task with an independent context and returns its final answer; include relevant context in the task prompt. `read_only=true` keeps the normal tool schemas and adds an enforced host-file write ceiling, including for Shell. By default, the child inherits its parent's captured permissions without an additional ceiling. Subagents do not recursively spawn subagents and default to 50 turns. Earlier tool calls may have effects even if the final response fails. Cancellation and `budget.maxModelCalls` are shared with the parent. An exhausted turn limit, interrupted final response, or empty final answer is reported as **incomplete**, not success. On a child failure, the parent receives bounded recent tool-error reasons and explicitly linked diagnostics; a successful delegation still returns only the final answer. Calls with the removed `type` argument are rejected. See [migration](../guides/migrating-to-core-plugins.md#subagent-capability-migration).

## Background jobs

Background shell commands and subagents share the job manager. `/jobs` lists them; `job_output` reads output and `job_kill` cancels a job. Completion notifications stay bound to the originating conversation. They can steer a running agent or wake an idle one, without moving results into the conversation currently selected in the UI.

Background subagent completion notifications include the full final answer. Background command notifications include only the last 1,200 characters of captured output; use `job_output` to read more. When a background callable returns an error without structured diagnostics, the job record retains a bounded, redacted copy of its original failure text instead of a generic status alone.

Client-side `web_search` and `web_fetch` transport errors identify the exception class (such as `ConnectError` versus `ReadTimeout`), but deliberately omit raw exception messages that might contain private URLs or credentials.

## MCP and diagnostics

Without configured servers, the default preset starts no MCP connection and registers no MCP management tools. An explicitly enabled MCP plugin can expose management even before servers are added.

Configure stdio MCP servers in user or project `mcp.json`, or the `mcpServers` setting. Server arguments and environment entries can use `${VARIABLE}` interpolation. A server's tools appear as `mcp__<server>__<tool>`. Disabled entries are not started. Server and extension descriptions remain third-party content unless their author supplies localization.

Managed MCP invocations respect the network flag and require an explicit operation exception
when a server cannot enforce local file scope. Trusted MCP service startup and autonomous
activity run outside Virgo's task sandbox; an offline preset does not isolate that service.

`/mcp` shows connection state; `/mcp reconnect` reloads server configuration. The model's `mcp_status` reads local state, while `mcp_reconnect` performs bounded, permission-checked recovery for a specific server and known error. Reconnecting never replays a failed business operation. `diagnostic_get` exposes scoped evidence already granted to the calling runtime. See [Troubleshooting](../guides/troubleshooting.md) for the exact boundaries.

Implementation: [base tools](../../../src/virgo/tools), [official plugins](../../../src/virgo/plugins), [extension API](../../../src/virgo/harness/extensions.py).

## Removed product features

Schedule, Workflow, Team, persistent Tasks, general Memory and Goal no longer register tools or commands. Their files and historical tool results are preserved. Use [Migration](../guides/migrating-to-core-plugins.md) to inspect the old data and choose project scripts, CI or external systems where needed.
