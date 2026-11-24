# Execution interface

[Documentation](../README.md) · [Project home](../../../README.md) · [简体中文](../../zh-CN/development/execution-interface.md)

Execution is core infrastructure across `virgo.execution_policy`, `virgo.execution`, `virgo.file_access`, `virgo.output_store`, and `virgo.sandbox`. The session selects **Read-Only**, **Workspace-Edit**, or **No-Limit** (`execution.mode`) and captures the originating task's immutable execution context (`HostContext` or `SandboxContext`). Child and deferred tasks retain that context instead of reading the active UI session.

## Policy and process launch

- `virgo.execution_policy.PermissionPolicy` is the single deny-first policy data model shared by Broker file tools and native Shell sandboxes. Core policy definitions (`ExecutionMode`, `DEFAULT_MODES`, `HOST_MODE`, `PermissionSnapshot`, `PROTECTED_NAMES = (".virgo",)`, `PRIVATE_HOME_RELATIVES`, `default_private_paths`, `default_deny_rules`, `filter_sandbox_environment`) and `virgo.sandbox` native backends live in core with no reverse imports from `virgo.harness`. Hard `read`-denies on private credential paths are unoverridable (`hard_denied`), while confirmable `write`-denies on project `.virgo` can be overridden by explicit user grants (`policy.grant(...)`).
- `SandboxExecutor.run_process(request, context, cancellation)` is the single local process entry point. `ProcessRequest` specifies explicit `argv`, `cwd`, base `env`, optional validated `env_overrides`, and stdio streams. Unrestricted contexts (including No-Limit Shell and Host stdio MCP servers via `HostContext.unrestricted`) run directly on Host without probing native sandbox backends.
- Native Shell adapters compile `PermissionPolicy` into platform backends:
  - **Linux (`bubblewrap`)**: `--ro-bind / /` wide read-only host view, masked `private_paths`, dropped capabilities, `--info-fd` startup acknowledgement, and no `libseccomp` dependency.
  - **macOS (Seatbelt)**: direct `/usr/bin/sandbox-exec` (probed once per executor) + `HostProcess` process-group supervision.
  - **Native Windows (Standard AppContainer `windows-appcontainer`)**: NTFS and ReFS Dev Drive support, session-level single SID + single-call OS-propagated `SetNamedSecurityInfoW` inheritable ACEs, automatic read-only ACEs for `.venv` base Python and user-owned `PATH` toolchains, `protect_readonly_path()` on unapproved `.virgo`, session-local `DefineDosDeviceW` virtual drive when ancestors lack `S-1-15-2-1`, write-ahead JSON crash-recovery journal, and direct `CreateProcessW` + `WindowsProcess` kill-on-close Job Object supervision.
- Startup acknowledgement, cancellation, process-tree cleanup, and no-replay diagnostics are owned by those implementations. An unavailable native boundary never silently falls back to Host (interactive Shell sessions may ask the user for an explicit one-shot No-Limit fallback before payload start). Any failure after payload release (`ExecutionFailed`) never triggers automatic replay.

## Broker file tools, OutputStore, and Host MCP

- **File tools** (`virgo.tools.fs` + `virgo.file_access`) enforce `PermissionPolicy` directly inside the Broker with final-component `O_NOFOLLOW` and atomic temp-file `os.replace`, without a separate OS sandbox helper process (see [ADR 002](../decisions/002-execution-boundaries-and-broker-tools.md)).
- **Command outputs** (`virgo.output_store.OutputStore`) are piped to the Broker and persisted per session under `<VIRGO_HOME>/outputs/session-<id>` using compact 16-char IDs (`out_<12-hex>`, backward-compatible with legacy 48-hex IDs), UTF-8 boundary-aligned pagination (`read_output`), session-trash cleanup, and a 7-day TTL. Untruncated foreground commands unlink their temporary spool immediately.
- **MCP servers** share one connection across main and subagent runtimes. Virgo starts stdio servers via `HostContext.unrestricted` and `SandboxExecutor`; for URL transports, the Host client connects to the configured endpoint. Both enforce project trust and fingerprint-bound tool approvals (see [MCP configuration](../guides/extensions.md#mcp-mcp)).
- Each POSIX Host command owns a process group (`HostProcess`); Windows Host commands run inside a kill-on-close Job Object (`WindowsProcess`).

See also: [Architecture overview](architecture.md) · [Permissions & approvals](../guides/permissions.md) · [Execution acceptance](execution-acceptance.md) · [ADR 002](../decisions/002-execution-boundaries-and-broker-tools.md)
