# Abstraction boundaries

[Architecture](architecture.md) · [Extensions](extensions.md) · [Test retirements](test-retirements.md) · [简体中文](../../zh-CN/development/abstraction-boundaries.md)

The AI, Agent, Harness/Plugins and CLI layers separate transport, execution and application policy. Shared rules have one owner; callers use that owner instead of duplicating policy or runtime identity checks.

## Ownership of shared rules

| Rule | Owner | Callers and boundaries |
| --- | --- | --- |
| OpenAI reasoning-effort mapping | `ai/providers/reasoning.py` | Chat, Responses, Codex and `completion_request_metadata()` share pure functions. Explicit maps, model declarations and default fallbacks retain their precedence. Adapters still own HTTP clients, authentication and stream events. |
| Execution and source | `virgo.execution`, `harness/execution.py`, `origin_context.py` | Host direct execution; Sandbox extension owns resource grants and isolation. |
| Trusted Runtime for a tool context | `AgentSession.runtime_for_context()` | Diagnostics and other privileged capabilities share identity validation; each feature handles denial and its subsequent operation. Resolution does not consult the selected UI conversation or grant diagnostic access. |

Reasoning mapping shares only equivalent semantics: Chat-compatible APIs support `reasoningEffortMap`, while Responses/Codex retain their `max → xhigh` fallback. Keep protocol differences explicit instead of hiding them in a generic provider with many branches. Diagnostic metadata must use the same request policy.

Runtime resolution requires a `ExecutionOrigin` whose runtime is a real `Runtime` owned by this Session, whose source object is the Runtime's original binding, and whose agent name matches the tool context. Any mismatch returns `None`; missing context must never fall back to `session.main`.

## Layers to retain

- `Agent` owns messages, queues, cancellation and subscriptions; `run_loop()` orders model and tool execution. Tool errors, execution facts and final result projection stay near this lifecycle.
- `Runtime` owns one conversation's request policy; `AgentSession` owns creation, switching, services and storage ownership. Candidate construction stays separate from activation so failure does not alter the active conversation.
- CLI controllers, the thread bridge, Transcript and the Textual App own scheduling, cross-thread messages, rendering and interaction. Splitting the App solely by file length would cut across conversation panes and modal lifecycles.
- Provider adapters translate protocol requests/events. Tools divide filesystem, shell, output-budget and platform-process responsibilities. Existing adapter reuse and compatibility entry points remain useful; removing every deferred import is not a reason for a new framework.
- Tests retain distinct failure modes and real integration boundaries. Remove a shallow duplicate only when existing fuller scenarios cover it; CI continues to display the deletion and validate retained coverage.

## Public plugin contracts

### Custom permission policy capabilities

`ExecutionProvider` supplies source capture, file access, process launch, external-operation admission and state. Register one provider owner before Runtime construction. `api.authorize_tool` retains source binding for adapted operations; `api.create_runtime(parent=..., execution_context=...)` inherits the submitted source context. Trusted Python extensions are outside this managed boundary.

Core scope detection and `ExecutionOrigin` live in the harness. Operations needing new authorization ask the user under `ask` and are refused under `never`. Unknown caller identity fails explicitly; it does not select the current UI conversation.

### Dynamic tool publication

`api.replace_tools(namespace, entries)` validates owned names and the entire replacement before atomic publication. A runtime updates schemas only at its next request boundary; running batches retain their existing tool objects. The [MCP extension](../../../extensions/README.md) validates candidate connections first, never replays business calls, and leaves a healthy active connection intact when a replacement fails.

### Registration and lifecycle

The existing `setup(api)` and event bus are retained. Core records registration ownership, rejects collisions, rolls back partial optional setup, and executes cleanup callbacks in reverse order with diagnostic reporting. Python plugins remain trusted in-process code; project trust is granted only by user settings or an explicit startup flag. Do not turn these contracts into a package manager, dependency solver or hot-reload system.

See [Core/plugin boundaries](core-plugin-boundaries.md) and the [public API](extensions.md) for the concrete interface.

## Existing validation entry points

Request mapping is covered by `test_credentials.py`, `test_codex.py`, `test_responses.py` and `test_auxiliary_requests.py`. CLI arguments, TUI mode switching and execution-time permission checks cover permission compatibility. `test_diagnostic_access.py` covers missing, foreign and forged sources as well as conversation switching. These existing product scenarios remain. The Core/plugin refactor added `test_core_plugins_e2e.py` before production changes to cover the new assembly, trust, ownership, cleanup, budget and migration contracts.
