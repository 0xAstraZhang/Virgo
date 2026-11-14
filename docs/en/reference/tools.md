# Tools and automation

[Documentation](../README.md) · [简体中文](../../zh-CN/reference/tools.md)

The model receives the tools enabled for its role, configuration and current capabilities. The exact list can change with extensions, MCP connections and web routing. Tool names, argument names and enum values remain stable across interface languages.

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

## Checklists, questions and memory

`todo_write` replaces the session checklist with the complete supplied list. Statuses are `pending`, `in_progress` and `completed`; multiple items can be in progress. Invalid input leaves the old list intact. `/todos` displays it. An incomplete checklist does not itself force another model turn.

`ask_user` lets the main agent ask a question with up to three options, plus custom input, or free-form input alone. Noninteractive, cancelled and timed-out questions report `answered=false` without inventing an answer. Unattended runs use `askUser.unattendedTimeout`; a user joining the conversation can cancel that timeout.

The `memory` tool saves, updates or deletes Markdown memory documents. User and feedback memories live in the user directory; project and reference memories live in the project's `.virgo/memory/`. Only a nonempty index and reading entry point enter the initial system prompt. `/memory tidy` uses the small model to consolidate records and rolls back a failed replacement. Use project documentation for shared maintained knowledge; avoid storing secrets or transient task status as memory.

## Subagents and teams

The `agent` tool runs a focused task with an independent context and returns its final answer. Built-in types are `general` and read-only `explore`; custom types live in `agents/*.md`. Subagents do not recursively spawn subagents and default to 50 turns. Their earlier tool calls may have had effects even if the final response fails.

Teams keep long-lived agents with separate contexts and in-process mailboxes. A teammate waits after finishing its current assignment. `team_message` delegates work or wakes it; changing a task's owner alone does not wake it. Optional isolated Git worktrees live under `.virgo/worktrees/`. Team shutdown releases unfinished work claimed by the departing teammate. `/team` shows state.

The durable task graph is separate from the session checklist. Task tools create, list, inspect and update records under `.virgo/tasks/`. `task_update(status="in_progress")` explicitly claims work, checks unfinished dependencies and ownership, and rejects dependency cycles. Completing a task identifies newly unblocked work. On Unix, mutations use an interprocess file lock; on Windows the implementation uses the process-local thread lock.

## Background jobs, schedules and goals

Background shell commands and subagents share the job manager. `/jobs` lists them; `job_output` reads output and `job_kill` cancels a job. Completion notifications stay bound to the originating conversation. They can steer a running agent or wake an idle one, without moving results into the conversation currently selected in the UI.

Schedules deliver saved prompts while Virgo is running. Manual creation, persistence defaults, time zones and missed-run behavior are covered in [Scheduled tasks](../guides/schedules.md).

`/goal <condition>` explicitly enables a completion loop. A tool-free evaluator checks that condition against the available conversation evidence when the main agent tries to stop. It can confirm completion, report impossibility or request continued work. Evaluation waits for active background jobs. After `goal.maxContinues` consecutive misses, or evaluation failure, control returns to the user with the goal still set. `/goal clear` removes it.

## Workflows

Workflows are Python scripts that define a repeatable orchestration using `ctx.agent`, `ctx.map`, `ctx.parallel`, `ctx.phase` and `ctx.log`. The model launches the script rather than inventing its control flow. Successful agent calls are journaled under `.virgo/runs/`; `/workflow resume <run_id>` reruns the script and reuses matching completed agent calls.

Only journaled agent calls are reused. Arbitrary Python side effects in the script may run again during resume, so scripts must account for replay. Workflow names and descriptions are indexed when a workspace session is initialized; restart or initialize a new workspace session after adding or renaming one. The selected script is loaded again at execution. See [Extensions](../development/extensions.md#workflows).

## MCP and diagnostics

Configure stdio MCP servers in user or project `mcp.json`, or the `mcpServers` setting. Server arguments and environment entries can use `${VARIABLE}` interpolation. A server's tools appear as `mcp__<server>__<tool>`. Disabled entries are not started. Server and extension descriptions remain third-party content unless their author supplies localization.

`/mcp` shows connection state; `/mcp reconnect` reloads server configuration. The model's `mcp_status` reads local state, while `mcp_reconnect` performs bounded, permission-checked recovery for a specific server and known error. Reconnecting never replays a failed business operation. `diagnostic_get` exposes scoped evidence already granted to the calling runtime. See [Troubleshooting](../guides/troubleshooting.md) for the exact boundaries.

Implementation: [base tools](../../../src/virgo/tools), [built-in features](../../../src/virgo/features), [extension API](../../../src/virgo/harness/extensions.py).
