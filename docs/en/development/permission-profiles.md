# Permission modes and execution

| Mode | File access | Public web tools | Shell network | Provider |
| --- | --- | --- | --- | --- |
| Read-Only | Read the project and required runtime files. Commands have private scratch storage. | Allowed | Requires an operation grant | Native sandbox |
| Workspace-Edit | Read and write the real workspace. Protected resources include Git metadata and Virgo state. | Allowed | Allowed | Native sandbox |
| No-Limit | Host account rights | Allowed | Allowed | Host |

Fresh settings select No-Limit. Set `execution.mode` in user settings or use `--permission read-only`, `--permission workspace-edit` or `--permission no-limit`. The compatibility option `--execution host` selects No-Limit. Show the three choices with `/permissions list` or Shift+Tab in the TUI. Select the next mode with `/permissions <id>`. Use `/permissions show` to inspect the running task and the selection for its successor.

Core binds each task to its workspace and an immutable execution context. A queued child inherits the submitting task’s context. Switching modes affects new tasks. A child's `approval_policy="never"` applies to it and its descendants. File tools and Shell use the same project files. No-Limit uses Host directly. Sandbox selection loads the platform backend on demand. A backend refusal stops the operation. See the [platform support and filesystem contract](../guides/sandbox.md).

Core checks the exact tool call and its extra resources. An out-of-scope file read or write opens a confirmation for that operation before dispatch. A Shell command can request missing paths and networking together with `request_permissions` before execution. The confirmation shows the operation, working directory, additional resources and reason.

- **Allow once** binds the grant to one task and its exact operation. Virgo consumes it on dispatch. An explicitly requested grant expires after five minutes or when that run ends.
- **Always allow** stores the operation and approved resources in the user's `resource-grants.sqlite3`. Matches require the same project, mode, working directory, tool and complete arguments. Shell identity is part of the operation. Adding a write grant to an existing read grant prompts only for the additional write.
- **Deny** leaves the requested resource inaccessible. Existing processes keep the permissions they started with.

List saved rules for the active project with `/permissions grants`. Remove an entry with `/permissions revoke ID`. The TUI mode picker’s **Saved approvals** button opens the same listing. Project settings cannot install approvals. See the [configuration reference](../reference/configuration.md) and [migration guide](../guides/migrating-execution-policy.md).
