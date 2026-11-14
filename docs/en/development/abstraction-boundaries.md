# Abstraction boundaries

[Architecture](architecture.md) · [Extensions](extensions.md) · [Test retirements](test-retirements.md) · [简体中文](../../zh-CN/development/abstraction-boundaries.md)

The AI, Agent, Harness/Plugins and CLI layers separate transport, execution and application policy. Shared rules have one owner; callers use that owner instead of duplicating policy or runtime identity checks.

## Ownership of shared rules

| Rule | Owner | Callers and boundaries |
| --- | --- | --- |
| OpenAI reasoning-effort mapping | `ai/providers/reasoning.py` | Chat, Responses, Codex and `completion_request_metadata()` share pure functions. Explicit maps, model declarations and default fallbacks retain their precedence. Adapters still own HTTP clients, authentication and stream events. |
| Permission modes, labels and plan-mode tool filtering | `harness/permissions.py` | Harness and CLI use the shared contract. `harness/permission_policy.py` implements mandatory rules, approval fallback, caches and auditing; `plugins/auto_review.py` supplies optional model review. Labels accept an optional session translator. |
| Trusted Runtime for a tool context | `AgentSession.runtime_for_context()` | Diagnostics and MCP share identity validation; each feature handles denial and its subsequent operation. Resolution does not consult the selected UI conversation or grant diagnostic access. |

Reasoning mapping shares only equivalent semantics: Chat-compatible APIs support `reasoningEffortMap`, while Responses/Codex retain their `max → xhigh` fallback. Keep protocol differences explicit instead of hiding them in a generic provider with many branches. Diagnostic metadata must use the same request policy.

Runtime resolution requires a `ReviewSource` whose runtime is a real `Runtime` owned by this Session, whose source object is the Runtime's original binding, and whose agent name matches the tool context. Any mismatch returns `None`; missing context must never fall back to `session.main`.

## Layers to retain

- `Agent` owns messages, queues, cancellation and subscriptions; `run_loop()` orders model and tool execution. Tool errors, execution facts and final result projection stay near this lifecycle.
- `Runtime` owns one conversation's request policy; `AgentSession` owns creation, switching, services and storage ownership. Candidate construction stays separate from activation so failure does not alter the active conversation.
- CLI controllers, the thread bridge, Transcript and the Textual App own scheduling, cross-thread messages, rendering and interaction. Splitting the App solely by file length would cut across conversation panes and modal lifecycles.
- Provider adapters translate protocol requests/events. Tools divide filesystem, shell, output-budget and platform-process responsibilities. Existing adapter reuse and compatibility entry points remain useful; removing every deferred import is not a reason for a new framework.
- Tests retain distinct failure modes and real integration boundaries. Remove a shallow duplicate only when existing fuller scenarios cover it; CI continues to display the deletion and validate retained coverage.

## Public plugin contracts

### Custom permission policy capabilities

`PermissionPolicy` declares `preflight()` and `before_execute()`, the ordinary tool checkpoints. `api.register_permission_policy()` composes extra restrictions with the mandatory Core policy. Hosted web calls use `api.authorize_tool`; child creation uses `api.create_runtime(parent=..., permission=...)`. Official plugins do not replace the Core guard or set another runtime's mode directly.

Core scope detection and `ReviewSource` live in the harness. A disabled or failed optional model reviewer leaves human approval as the fallback for uncertain operations. Unknown caller identity fails explicitly; it does not select the current UI conversation.

### MCP tool registry publication

`api.replace_tools(namespace, entries)` validates owned names and the entire replacement before atomic publication. A runtime updates schemas only at its next request boundary; running batches retain their existing tool objects. MCP validates candidates first and never replays business calls. A transport generation that has been replaced may reject a stale handle conservatively. Candidate failure must leave a healthy active connection intact.

### Registration and lifecycle

The existing `setup(api)` and event bus are retained. Core records registration ownership, rejects collisions, rolls back partial optional setup, and executes cleanup callbacks in reverse order with diagnostic reporting. Python plugins remain trusted in-process code; project trust is granted only by user settings or an explicit startup flag. Do not turn these contracts into a package manager, dependency solver or hot-reload system.

See [Core/plugin boundaries](core-plugin-boundaries.md) and the [public API](extensions.md) for the concrete interface.

## Existing validation entry points

Request mapping is covered by `test_credentials.py`, `test_codex.py`, `test_responses.py` and `test_auxiliary_requests.py`. CLI arguments, TUI mode switching, plan mode and execution-time permission checks cover permission compatibility. `test_diagnostic_access.py` and `test_mcp_recovery.py` cover missing, foreign and forged sources as well as conversation switching. These existing product scenarios remain. The Core/plugin refactor added `test_core_plugins_e2e.py` before production changes to cover the new assembly, trust, ownership, cleanup, budget and migration contracts.
