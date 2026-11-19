# Execution interface

Execution is core infrastructure in `virgo.execution` and `virgo.sandbox`. The session selects Read-Only, Workspace-Edit or No-Limit and captures the originating task's immutable execution context. Child and deferred tasks retain that context instead of reading the selected UI session.

`SandboxExecutor.run_process(ProcessRequest, policy, cancellation)` is the common local process entry point. `ProcessRequest` contains explicit argv, cwd, environment and stream settings. The caller supplies effective authorization; the executor establishes the native boundary before payload release. No-Limit goes through the same executor with explicit host authority and does not require native backend discovery.

The native adapter reuses bubblewrap/seccomp on Linux, Seatbelt with launchd supervision on macOS, and LPAC/restricted tokens/ACLs/Job Objects on Windows. Startup acknowledgement, cancellation, process-tree cleanup and no-replay diagnostics remain owned by those implementations. An unavailable boundary never falls back to Host. A failure after release may already have effects and must not trigger automatic replay.

`SandboxExecutor.run_file_operation()` runs the standard-library Filesystem Helper through that same process entry point. Read, Write, Edit, Glob and Grep pass raw path arguments to the helper. Path resolution, metadata probes, traversal and content access happen in its process. Internal project-instruction and template reads use the same boundary. No-Limit also starts the helper; choosing host authority does not restore direct Broker file mutations.

The executor captures the helper and its Windows companion once, before model operations, and starts the captured interpreter with `-I -S`. Subsequent edits to an editable Virgo installation do not replace this running session's worker. Restricted process requests protect the stable per-user cache runtime parent, including snapshots created after a process starts and when the workspace includes that parent. The runtime location does not depend on command `TMPDIR`.

Requests and responses use JSON on private stdin/stdout pipes, with a four-byte length prefix and a 16 MiB frame limit. stderr carries bounded diagnostics. The executor owns cancellation, startup evidence and process-tree cleanup. A malformed response or a failure after launch is not permission to replay a mutation. See [Filesystem Helper and output records](filesystem-helper.md) for operation and metadata limits.

Shell and tool output belongs to a Broker `OutputStore` under the Virgo user directory. Children receive a pipe; the Broker writes the private record. Model results contain an opaque `output_id`, and `read_output` pages the registered record for its originating conversation. A filesystem path or an ID belonging to another conversation is not an output capability.

Each POSIX Host command owns a process group. On leader exit the handle signals remaining group members before reaping the leader. The OS delivers the signal asynchronously. Concurrent cancellation and repeated cleanup retain the original command status. The group covers descendants that stay in it. Windows delegates process lifetime to the Host Job Object implementation.
