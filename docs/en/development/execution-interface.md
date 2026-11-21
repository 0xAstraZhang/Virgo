# Execution interface

Execution is core infrastructure across `virgo.execution_policy`, `virgo.execution`, `virgo.file_access`, `virgo.output_store`, and `virgo.sandbox`. The session selects **Read-Only**, **Workspace-Edit**, or **No-Limit** (`execution.mode`) and captures the originating task's immutable execution context (`HostContext` or `SandboxContext`). Child and deferred tasks retain that context instead of reading the active UI session.

## Policy and process launch

- `virgo.execution_policy.PermissionPolicy` is the single deny-first policy data model shared by Broker file tools and native Shell sandboxes. Core policy definitions (`ExecutionMode`, `DEFAULT_MODES`, `HOST_MODE`, `PermissionSnapshot`, `PROTECTED_NAMES`) live in core with no reverse imports from `virgo.harness`.
- `SandboxExecutor.run_process(request, context, cancellation)` is the single local process entry point. `ProcessRequest` specifies explicit `argv`, `cwd`, base `env`, optional validated `env_overrides`, and stdio streams. Unrestricted contexts (including No-Limit Shell and Host MCP servers via `HostContext.unrestricted`) run directly on Host without probing native sandbox backends.
- Native Shell adapters compile `PermissionPolicy` into `bubblewrap` + `libseccomp2` on Linux (including anonymous Unix stream `socketpair` for `asyncio`), direct `/usr/bin/sandbox-exec` (Seatbelt) + `HostProcess` process-group supervision on macOS, and Standard AppContainer (`windows-appcontainer`, session-level single SID + single-call OS-propagated `SetNamedSecurityInfoW` inheritable ACEs, `protected_readonly` VER-01 without `FILE_DELETE_CHILD`, session-local `DefineDosDeviceW` virtual drive when ancestors lack `S-1-15-2-1`, write-ahead JSON crash-recovery journal, and `WindowsProcess` kill-on-close Job Object supervision) on native Windows. Startup acknowledgement, cancellation, process-tree cleanup, and no-replay diagnostics remain owned by those implementations.
- An unavailable native boundary never silently falls back to Host (interactive Shell sessions may ask the user for an explicit one-shot No-Limit fallback before payload start). Any failure after payload release (`ExecutionFailed`) never triggers automatic replay.

## Broker file tools, OutputStore, and Host MCP

- **File tools** (`virgo.tools.fs` + `virgo.file_access`) enforce `PermissionPolicy` directly inside the Broker with final-component `O_NOFOLLOW` and atomic temp-file `os.replace`. There is no separate OS sandbox helper process for file tools (Principle R4).
- **Command outputs** (`virgo.output_store.OutputStore`) are piped to the Broker and persisted per session under `<VIRGO_HOME>/outputs/session-<id>` with UTF-8 boundary-aligned pagination (`read_output`), session-trash cleanup, and a 7-day TTL. Untruncated foreground commands unlink their temporary spool immediately.
- **MCP servers** (`extensions/mcp.py`) launch on Host via `HostContext.unrestricted` and `SandboxExecutor` with one shared connection per server across `main` and `subagent` runtimes. Project `.virgo/mcp.json` requires explicit project trust, and persistent `"Always allow"` approvals bind to the SHA-256 fingerprint of the server's expanded `command`, `args`, and `env`.
- Each POSIX Host command owns a process group (`HostProcess`); Windows Host commands run inside a kill-on-close Job Object (`WindowsProcess`).
