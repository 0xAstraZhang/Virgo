# Permission modes and execution

| Mode | File access | Public web tools | Shell network | Provider |
| --- | --- | --- | --- | --- |
| Read-Only | Read the project and required runtime files. Commands have private scratch storage. | Allowed | Requires an operation grant | Native sandbox |
| Workspace-Edit | Read and write the real workspace. Protected resources include Git metadata and Virgo state. | Allowed | Allowed | Native sandbox |
| No-Limit | Host account rights | Allowed | Allowed | Host |

With no user settings file, Virgo starts in Workspace-Edit. Existing user files keep the old implicit Workspace-Edit mode. Set `execution.mode` in user settings or use `--permission read-only`, `--permission workspace-edit` or `--permission no-limit`. The compatibility option `--execution host` selects No-Limit. Show the three choices with `/permissions list` or Shift+Tab in the TUI. Select the next mode with `/permissions <id>`. Use `/permissions show` to inspect the running task and the selection for its successor.

Core binds each task to its workspace and an immutable execution context. A queued child inherits the submitting task’s context. Switching modes affects new tasks. A child's `approval_policy="never"` applies to it and its descendants. File tools and Shell use the same project files and executor. No-Limit selects an unrestricted policy on that executor. A backend refusal stops a restricted operation. See the [platform support and filesystem contract](../guides/sandbox.md).

Session initialization creates a missing Virgo user state directory with mode `0700` before capturing execution permissions or reading automatic project context. This also applies to in-memory sessions. Restricted policies keep the private directory as an explicit read-deny root; startup does not wait for the first output record to create it. Failure to create the directory stops initialization rather than dropping its deny rule.

`PermissionPolicy` contains filesystem rules, a Boolean `network_allowed`, and an explicit `unrestricted` flag. Each filesystem rule identifies a path subtree, read or write access, and allow or deny. A write allow includes reading. A read deny also prevents writing. All matching denies override all allows, regardless of order or how specific an allow is. A protected write deny can therefore keep control metadata readable without letting a later operation grant modify it. The executor must establish the requested rules or reject the launch; a rule in a policy object is not proof that a backend enforced it.

Use `request_permissions` before an operation that needs extra paths or networking. Its confirmation shows the exact operation, working directory, additional resources and reason. Approval appends allows to this operation's effective policy and never overrides a deny. File operations otherwise reach the native executor, which enforces the current policy. There is no retry or automatic replay after an operation might have run.

An atomic file write or edit requires write permission for its destination directory. Request that existing directory explicitly in `write_paths`; Virgo does not silently expand a file grant to its parent. Shell can still request an existing exact file scope when its operation only needs that scope. Paths are normalized lexically for approval, without resolving or probing the model's target on the host. The restricted process performs file resolution and access. Networking accepts only `true` or `false`; directional grants are no longer accepted.

- **Allow once** binds the grant to one task and its exact operation. Virgo consumes it on dispatch. An explicitly requested grant expires after five minutes or when that run ends.
- **Always allow** stores the operation and approved resources in the user's `resource-grants.sqlite3`. Matches require the same project, mode, working directory, tool and complete arguments. Shell identity is part of the operation. Adding a write grant to an existing read grant prompts only for the additional write.
- **Deny** leaves the requested resource inaccessible. Existing processes keep the permissions they started with.
- Local MCP servers installed through the stdio executor adapter run under the originating operation policy, including exact transient grants. They are process tools, so a server-provided read-only hint never substitutes for OS enforcement. Trusted Python extension adapters and explicitly trusted host hooks retain host authority. Other external tools use their separate per-call capability approval; read-only declarations do not place those external services inside the local sandbox. No-Limit remains explicit host authority.

List saved rules for the active project with `/permissions grants`. Remove an entry with `/permissions revoke ID`. The TUI mode picker’s **Saved approvals** button opens the same listing. Project settings cannot install approvals. [Migrating permission settings](../guides/migrating-execution-policy.md)

The old saved-grant format inferred whether a path was a file or directory by inspecting it on the host. Those records are retired rather than reinterpreted as subtree grants. Existing approvals must be granted again; the obsolete table is removed when a new approval is saved.

## Runtime reads and platform scope

`PermissionPolicy.allows()` describes explicit task grants. Native processes also require readable operating-system and interpreter dependencies. Established boundary records list these under `runtime_read_dependencies`; Windows additionally reports ambient LPAC-readable objects under `ambient_read_access`. These additions also apply to the filesystem helper. A read deny overrides both explicit grants and these additional read sources, or the executor rejects the unsupported boundary. The supported portable guarantee is explicit deny precedence, not identical filesystem read allowlists on all platforms. Before a native launch is verified, `effective` is unknown.
