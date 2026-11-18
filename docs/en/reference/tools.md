# Tools and plugins

[Documentation](../README.md) · [简体中文](../../zh-CN/reference/tools.md)

The model receives the tools enabled for its role, configuration and current capabilities. The exact list can change with extensions, MCP connections and web routing. Tool names, argument names and enum values remain stable across interface languages.

Core owns file and selected-shell tools. The required Sandbox extension supplies resource requests only when selected. `web`, `todo`, `ask_user`, `subagent`, `background` and `diagnostic_get` supply the optional tools below and are enabled by default; `plugins.<id>.enabled` overrides each one. The [MCP extension](../../../extensions/README.md) adds `mcp_status` and one tool per configured server. See [Configuration](configuration.md#plugins).

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

Sandbox registers `request_permissions(tool, arguments, permissions, reason)`. `permissions` accepts additional `read_paths`, `write_paths`, and `network_allowed` (a list such as `["egress"]`, or `true` for all three network directions). Supply the complete arguments of one managed file, process or network operation. Approval does not execute it. The identical subsequent call atomically consumes the grant, bound to source runtime/run, tool instance, arguments, cwd, environment, policy version and resource identities. It expires after five minutes or run end. Rejection, cancellation, failure and history replay cannot restore it. Whole agents, workflows, arbitrary MCP calls, broker protection removal and Host switching cannot be granted.

Refusals report requested/effective boundary, restricted resource, payload-start evidence, possible effects and cleanup. Entry into a Python tool function is distinct from execution of the user payload. Current unsupported native policies are rejected before payload release; see [Sandbox](../guides/sandbox.md).

## Checklists and questions

`todo_write` replaces the existing execution checklist with the complete supplied list. Statuses are `pending`, `in_progress` and `completed`; multiple items can be in progress. Invalid input leaves the old list intact. `/todos` displays it. An incomplete checklist does not itself force another model turn.

`ask_user` lets the main agent ask a question with up to three options, plus custom input, or free-form input alone. Answering a clarification does not confirm execution. Noninteractive, cancelled and timed-out questions report `answered=false` without inventing an answer. Unattended runs use `askUser.unattendedTimeout`; a user joining the conversation can cancel that timeout.

## Focused subagents

The `agent(description, prompt, background=false)` tool delegates a focused task with an independent model context and returns its final answer. Supply task context in the prompt. Children inherit the source execution context, including queued background tasks. The `read_only` parameter has been removed from this tool and the Runtime/ExtensionAPI factories. Subagents do not recursively spawn subagents and default to 50 turns. Earlier tool calls may have effects even if the final response fails. Cancellation and `budget.maxModelCalls` are shared with the parent. An exhausted turn limit, interrupted final response, or empty final answer is reported as **incomplete**, not success. On a child failure, the parent receives bounded recent tool-error reasons and explicitly linked diagnostics; a successful delegation still returns only the final answer. Calls with the removed `type` argument are rejected. See [migration](../guides/migrating-to-core-plugins.md#subagent-capability-migration).

## Background jobs

Background shell commands and subagents share the job manager. `/jobs` lists them; `job_output` reads output and `job_kill` cancels a job. Completion notifications stay bound to the originating conversation. They can steer a running agent or wake an idle one, without moving results into the conversation currently selected in the UI.

Background subagent completion notifications include the full final answer. Background command notifications include only the last 1,200 characters of captured output; use `job_output` to read more. When a background callable returns an error without structured diagnostics, the job record retains a bounded, redacted copy of its original failure text instead of a generic status alone.

Client-side `web_search` and `web_fetch` transport errors identify the exception class (such as `ConnectError` versus `ReadTimeout`), but deliberately omit raw exception messages that might contain private URLs or credentials.

## MCP extension

MCP is an extension, not a built-in plugin. Copy [`extensions/mcp.py`](../../../extensions/README.md) into a user or trusted project extensions directory, install the optional `mcp` SDK extra, then configure stdio servers in user or project `mcp.json` or the `mcpServers` setting. Server arguments and environment entries can use `${VARIABLE}` interpolation. A server's tools appear as `mcp__<server>__<tool>`. Disabled entries are not started. Server descriptions remain third-party content.

Without the extension, no MCP connection starts and no `mcp_status` tool is registered. With it, the model's `mcp_status` reads local connection state and tool counts without contacting servers.

MCP invocations check network policy. Sandbox refuses calls whose local resource scope cannot be enforced; approval cannot bypass that refusal. Server startup and autonomous activity run outside Virgo's task sandbox; an offline preset does not isolate that service.

`/mcp` shows connection state; `/mcp reconnect` reloads server configuration and keeps the previous connections and tools when a replacement fails. There is no model-facing reconnect tool. See [Troubleshooting](../guides/troubleshooting.md) for the runtime boundaries.

Implementation: [base tools](../../../src/virgo/tools), [official plugins](../../../src/virgo/plugins), [MCP extension](../../../extensions/mcp.py), [extension API](../../../src/virgo/harness/extensions.py).

## Removed product features

Schedule, Workflow, Team, persistent Tasks, general Memory and Goal no longer register tools or commands. Their files and historical tool results are preserved. Use [Migration](../guides/migrating-to-core-plugins.md) to inspect the old data and choose project scripts, CI or external systems where needed.
