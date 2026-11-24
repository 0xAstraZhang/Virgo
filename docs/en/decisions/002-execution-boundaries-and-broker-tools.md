# 002 — Layered execution boundaries and Broker file tools

[Decisions](README.md) · [Documentation](../README.md) · [简体中文](../../zh-CN/decisions/002-execution-boundaries-and-broker-tools.md)

**Status:** Accepted

## Context

Running every tool—including simple file reads, trusted Python extensions, and MCP servers—inside an OS sandbox adds helper-process startup overhead, complicates approval UX, and creates brittle interactions with user-installed tools. Conversely, allowing arbitrary Shell commands to run without OS-level containment exposes the workspace boundary and ambient credentials.

## Decision

- **Single deny-first policy model**: `virgo.execution_policy.PermissionPolicy` is the single immutable source of truth shared across Broker file tools and native Shell sandboxes across three explicit modes (`read-only`, `workspace-edit`, `no-limit`). Retired legacy policy keys and CLI flags fail explicitly instead of silently migrating.
- **Broker-direct file tools**: Built-in file tools (`read`, `write`, `edit`, `glob`, `grep` in `virgo.tools.fs` + `virgo.file_access`) enforce `PermissionPolicy` directly inside the Broker with final-component `O_NOFOLLOW` and atomic temp-file `os.replace`, with no separate OS sandbox helper process.
- **OS-sandboxed Shell only**: Arbitrary model-spawned commands execute through `SandboxExecutor.run_process(ProcessRequest, context, cancellation)` (`virgo.execution`), compiled into the platform's native sandbox in **Read-Only** and **Workspace-Edit**.
- **Host trusted integrations**: Trusted Python extensions, command hooks, and MCP clients/stdio servers (`HostContext.unrestricted`) run on Host rather than inside the OS sandbox; trust is gated at install or explicit project approval.
- **Immutable per-task context capture**: Each task captures its originating `HostContext` or `SandboxContext` and `ExecutionOrigin` at launch; child subagents and background jobs inherit that snapshot even if the user later changes mode, branch, or UI session.
- **Fail closed, never silently downgrade or replay**: If a native sandbox backend cannot start before payload release, Virgo fails closed (offering an explicit one-shot **No-Limit** choice in interactive Shell sessions without altering session mode). Any failure after payload release (`ExecutionFailed`) never triggers automatic replay.

## Tradeoffs and constraints

- **Constant-time file operations**: Avoiding a file-tool sandbox helper keeps Broker `read` O(1) across repository sizes and lets out-of-scope file prompts resolve symlinks and execute immediately upon approval.
- **Strict execution boundary invariants**: Changes to `virgo.execution` or `virgo.sandbox` must preserve startup acknowledgement, cancellation, process-tree cleanup, and no-replay semantics.
- **Hard denies stay non-grantable**: Private credential paths cannot be overridden through `request_permissions` or saved grants.

## Related

- [Execution interface](../development/execution-interface.md)
- [Permissions & approvals](../guides/permissions.md)
- [Execution boundaries & sandbox](../guides/sandbox.md)
