# Neutral execution contracts

`virgo.execution` defines execution context, provider and process-handle protocols without importing an isolation backend. `HostContext` supplies direct filesystem/process operations with host OS authority. A process handle supports waiting, cancellation and cleanup. Source-runtime validation and provider selection belong to the session integration layer.

This foundation does not by itself change the session default or certify Sandbox behavior. The dependent execution-policy migration binds the provider before Runtime construction and captures task contexts. File matching, editing and result formatting remain tool business logic. An execution failure after release cannot imply that no side effects occurred.

Each POSIX Host command owns a new process group. On leader exit, the handle signals remaining group members for termination before reaping the leader and releasing its group ID. The OS delivers the signal asynchronously. Concurrent cancellation and repeated cleanup retain the original command status. Descendants that deliberately move to another group lie outside this guarantee. Windows delegates to the existing host Job Object process implementation. These lifecycle operations are not an isolation claim.
