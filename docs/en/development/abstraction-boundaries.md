# Abstraction boundaries

[Architecture](architecture.md) · [Extensions](extensions.md) · [Test retirements](test-retirements.md) · [简体中文](../../zh-CN/development/abstraction-boundaries.md)

The AI, Agent, Harness/Features and CLI layers separate transport, execution and application policy. Shared rules have one owner; callers use that owner instead of duplicating policy or runtime identity checks.

## Ownership of shared rules

| Rule | Owner | Callers and boundaries |
| --- | --- | --- |
| OpenAI reasoning-effort mapping | `ai/providers/reasoning.py` | Chat, Responses, Codex and `completion_request_metadata()` share pure functions. Explicit maps, model declarations and default fallbacks retain their precedence. Adapters still own HTTP clients, authentication and stream events. |
| Permission modes, labels and plan-mode tool filtering | `harness/permissions.py` | Harness and CLI use the shared contract. `features/permission.py` implements approval, rules, caches and auditing, and re-exports the old public names. Labels accept an optional session translator. |
| Trusted Runtime for a tool context | `AgentSession.runtime_for_context()` | Diagnostics and MCP share identity validation; each feature handles denial and its subsequent operation. Resolution does not consult the selected UI conversation or grant diagnostic access. |

Reasoning mapping shares only equivalent semantics: Chat-compatible APIs support `reasoningEffortMap`, while Responses/Codex retain their `max → xhigh` fallback. Keep protocol differences explicit instead of hiding them in a generic provider with many branches. Diagnostic metadata must use the same request policy.

Runtime resolution requires a `ReviewSource` whose runtime is a real `Runtime` owned by this Session, whose source object is the Runtime's original binding, and whose agent name matches the tool context. Any mismatch returns `None`; missing context must never fall back to `session.main`.

## Layers to retain

- `Agent` owns messages, queues, cancellation and subscriptions; `run_loop()` orders model and tool execution. Tool errors, execution facts and final result projection stay near this lifecycle.
- `Runtime` owns one conversation's request policy; `AgentSession` owns creation, switching, services and storage ownership. Candidate construction stays separate from activation so failure does not alter the active conversation.
- CLI controllers, the thread bridge, Transcript and the Textual App own scheduling, cross-thread messages, rendering and interaction. Splitting the App solely by file length would cut across conversation panes and modal lifecycles.
- Provider adapters translate protocol requests/events. Tools divide filesystem, shell, output-budget and platform-process responsibilities. Existing adapter reuse and compatibility entry points remain useful; removing every deferred import is not a reason for a new framework.
- Tests retain distinct failure modes and real integration boundaries. Remove a shallow duplicate only when existing fuller scenarios cover it; CI continues to display the deletion and validate retained coverage.

## Interfaces needing separate design

### Custom permission policy capabilities

`PermissionPolicy` declares `preflight()` and `before_execute()`, the two ordinary tool checkpoints. Some built-ins need more: `WebAccess._permitted()` uses `on_tool_call`, `mode_for()` and `decide()`; subagents and teammates use `set_agent_mode()`. Implementing only the minimal protocol does not establish compatibility with those features.

Extensions should wrap and forward the built-in policy as described in [Extensions](extensions.md#events-and-policies). Supporting independent minimal policies would first require explicit contracts or denial behavior for these capabilities, verified through hosted-web, subagent and teammate scenarios before implementation. The shared-helper extraction does not change permission decisions or extension capabilities.

### MCP tool registry publication

`MCPConnections._publish()` still updates Session tool entries directly. Additional dynamic capabilities may justify a Session-owned batch replacement API, with explicit guarantees for name collisions, old tool identity, concurrent calls and candidate failure. Moving tuple operations into a generic setter alone does not solve those boundaries.

## Existing validation entry points

Request mapping is covered by `test_credentials.py`, `test_codex.py`, `test_responses.py` and `test_auxiliary_requests.py`. CLI arguments, TUI mode switching, plan mode and execution-time permission checks cover permission compatibility. `test_diagnostic_access.py` and `test_mcp_recovery.py` cover missing, foreign and forged sources as well as conversation switching. These are existing product scenarios; the shared-helper extraction introduced no additional product tests.
