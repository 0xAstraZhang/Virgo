# 005 — Runtime layering, extensibility boundaries, and durable state

[Decisions](README.md) · [Documentation](../README.md) · [简体中文](../../zh-CN/decisions/005-runtime-layering-and-durable-state.md)

**Status:** Accepted

## Context

As Virgo grew to support multiple LLM providers, subagents, background jobs, MCP servers, context compaction, and a full-screen TUI, tight coupling between UI state, model orchestration, execution backends, and optional features made cold-start performance, crash recovery, and testing harder to sustain.

## Decision

- **Strict lower-triangular layers and block-diagonal peers**: `src/virgo/` maintains an acyclic package and module dependency graph across all imports (top-level, function-local, and `TYPE_CHECKING`) with unidirectional layering: Layer 0 leaves (`_gitignore`, `_win32`, `i18n`, `diagnostics`, `execution_policy`) → Layer 1 contracts/processes and core (`execution_types`, `process`, `ai`, `agent`) → Layer 2a mutually independent execution leaves (`file_access`, `output_store`, `sandbox`) → Layer 2b facade & tools (`execution`, `tools`) → Layer 3 `harness` (with zero-sibling-import `harness/contracts.py` host protocols and unidirectional `ShellSelection` ← `JobManager` wiring) → Layer 4 mutually independent `plugins`/`extensions` → Layer 5 `cli`. Sibling AI provider families, execution leaves, built-in tool families, and plugins/extensions stay mutually independent, and cross-boundary contracts (such as `ExtensionAPI` request preparer/fallback-matcher hooks, `declare_diagnostic_query_tool`, `ToolResult.error_kind`, explicit `UI` confirmation capability flags, and `AgentSession` presentation façades) are AST-checked in `tests/test_architecture_matrix.py`. See [Functional requirements & Axiomatic Design](../development/axiomatic-design.md) for the distinction between this static DSM and intentionally bounded runtime feedback loops.
- **Three-tier capability model**:
  - **Core tools** (`fs`, `shell`, `output`, `request_permissions`) are wired directly by `AgentSession`.
  - **Built-in plugins** (`virgo.plugins`: `diagnostic_get`, `skills`, `ask_user`, `subagent`, `background`, `web`) load transactionally via `setup(api)` and toggle via `plugins.<id>.enabled` without being overridden by filename or disabled by `--no-extensions`.
  - **Built-in extensions** (`virgo.extensions`: `todo`, `mcp`) load through the standard user/project extension pipeline (`built-in < user < trusted project`), disable via `extensions.disabled` or `--no-extensions`, and keep cold start fast (for example, `mcp` lazily imports the MCP SDK only when at least one server is configured).
- **Core-owned supervision**: `AgentSession` (composing `ToolRegistry` and `ModelRegistry`) and `JobManager` own session stores, lifecycle notifications, and process/job cleanup while `Runtime` executes conversations and plugins expose only user/model interfaces through `ExtensionAPI`.
- **Purpose-built, failure-isolated durable stores**:
  - **Sessions (`sessions/<workspace-slug>/*.jsonl`)**: Append-only JSONL trees with file locking, automatic truncation of an incomplete unterminated trailing line on writable resume, and fail-closed `SessionSaveError` on broken persistence without discarding in-memory history.
  - **Command & MCP outputs (`outputs/session-<id>/`)**: Broker-private `OutputStore` (`out_<12-hex>`, UTF-8-aligned `read_output` paging, 7-day TTL, immediate unlink for untruncated foreground commands) so large tool output survives session resume without bloating conversation transcripts.
  - **Sub-agent traces (`subagents/session-<id>/`)**: Per-session `SubagentStore` (`subagent-<n>.jsonl` backed by `SessionStore`) keeping sub-agent execution out of the main conversation transcript while enabling multi-turn sub-agent reuse within a session and on-demand trace inspection (including after session resume); cleaned up alongside parent session removal.
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
