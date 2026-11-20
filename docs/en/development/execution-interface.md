# Execution interface

Execution is core infrastructure in `virgo.execution` and `virgo.sandbox`. The session selects Read-Only, Workspace-Edit or No-Limit and captures the originating task's immutable execution context. Child and deferred tasks retain that context instead of reading the selected UI session.

`SandboxExecutor.run_process(ProcessRequest, policy, cancellation)` is the common local process entry point. `ProcessRequest` contains explicit argv, cwd, environment and stream settings. The caller supplies effective authorization; the executor establishes the native boundary before payload release. No-Limit goes through the same executor with explicit host authority and does not require native backend discovery.

The native adapter reuses bubblewrap/seccomp on Linux, Seatbelt with launchd supervision on macOS, and LPAC/restricted tokens/ACLs/Job Objects on Windows. Startup acknowledgement, cancellation, process-tree cleanup and no-replay diagnostics remain owned by those implementations. An unavailable boundary never falls back to Host. A failure after release may already have effects and must not trigger automatic replay.

File operations still use the captured context in this migration stage. The next dependent change introduces a bounded Filesystem Helper protocol through the same executor; until that migration, file tools retain the existing Broker guards. This process refactor does not claim that file tools already execute in native isolation.

Each POSIX Host command owns a process group. On leader exit the handle signals remaining group members before reaping the leader. The OS delivers the signal asynchronously. Concurrent cancellation and repeated cleanup retain the original command status. The group covers descendants that stay in it. Windows delegates process lifetime to the Host Job Object implementation.
