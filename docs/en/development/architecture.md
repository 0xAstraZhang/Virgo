# Architecture

[Documentation](../README.md) · [简体中文](../../zh-CN/development/architecture.md)

Virgo separates model transport, the agent loop and application policy. Built-in features use the same extension API as project extensions. The model chooses how to approach a task; the harness supplies capabilities, execution boundaries, persistence and observations.

## Repository map

| Path | Responsibility |
| --- | --- |
| `src/virgo/ai/` | Provider-neutral messages, streaming, authentication, model metadata and provider adapters |
| `src/virgo/agent/` | Tool-call loop, events, cancellation, steering and follow-up queues |
| `src/virgo/harness/` | Session/runtime construction, settings, commands, extensions, context, permissions and persistence |
| `src/virgo/tools/` | File, shell and web execution primitives |
| `src/virgo/features/` | Built-in capabilities registered through `setup(api)` |
| `src/virgo/cli/` | Entry point, print mode, plain REPL and Textual full-screen application |
| `src/virgo/i18n/` | Language registry, translation API and built-in catalogs |
| `tests/` | Tests organized by source responsibility; shared support in `tests/support/` |
| `ci/`, `.github/workflows/` | Test execution, revision comparison and workflow orchestration |
| `examples/project/` | Files to adapt for a project's instructions and `.virgo/` configuration |
| `docs/en/`, `docs/zh-CN/` | Matching maintained documentation |

The installed entry point and `python -m virgo` both enter [`cli/main.py`](../../../src/virgo/cli/main.py). The small root `main.py` is a compatibility launcher. Keep runtime code inside the package so source and installed-wheel execution use the same implementation.

Shared rules live in the layer that owns their meaning. `ai/providers/reasoning.py` supplies OpenAI reasoning-effort mapping to request construction and diagnostic metadata. `harness/permissions.py` owns permission modes, localized labels and plan-mode tool filtering; the concrete approval policy stays in `features/permission.py`, which re-exports the public helpers for compatibility. Features resolve trusted callers through `AgentSession.runtime_for_context()` instead of duplicating identity checks or borrowing the currently selected runtime. See [Abstraction boundaries](abstraction-boundaries.md) for the retained layers and interfaces that need separate design.

## Sessions and runtimes

`AgentSession` owns workspace paths, effective settings, authentication, a session-scoped translator, the UI adapter, registered tools/commands, shared services and diagnostic logs. A `Runtime` owns one agent context: main, subagent or teammate. Its construction explicitly binds working directory, tools, model, conversation store, accounting store and review ancestry.

Candidate runtimes build their system context before activation. `RuntimeContext` lets runtime-aware prompt sections read the candidate's store and role without temporarily replacing `session.main` or `session.store`. A failed candidate leaves the active conversation intact.

The full-screen application keeps controllers for live conversations. Switching the selected conversation changes what the user sees; it does not reassign background work. Async callbacks bind their original store and review source when queued. A shared accounting store does not grant access to another runtime's diagnostic evidence.

## Request and tool flow

1. A command or prompt enters the session. Commands execute locally; prompt templates and explicit skills expand into model input.
2. The runtime prepares its current tool snapshot, pending schema changes, context compatibility, compaction and diagnostic notifications.
3. Provider-neutral messages and tool specs are adapted to the selected model protocol. The provider streams events into the agent loop.
4. Tool arguments are prepared and validated. The harness performs permission preflight, runs ordered hooks, and checks authorization again immediately before execution.
5. Tools run, normally in parallel unless they require sequential execution. Result hooks may transform the result; execution/transport facts survive that transformation.
6. Results and events enter the append-only conversation. Steering is applied between tool batches; follow-ups and enabled stop hooks decide whether another turn is needed.

The loop itself does not perform application I/O. Execution arrives through tool callables, and rendering arrives through event subscribers. The UI talks to the harness through `UI`; the full-screen bridge queues worker updates so threads do not touch Textual widgets directly.

## Contracts worth preserving

**Message ownership.** `ToolResult.content` is model-visible text/image content. `details` holds structured UI/state metadata and is not blindly serialized to a provider. Diagnostic projection is an explicit, bounded exception: a safe summary is added to new failed results, with more evidence available through an authorized tool.

**Stable request prefixes.** Between configuration/schema changes and compaction boundaries, system/tool prefixes remain stable and new messages append to the previous request. Do not rescan and rewrite earlier results on every model call. Model, permission, shell and language changes are explicit configuration boundaries. Native context is checked against the active model/system/tool binding.

**Permission checks.** The policy exposes `preflight(event)` and `before_execute(event)` rather than relying only on an event subscription. The harness preserves ordering relative to command and user hooks, while the execution-time check catches argument, workspace or authorization changes. A policy exception blocks execution.

**Persistence failure.** `SessionStore` retains logical entries independently from a successful disk append. A save error marks the store, signals its runtimes and pauses later scheduling. The diagnostic log is separate so reporting does not append recursively to a broken session. Do not treat a UI switch or an event completion as proof that persistence succeeded.

**Output and side effects.** Large tool output is bounded and written to files. Failure does not prove that execution never started. Keep tool execution state separate from result-hook success and transport completion; recovery cannot safely infer idempotency from `read_only`.

**Localization.** Translators belong to sessions and are passed to UI/tool contexts. Language catalogs contain display text, not protocol identifiers, executable commands, user text or remote payloads. See [Localization](localization.md).

## Context, diagnostics and recovery

The base system context combines identity/guidelines, extension sections, project instructions, optional custom system text, and the actual environment. `_base_context` adds the selected response-language preference to the request without rebuilding the base prompt on every turn. Skills and memory disclose indexes first; empty resources do not add a large prompt block. Configuration and capability facts come from their owning services.

Compaction preserves full history while replacing only the model's active context. Supported official providers use their native representation; other providers receive a continuation summary and recent messages. Context hooks should preserve append-only behavior unless they represent a documented boundary.

`diagnostic_view.py` projects bounded safe summaries. `diagnostic_access.py` owns per-runtime authorization and pagination. `recovery.py` persists granted IDs and pending/delivered evidence and appends notifications at normal request boundaries. Provider retry remains in `Runtime._attempts`; evidence collection does not introduce a second retry loop.

MCP recovery validates a new connection and schema before replacing live handles. Schema changes publish after a tool batch at a safe request boundary; restricted runtimes keep their capability limits. Reconnection never replays business operations. Detailed state and budgets are in [Troubleshooting](../guides/troubleshooting.md).

## Where to extend

Use [`ExtensionAPI`](../../../src/virgo/harness/extensions.py) for tools, commands, event handlers and prompt sections. Use an AI adapter for a new model protocol, a shell adapter for a new command language, and a language pack for another display language. Keep policy in the harness/features and low-level execution in tools/adapters.

Continue with [Extensions](extensions.md), [Localization](localization.md), or [Development and CI](testing-and-ci.md).
