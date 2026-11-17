# Neutral execution contracts

`virgo.execution` defines execution context, provider and process-handle protocols without importing an isolation backend. `HostContext` supplies direct filesystem/process operations with host OS authority. A process handle supports waiting, cancellation and cleanup. Source-runtime validation and provider selection belong to the session integration layer.

This foundation does not by itself change the session default or certify Sandbox behavior. The dependent execution-policy migration binds the provider before Runtime construction and captures task contexts. File matching, editing and result formatting remain tool business logic. An execution failure after release cannot imply that no side effects occurred.

Host POSIX cleanup covers the live process group. Windows delegates to the existing host Job Object process implementation. These lifecycle operations are not an isolation claim.
