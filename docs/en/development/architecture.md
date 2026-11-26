# Architecture overview

[Documentation](../README.md) · [Project home](../../../README.md) · [简体中文](../../zh-CN/development/architecture.md)

Virgo is organized into unidirectional package layers under `src/virgo/`. Lower layers stay independent of session state and terminal UIs so model adapters, the agent loop, execution guardrails, and the session harness can evolve and be tested independently.

## Package layers

| Layer | Packages / modules | Primary responsibility |
| --- | --- | --- |
| **0. Leaf foundations** | `_gitignore`, `_win32`, `i18n`, `diagnostics`, `execution_policy` | Pure `.gitignore` matching, shared Win32 bindings, session-scoped localization (`en`, `zh-CN`), no-I/O error capture and redaction, and deny-first `PermissionPolicy` / built-in execution modes with zero sandbox or harness imports |
| **1. Contracts, processes & core** | `execution_types`, `process`, `ai`, `agent` | Execution data/error contracts (`ExecutionContext`, `ProcessRequest`, `ProcessHandle`), POSIX `HostProcess` and Windows `WindowsProcess` primitives, unified streaming LLM adapters (`anthropic`, `openai`, `openai-responses`, `openai-codex`, `faux`), OAuth/API auth, model catalog, and stateless `run_loop` / `Agent` / `AgentTool` orchestration (including bounded parallel execution of non-sequential tool batches) |
| **2a. Execution leaves** | `file_access`, `output_store`, `sandbox` | Mutually independent Broker file I/O (`O_NOFOLLOW` + atomic `os.replace`), session-persisted Broker output storage (`OutputStore`), and native OS sandbox adapters (`bubblewrap`, macOS Seatbelt, Windows Standard AppContainer) |
| **2b. Execution facade & tools** | `execution`, `tools` | Composition facade (`HostContext`, `SandboxContext`, `SandboxExecutor`, `is_windows_sandbox_supported`, `default_execution_mode`) and built-in tool implementations (`fs`, `shell_env`/`shell`/`bash`, `output`, `web`, `web_fetch`) |
| **3. Session harness** | `harness` | `AgentSession` (composing `ToolRegistry` and `ModelRegistry`), `Runtime`, `EventBus`/`ExtensionAPI` (including generic request preparers/fallbacks), JSONL `SessionStore`, `ModeExecutionProvider`, `ResourceApprovals`/`ResourceRules`, `high_risk` checks, Core-owned `JobManager`, SQLite `DiagnosticLog`, settings/trust loading, unified three-layer context compaction (`CompactionController` + `compaction_records`), prompt assembly, explicit-capability `UI` interface, and slash commands |
| **4. Capabilities** | `plugins`, `extensions` | Six built-in plugins (`diagnostic_get`, `skills`, `ask_user`, `subagent`, `background`, `web`) and two built-in extensions (`mcp`, `todo`) loaded via `setup(api)` |
| **5. Presentation** | `cli`, `__main__` | Entry point (`virgo.cli.main:main`), plain REPL, Textual full-screen TUI (`cli/tui/`), and print/JSON output UI |

### Dependency invariants

Enforced via AST checks in `tests/test_architecture_matrix.py` (covering both top-level and function-local imports) and `tests/harness/test_permission_modes_e2e.py`:

- **Strict lower-triangular DAG**: Zero package-level and zero module-level dependency cycles across `src/virgo/` (covering top-level, function-local, and `TYPE_CHECKING` imports). No lower layer imports a higher layer, Layer 0 leaf packages carry zero `TYPE_CHECKING` upward imports, and `virgo.harness.contracts` holds zero-sibling-import Layer-3 host protocols so harness modules never back-import `session` or `runtime`.
- **Pure policy and core leaves**: `virgo.execution_policy` never imports `virgo.sandbox`, `virgo.execution`, or `virgo.harness` (Windows sandbox capability probing lives in `virgo.execution`). `virgo.ai` and `virgo.agent` do not import `execution*`, `process`, `file_access`, `output_store`, `sandbox`, `tools`, `harness`, `plugins`, `extensions`, or `cli`.
- **Block-diagonal peer independence**: Peer AI provider families (`anthropic` vs OpenAI family vs `faux`), execution leaf components (`file_access`, `output_store`, `sandbox`), built-in tool families (`fs`, `shell`/`bash`, `web`, `web_fetch`), and peer plugins/extensions never import each other.
- **Explicit cross-boundary contracts**: Plugins and extensions act through `ExtensionAPI` (without reaching into `api.session` or private `runtime._*`/`session._*` state). `ShellSelection` receives an optional `BackgroundLauncher` pushed from `JobManager` while `JobManager` resolves default shells only via injected callback (zero cross-service lookup). `CompactionController` and `Runtime` delegate request-extension fallback detection via `ExtensionAPI.add_request_fallback(..., matches=...)` and diagnostic-query capability via `ExtensionAPI.declare_diagnostic_query_tool(...)` instead of hardcoding plugin error kinds or tool names. `cli` interacts with `AgentSession` and `Runtime` through public façade methods (`observe_events`, `attach_ui`/`detach_ui`, `cancel_signal`, `has_runnable_wakeups`, `run_concurrent_command`) instead of touching `session.bus`, `session.wakeups`, `agent.signal`, or `session.services["execution"]`. See [Functional requirements & Axiomatic Design](axiomatic-design.md) for the full FR–DP matrix and bounded runtime feedback loops.

## Core tools, built-in plugins, and built-in extensions

Virgo separates runtime capabilities into three tiers with distinct override and failure boundaries:

| Tier | Wiring & inventory | Disabling & overriding | Failure isolation |
| --- | --- | --- | --- |
| **Core tools** | Registered directly in `AgentSession.__init__`: `read`, `write`, `edit`, `glob`, `grep`, `present_file` (displays the verified absolute path of a readable local file), `read_output`, `bash`/`powershell`, and `request_permissions` (hidden automatically when interactive escalation is unavailable) | Cannot be overridden by filename | Core initialization errors fail startup |
| **Built-in plugins** (`virgo.plugins`) | Loaded by `load_builtin_features()` via `setup(api)`: `diagnostic_get`, `skills`, `ask_user`, `subagent`, `background`, `web` | Controlled per ID via `plugins.<id>.enabled` (`false` skips import); unaffected by `--no-extensions`; cannot be overridden by filename | Loaded in a rollback transaction; failure degrades only that plugin (if `background` degrades, `shell` and `subagent` drop their `background` argument) |
| **Built-in & custom extensions** (`virgo.extensions` + user/project files) | Loaded by `load_extensions()` via `setup(api)`: built-in `mcp` and `todo`, plus `~/.virgo/extensions/*.py` and trusted `.virgo/extensions/*.py` | Resolved by filename stem (`built-in < user < trusted project`); disable individually via `extensions.disabled` or skip all extensions via `--no-extensions` | Same transactional rollback via `setup(api)` |

## Runtime supervision and task isolation

- **`AgentSession` and `Runtime`**: `AgentSession` owns user settings, mode selection, extensions, durable stores, and the Core `JobManager`, while `Runtime` runs individual model conversations (main session and delegated subagents).
- **Immutable execution context**: Every task captures its originating `HostContext` or `SandboxContext` along with its `ExecutionOrigin` (anchored at the active branch leaf). Child subagents and background jobs inherit that captured context and branch accounting even if the user switches mode, branch, or active UI session later.
- **Core-owned job supervision**: `JobManager` owns background job lifecycle, completion notifications, and cleanup; the `background` and `subagent` plugins only expose model tools (`job_output`, `job_kill`, `agent`) and `/jobs`.

Each subagent can use up to `subagents.maxTurns` assistant turns per run (default 20) and `subagents.maxTotalTurns` across all runs of the same `agent_id` in a session (default 40). Compaction and session resume preserve the count. Set `subagents.effort` to a supported reasoning level to override the parent's effort for child requests. The default is `null`, which inherits the parent's setting.

The Codex account adapter shares a session HTTP connection pool across requests. It replays only the `__oailb` and `__cflb` routing cookies for the same account, origin and exact endpoint path. Turn state is further scoped to the runtime. Each I/O task reads its own HTTPS event stream and closes the response. A terminal protocol event can arrive before HTTP EOF; on normal completion the adapter briefly drains the trailing chunk/EOF within a bounded cancel-aware window so the keep-alive connection can be reused, and waits for cleanup before returning the result or completing cancellation.

## Local state layout

By default, Virgo stores user state under `~/.virgo` (overrideable via `VIRGO_HOME`):

| Path | Purpose |
| --- | --- |
| `settings.json`, `auth.json`, `mcp.json` | User settings, OAuth/API credentials, and user MCP server definitions |
| `AGENTS.md`, `SYSTEM.md`, `APPEND_SYSTEM.md` | User-level prompt context and system prompt overrides |
| `extensions/`, `skills/`, `prompts/` | User Python extensions, `SKILL.md` skills, and Markdown slash-command templates |
| `sessions/<workspace-slug>/` | Append-only JSONL session trees (`session`, `message`, `compaction`, `model_change`, `leaf`, `custom` records) with cross-platform file locking and incomplete-tail recovery on resume |
| `outputs/session-<id>/` | Broker-private `OutputStore` spools (`out_<12-hex>`, searchable / tail / line- or UTF-8-aligned byte-paged inspection via `read_output`, 7-day TTL, cleaned on session trash) |
| `logs/diagnostics.sqlite3` | WAL-mode durable diagnostic error store (isolated from session saves; runtime-scoped evidence exposed to the model via `diagnostic_get`) |
| `permission-grants.sqlite3` | Saved **Always allow** resource grants (`resource_grants` table keyed by `(project, mode, resource, target)`) |

The main agent can use `session_info` to obtain the same session ID and transcript path as `/session`. A memory-only conversation returns a null path.

Main model requests append a `custom/request_header` before adapter dispatch on first use (including resume) and whenever the resolved configuration changes. It records provider, model, API, requested/wire/effective reasoning effort and output limits. Usage remains on the assistant message.

On native Windows, AppContainer session lease journals live separately under `%LOCALAPPDATA%\Virgo\sandbox-sessions\` (overrideable via `VIRGO_WINDOWS_SANDBOX_STATE_DIR`).

See also: [Functional requirements & Axiomatic Design](axiomatic-design.md) · [Execution interface](execution-interface.md) · [Native execution & performance acceptance](execution-acceptance.md) · [Architectural decisions](../decisions/README.md)
