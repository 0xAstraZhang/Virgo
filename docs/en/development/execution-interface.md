# Execution interface

The session selects one of three modes. No-Limit uses `HostContext` with the current account's file and network permissions. Read-Only and Workspace-Edit use an execution context supplied by the Sandbox extension. The Core captures that context for the originating task, checks the operation's resource requirements, and passes effective permissions to the provider. Child tasks keep their parent's permission ceiling.

`virgo.execution` defines the shared file and process interfaces. A Host file commit writes the original workspace path, and a command starts in the supplied working directory. A No-Limit command starts directly on Host and requires no sandbox discovery or resource approval. Sandbox providers implement the same interfaces and must report an unavailable boundary before releasing a command. After release, a failure can leave effects in the workspace.

Each POSIX Host command owns a process group. On leader exit the handle signals remaining group members before reaping the leader. The OS delivers the signal asynchronously. Concurrent cancellation and repeated cleanup retain the original command status. The group covers descendants that stay in it. Windows delegates process lifetime to the Host Job Object implementation.
