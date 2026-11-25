# Architecture overview

[Documentation](../README.md) · [Project home](../../../README.md) · [简体中文](../../zh-CN/development/architecture.md)

Virgo is organized into unidirectional package layers under `src/virgo/`. Lower layers stay independent of session state and terminal UIs so model adapters, the agent loop, execution guardrails, and the session harness can evolve and be tested independently.

## Package layers

| Layer | Packages / modules | Primary responsibility |
| --- | --- | --- |
| **0. Leaf foundations** | `i18n`, `diagnostics`, `execution_policy`, `output_store` | Session-scoped localization (`en`, `zh-CN`), no-I/O error capture and redaction, deny-first `PermissionPolicy` and built-in execution modes, and session-persisted Broker output storage |
| **1. Model & agent core** | `ai`, `agent` | Unified streaming LLM adapters (`anthropic`, `openai`, `openai-responses`, `openai-codex`, `faux`), OAuth/API auth, model catalog, and stateless `run_loop` / `Agent` / `AgentTool` orchestration (including bounded parallel execution of non-sequential tool batches) |
| **2. Execution & tools** | `execution`, `file_access`, `sandbox`, `tools` | Unified `SandboxExecutor` and immutable `HostContext`/`SandboxContext`, Broker file I/O (`O_NOFOLLOW` + atomic `os.replace`), native OS sandbox adapters (`bubblewrap`, macOS Seatbelt, Windows Standard AppContainer), and built-in tool implementations (`fs`, `shell`, `bash`, `output`, `web`, `web_fetch`) |
| **3. Session harness** | `harness` | `AgentSession`, `Runtime`, `EventBus`/`ExtensionAPI`, JSONL `SessionStore`, `ModeExecutionProvider`, `ResourceApprovals`/`ResourceRules`, `high_risk` checks, Core-owned `JobManager`, SQLite `DiagnosticLog`, settings/trust loading, unified three-layer context compaction (`CompactionController`), prompt assembly, and slash commands |
| **4. Capabilities** | `plugins`, `extensions` | Six built-in plugins (`diagnostic_get`, `skills`, `ask_user`, `subagent`, `background`, `web`) and two built-in extensions (`mcp`, `todo`) loaded via `setup(api)` |
| **5. Presentation** | `cli` | Entry point (`virgo.cli.main:main`), plain REPL, Textual full-screen TUI (`cli/tui/`), and print/JSON output UI |

### Dependency invariants

- `virgo.ai` and `virgo.agent` do not import `execution*`, `sandbox`, `tools`, `harness`, `plugins`, `extensions`, or `cli`.
- `virgo.execution_policy`, `virgo.execution`, `virgo.file_access`, `virgo.output_store`, `virgo.sandbox`, and `virgo.tools` do not import `harness` or `cli` (`virgo.sandbox` independence from `virgo.harness` is AST-verified in `tests/harness/test_permission_modes_e2e.py`).
- `virgo.execution_policy` remains free of harness dependencies; on native Windows only, `is_windows_sandbox_supported()` lazily probes `virgo.sandbox.windows_acl`.

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

The Codex account adapter shares a session HTTP connection pool across requests. It replays only the `__oailb` and `__cflb` routing cookies for the same account, origin and exact endpoint path. Turn state is further scoped to the runtime. Each I/O task reads its own HTTPS event stream and closes the response. A terminal protocol event can arrive before HTTP EOF. The adapter waits for that request to finish cleanup before returning the result or completing cancellation.

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

See also: [Execution interface](execution-interface.md) · [Native execution & performance acceptance](execution-acceptance.md) · [Architectural decisions](../decisions/README.md)
