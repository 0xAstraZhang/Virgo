# 005 — Runtime layering, extensibility boundaries, and durable state

[Decisions](README.md) · [Documentation](../README.md) · [简体中文](../../zh-CN/decisions/005-runtime-layering-and-durable-state.md)

**Status:** Accepted

## Context

As Virgo grew to support multiple LLM providers, subagents, background jobs, MCP servers, context compaction, and a full-screen TUI, tight coupling between UI state, model orchestration, execution backends, and optional features made cold-start performance, crash recovery, and testing harder to sustain.

## Decision

- **Unidirectional package layers**: `virgo.ai` and `virgo.agent` stay free of execution, harness, and UI imports; `virgo.execution*`, `virgo.file_access`, `virgo.output_store`, `virgo.sandbox`, and `virgo.tools` never import `virgo.harness` or `virgo.cli` (with `virgo.sandbox` independence AST-checked in CI).
- **Three-tier capability model**:
  - **Core tools** (`fs`, `shell`, `output`, `request_permissions`) are wired directly by `AgentSession`.
  - **Built-in plugins** (`virgo.plugins`: `diagnostic_get`, `skills`, `ask_user`, `subagent`, `background`, `web`) load transactionally via `setup(api)` and toggle via `plugins.<id>.enabled` without being overridden by filename or disabled by `--no-extensions`.
  - **Built-in extensions** (`virgo.extensions`: `todo`, `mcp`) load through the standard user/project extension pipeline (`built-in < user < trusted project`), disable via `extensions.disabled` or `--no-extensions`, and keep cold start fast (for example, `mcp` lazily imports the MCP SDK only when at least one server is configured).
- **Core-owned supervision**: `AgentSession` and `JobManager` own session stores, lifecycle notifications, and process/job cleanup while `Runtime` executes conversations and plugins expose only user/model interfaces.
- **Purpose-built, failure-isolated durable stores**:
  - **Sessions (`sessions/<workspace-slug>/*.jsonl`)**: Append-only JSONL trees with file locking, automatic truncation of an incomplete unterminated trailing line on writable resume, and fail-closed `SessionSaveError` on broken persistence without discarding in-memory history.
  - **Command & MCP outputs (`outputs/session-<id>/`)**: Broker-private `OutputStore` (`out_<12-hex>`, UTF-8-aligned `read_output` paging, 7-day TTL, immediate unlink for untruncated foreground commands) so large tool output survives session resume without bloating conversation transcripts.
  - **Diagnostics (`logs/diagnostics.sqlite3`)**: WAL-mode SQLite isolated from session saves, separating routine tool errors from runtime faults and scoping model-visible `diagnostic_get` access strictly to error IDs emitted in that runtime.
  - **Saved grants (`permission-grants.sqlite3`)**: Single `resource_grants` table keyed by `(project, mode, resource, target)`.

## Tradeoffs and constraints

- **Lazy imports for heavy optional SDKs**: Built-in extensions or optional features must not degrade cold-start budgets when unconfigured.
- **No reverse imports into `harness` or `cli`**: Core policy, execution, sandbox, and agent packages must remain testable without constructing a TUI or full `AgentSession`.
- **Isolated failure domains**: Diagnostic logging failures must never break session persistence, and single-plugin setup failures must roll back cleanly without crashing startup.

## Related

- [Architecture overview](../development/architecture.md)
- [Extensions (Todos & MCP)](../guides/extensions.md)
- [Execution interface](../development/execution-interface.md)
