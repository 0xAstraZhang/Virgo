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

Use `/diagnostics` to find the log path and the latest ten errors. `/diagnostics <error-id>` shows full JSON for a unique ID or prefix among the current instance's last 100 records. After restarting, inspect the JSONL log directly; the command does not scan all historical logs.

Logs are written on the first error under `~/.virgo/logs/`, or `$VIRGO_HOME/logs/`. Each instance has its own `harness-<UTC-time>-<pid>-<id>.jsonl`. `/new` keeps that instance's log and records the original session ID on each event. `--no-session` still permits diagnostic logs.

## Read a diagnostic

| Field | Meaning |
| --- | --- |
| `error_id` | Stable identity reused when the error crosses boundaries |
| `phase`, `code`, `kind` | Where it happened, machine-readable classification and retry/overflow/fatal/blocked category |
| `exception_type`, `cause_chain`, `traceback` | Observed exception and captured causes/stack locations |
| `context` | Allowlisted correlation metadata: session, runtime, agent, tool call, model call, attempt, timing and HTTP information |
| `log_path`, `logging_error` | Where the record was saved, or why logging failed |

For model failures, inspect the actual API, HTTP status, request ID and retry information when present. For local failures, inspect phase, exception and cause chain. A successful later retry does not delete earlier failure records. An exception message is evidence, not proof of the unique root cause.

Logs default to 5 MiB per file with two rotated files. Read the `.1` and `.2` siblings when needed. This is a per-instance limit; logs from older instances are not globally purged. New files/directories use restricted permissions where the platform supports them. If logging itself fails, Virgo attempts a sanitized stderr record without replacing the original error.

Diagnostics do not intentionally collect frame locals, source-code lines, complete request bodies, headers, system prompts or conversation messages. Credentials and sensitive metadata are redacted. Application error messages and server stderr can still contain application text, so review a log before sharing it. This does not change the storage contract of conversation history or full tool-output files.

## What the model can inspect

A failed tool result can include an added diagnostic summary of at most 2 KiB. It preserves the original result content and IDs. The model can request further evidence with `diagnostic_get(error_id, section, cursor)` when the tool is exposed to that runtime.

| Section | Evidence |
| --- | --- |
| `summary` | Allowlisted fields and correlation context |
| `causes` | Captured exception chain and bounded locations |
| `trace` | Captured stack text |
| `stderr` | Collected, sanitized MCP stderr |
| `attempts` | Retained, authorized failures for the same logical call and purpose |
| `related` | Explicitly propagated related diagnostics |

Queries use exact IDs, never arbitrary paths or ID prefixes. Access belongs to the calling runtime; sharing a session accounting store or selecting another UI conversation does not grant access. Defaults retain 100 evidence records and 1000 authorized IDs. Responses distinguish `not_found`, `expired` and `unavailable`. Each complete JSON page is bounded to 8 KiB; a cursor is bound to its error, section and evidence snapshot.

`plugins.diagnostic_get.enabled=false` disables the model lookup tool. Core logs, error IDs, bounded result summaries and recovery evidence remain available; `/diagnostics` also remains available. Diagnostics append at normal model-request boundaries; gathering evidence does not itself start a model turn or rewrite old messages.

## Plugin configuration and upgrades

An unknown plugin ID or malformed configuration fails startup with a configuration error. Optional setup failures identify the plugin and report degradation; partial registrations are rolled back. Disabled plugins do not start their services. Project Python extensions need external trust; use `--trust-project-extensions` only after reviewing them.

Old schedules are intentionally inactive. Schedule, Workflow, Team, Tasks, Memory and Goal were removed; their files remain for manual inspection. See [Migration](migrating-to-core-plugins.md), rather than recreating an old command by changing feature flags.

## Execution failure does not imply no side effects

`details.execution` records whether a tool started and whether it returned success, error or an unknown outcome. `details.transport` records MCP dispatch and response state. A result-hook failure after successful execution does not undo the operation. A timed-out shell, failed child agent or missing remote response may already have changed files or external state. Verify the outcome before repeating the operation.

Provider retries remain controlled by the runtime's existing retry policy. Valid `Retry-After` is a minimum delay, and `retry.maxWaitSeconds` bounds cumulative waiting. A request that has already begun a server-side native web operation is not automatically replayed as an unsupported-capability fallback.

## MCP recovery

`mcp_status` reads local state without contacting the server. The model's `mcp_reconnect(server, error_id)` requires an authorized diagnostic for that server's active connection generation and passes normal permission checks. It cannot interrupt an in-flight call or compete with another pending recovery. Plan mode does not permit reconnection.

A replacement connection must initialize, list tools and pass schema/name validation before publication. Same-schema recovery replaces the connection; schema changes publish at a safe model-request boundary. Explicitly restricted child tool lists do not gain new capabilities. Invalid candidates leave the old connection and tools in place.

There are at most two recovery attempts without a successful remote tool call; a successful handshake alone does not reset that budget. Reconnection never replays a failed call. If dispatch reached `sending` or `sent` and the response was lost, the business outcome remains unknown. `/mcp reconnect` is the user's broader configuration-reload path and also preserves active-call boundaries.

## Session-save failure

The session store keeps logical in-memory entries after a write failure and reports the original cause through the independent log. It pauses scheduling and signals the owning runtime so further work does not race ahead of unsaved history. Switching views does not repair the failed store, and a diagnostic notification cannot clear cancellation or a save failure. Avoid closing the process until you have recovered needed unsaved information.

Implementation: [diagnostics](../../../src/virgo/diagnostics.py), [log storage](../../../src/virgo/harness/diagnostics.py), [runtime evidence](../../../src/virgo/harness/diagnostic_access.py), [recovery](../../../src/virgo/harness/recovery.py), [MCP](../../../src/virgo/plugins/mcp.py).
