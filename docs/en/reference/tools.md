# Tools and plugins

[Documentation](../README.md) · [简体中文](../../zh-CN/reference/tools.md)

The model receives the tools enabled for its role, configuration and current capabilities. The exact list can change with extensions, MCP connections and web routing. Tool names, argument names and enum values remain stable across interface languages.

Core owns file, selected-shell and permission-request tools. `web`, `todo`, `ask_user`, `subagent`, `background` and `diagnostic_get` supply the optional tools below and are enabled by default; `plugins.<id>.enabled` overrides each one. The [MCP extension](../../../extensions/README.md) adds `mcp_status` and one tool per configured server. See [Configuration](configuration.md#plugins).

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

## Permission requests

`request_permissions(tool, arguments, permissions, reason)` requests a single-operation exception.
The three optional flags are `read_only`, `workspace_only`, `network_allowed`. Approval does not
execute or replay the operation; the next identical call consumes the grant in its originating run.
Supported targets are managed file, Shell, Web and MCP operations. Child tasks and workflows
keep their originating permissions and can request exact operations inside their own run.
Explicit deny rules and explicit read-only child ceilings remain binding. The permission preset is unchanged.
Structured refusals expose the failed dimension, target, payload-start and side-effect facts in
model-visible content and the TUI. Backend-unavailable errors are distinct from permission denial.

## Checklists and questions

`todo_write` replaces the existing execution checklist with the complete supplied list. Statuses are `pending`, `in_progress` and `completed`; multiple items can be in progress. Invalid input leaves the old list intact. `/todos` displays it. An incomplete checklist does not itself force another model turn.

`ask_user` lets the main agent ask a question with up to three options, plus custom input, or free-form input alone. Answering a clarification does not confirm execution. Noninteractive, cancelled and timed-out questions report `answered=false` without inventing an answer. Unattended runs use `askUser.unattendedTimeout`; a user joining the conversation can cancel that timeout.

## Focused subagents

The `agent(description, prompt, read_only=false, background=false)` tool delegates a focused task with an independent context and returns its final answer; include relevant context in the task prompt. `read_only=true` keeps the normal tool schemas and adds an enforced host-file write ceiling, including for Shell. By default, the child inherits its parent's captured permissions without an additional ceiling. Subagents do not recursively spawn subagents and default to 50 turns. Earlier tool calls may have effects even if the final response fails. Cancellation and `budget.maxModelCalls` are shared with the parent. An exhausted turn limit, interrupted final response, or empty final answer is reported as **incomplete**, not success. On a child failure, the parent receives bounded recent tool-error reasons and explicitly linked diagnostics; a successful delegation still returns only the final answer. Calls with the removed `type` argument are rejected. See [migration](../guides/migrating-to-core-plugins.md#subagent-capability-migration).

## Background jobs

Background shell commands and subagents share the job manager. `/jobs` lists them; `job_output` reads output and `job_kill` cancels a job. Completion notifications stay bound to the originating conversation. They can steer a running agent or wake an idle one, without moving results into the conversation currently selected in the UI.

Background subagent completion notifications include the full final answer. Background command notifications include only the last 1,200 characters of captured output; use `job_output` to read more. When a background callable returns an error without structured diagnostics, the job record retains a bounded, redacted copy of its original failure text instead of a generic status alone.

Client-side `web_search` and `web_fetch` transport errors identify the exception class (such as `ConnectError` versus `ReadTimeout`), but deliberately omit raw exception messages that might contain private URLs or credentials.

## MCP extension

MCP is an extension, not a built-in plugin. Copy [`extensions/mcp.py`](../../../extensions/README.md) into a user or trusted project extensions directory, install the optional `mcp` SDK extra, then configure stdio servers in user or project `mcp.json` or the `mcpServers` setting. Server arguments and environment entries can use `${VARIABLE}` interpolation. A server's tools appear as `mcp__<server>__<tool>`. Disabled entries are not started. Server descriptions remain third-party content.

Without the extension, no MCP connection starts and no `mcp_status` tool is registered. With it, the model's `mcp_status` reads local connection state and tool counts without contacting servers.

MCP invocations respect the network flag and require an explicit operation exception when a server cannot enforce local file scope. Server startup and autonomous activity run outside Virgo's task sandbox; an offline preset does not isolate that service.

`/mcp` shows connection state; `/mcp reconnect` reloads server configuration and keeps the previous connections and tools when a replacement fails. There is no model-facing reconnect tool. See [Troubleshooting](../guides/troubleshooting.md) for the runtime boundaries.

Implementation: [base tools](../../../src/virgo/tools), [official plugins](../../../src/virgo/plugins), [MCP extension](../../../extensions/mcp.py), [extension API](../../../src/virgo/harness/extensions.py).

## Removed product features

Schedule, Workflow, Team, persistent Tasks, general Memory and Goal no longer register tools or commands. Their files and historical tool results are preserved. Use [Migration](../guides/migrating-to-core-plugins.md) to inspect the old data and choose project scripts, CI or external systems where needed.
