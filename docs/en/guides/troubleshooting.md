# Troubleshooting and recovery

[Documentation](../README.md) · [简体中文](../../zh-CN/guides/troubleshooting.md)

## Start with the visible failure

| Symptom | Check |
| --- | --- |
| Full-screen startup fails | Use an interactive terminal with a supported `TERM`, or select `--ui plain` |
| No models in the picker | Let the startup refresh finish, reopen `/model`, check `/login`, or specify `/model provider/id` |
| Authentication fails after changing an endpoint | Log in again for the new protocol/endpoint; saved credentials are connection-bound |
| A configured shell is unavailable | Use `/shell` or specify an adapted executable path; Virgo does not silently substitute a different language |
| Work pauses after saving history fails | Keep the current process open, resolve the filesystem problem, and inspect the diagnostic before switching or exiting |

Use `/diagnostics` to find the database path and the latest ten errors across instances, including after a restart. Follow `/diagnostics --before <cursor>` to read earlier pages. `/diagnostics <error-id>` shows full JSON for an exact ID or unique prefix; an exact match takes precedence over prefix matches. These commands do not change model history or grant model access.

Errors are written on first use to `~/.virgo/logs/diagnostics.sqlite3`, or `$VIRGO_HOME/logs/diagnostics.sqlite3`. SQLite transactions and a unique error ID retain one record even when multiple processes report the same error. `/new` keeps the database; each record retains its original session context. `--no-session` still permits diagnostic logs.

When the selected permission preset blocks a shell command, the tool result identifies the first unapproved command, option, or shell construct. This is a permission denial, not a transient shell failure: do not retry the same effect with different syntax. Use the file-inspection tools or recognized read-only commands; request a writable run when the task needs changes. The classifier is conservative and is not an OS sandbox.

## Read a diagnostic

| Field | Meaning |
| --- | --- |
| `error_id` | Stable identity reused when the error crosses boundaries |
| `phase`, `code`, `kind` | Where it happened, machine-readable classification and retry/overflow/fatal/blocked category |
| `exception_type`, `cause_chain`, `traceback` | Observed exception and captured causes/stack locations |
| `context` | Allowlisted correlation metadata: session, runtime, agent, tool call, model call, attempt, timing and HTTP information |
| `log_path`, `logging_error` | Where the record was saved, or why logging failed |

For model failures, inspect the actual API, HTTP status, request ID and retry information when present. For local failures, inspect phase, exception and cause chain. A successful later retry does not delete earlier failure records. An exception message is evidence, not proof of the unique root cause.

Errors have no count limit and are not deleted by rotation. SQLite uses a bounded page cache and WAL journaling; the database and its WAL sidecars belong together while Virgo is running. New files/directories use restricted permissions where the platform supports them. If logging fails or a write lock cannot be obtained within one second, Virgo preserves the original error, marks the save failure and attempts sanitized stderr output. A bounded emergency cache keeps up to 100 unsaved errors in that process; this fallback cannot promise durable retention when storage is unavailable. `/diagnostics` includes those unsaved records in its paginated listing, even when the database cannot be read. An emergency-record cursor is local to this process; restart with `/diagnostics` if its record is saved or leaves the cache.

History commands incrementally import existing `harness-*.jsonl` files and their numbered rotation siblings. Source files remain unchanged. Complete valid lines are deduplicated by error ID; malformed or oversized lines produce import warnings, and an unfinished final line is retried on a later query. Import progress is committed in batches, checking the open file's current size so concurrent appends do not reset completed progress. Logging a new error and model queries do not trigger legacy import.

Diagnostics do not intentionally collect frame locals, source-code lines, complete request bodies, headers, system prompts or conversation messages. Credentials and sensitive metadata are redacted. Application error messages and server stderr can still contain application text, so review a log before sharing it. This does not change the storage contract of conversation history or full tool-output files.

## What the model can inspect

A failed tool result can include an added diagnostic summary of at most 2 KiB. It preserves the original result content and IDs. The model can request further evidence with `diagnostic_get(error_id, section, cursor)` when the tool is exposed to that runtime.

| Section | Evidence |
| --- | --- |
| `summary` | Allowlisted fields and correlation context |
| `causes` | Captured exception chain and bounded locations |
| `trace` | Captured stack text |
| `stderr` | Bounded, sanitized MCP server stderr when a connection fails |
| `attempts` | Retained, authorized failures for the same logical call and purpose |
| `related` | Explicitly propagated related diagnostics |

Queries use exact IDs, never arbitrary paths or ID prefixes. Access belongs to the calling runtime; sharing a session accounting store or selecting another UI conversation does not grant access. Evidence and authorized IDs have no count limit. Filtered snapshots and delivery bookkeeping use a separate private temporary SQLite database for each runtime, under the protected logs directory. Runtime cleanup removes that temporary store; reopening a saved session restores only grants from its own canonical history. Global history never supplies missing model evidence. Individual filtered records remain bounded to 64 KiB. Unknown or unauthorized IDs return `not_found`; uncaptured sections or lookup failures return `unavailable`. Each complete JSON page is bounded to 8 KiB. Cursors are bound to the runtime, error, section and collection revision; restart pagination if evidence changes. Attempt pages seek through an indexed call group instead of materializing all failures.

`plugins.diagnostic_get.enabled=false` disables the model lookup tool. Core logs, error IDs, bounded result summaries and recovery evidence remain available; `/diagnostics` also remains available. Diagnostics append at normal model-request boundaries; gathering evidence does not itself start a model turn or rewrite old messages.

## Plugin configuration and upgrades

An unknown plugin ID or malformed configuration fails startup with a configuration error. Optional setup failures identify the plugin and report degradation; partial registrations are rolled back. Disabled plugins do not start their services. Project Python extensions need external trust; use `--trust-project-extensions` only after reviewing them.

Old schedules are intentionally inactive. Schedule, Workflow, Team, Tasks, Memory and Goal were removed; their files remain for manual inspection. See [Migration](migrating-to-core-plugins.md), rather than recreating an old command by changing feature flags.

## Execution failure does not imply no side effects

`details.execution` records whether a tool started and whether it returned success, error or an unknown outcome. A result-hook failure after successful execution does not undo the operation. A timed-out shell, failed child agent or missing remote response may already have changed files or external state. Verify the outcome before repeating the operation.

Provider retries remain controlled by the runtime's existing retry policy. Valid `Retry-After` is a minimum delay, and `retry.maxWaitSeconds` bounds cumulative waiting. A request that has already begun a server-side native web operation is not automatically replayed as an unsupported-capability fallback.

## MCP extension

MCP is not built in; see the [MCP extension](../../../extensions/README.md) for installation and configuration. `mcp_status` reads the extension's local connection state without contacting servers. `/mcp` lists configured servers; `/mcp reconnect` reloads `mcp.json` and the `mcpServers` setting.

A reload connects every configured server before publishing anything. If any candidate fails, the previous connections and tools stay in place and the failure is reported. A successful reload publishes the new tool set at the next model-request boundary; an old tool handle fails conservatively after its connection is retired. Reconnection never replays a failed call. A cancelled call sends `notifications/cancelled` to the server, but a response that was already lost leaves the business outcome unknown.

Connection failures include a bounded, redacted stderr tail from the server and are recorded as diagnostics.

## Session-save failure

The session store keeps logical in-memory entries after a write failure and reports the original cause through the independent log. It pauses scheduling and signals the owning runtime so further work does not race ahead of unsaved history. Switching views does not repair the failed store, and a diagnostic notification cannot clear cancellation or a save failure. Avoid closing the process until you have recovered needed unsaved information.

Implementation: [diagnostics](../../../src/virgo/diagnostics.py), [log storage](../../../src/virgo/harness/diagnostics.py), [runtime evidence](../../../src/virgo/harness/diagnostic_access.py), [recovery](../../../src/virgo/harness/recovery.py), [MCP extension](../../../extensions/mcp.py).
