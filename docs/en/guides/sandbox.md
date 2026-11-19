# Native local execution

No-Limit runs through the same executor with the current account's Host permissions. Read-Only and Workspace-Edit select the core native executor. Both allow managed web search and reading. Shell networking starts disabled in Read-Only and enabled in Workspace-Edit. See [permission modes and approvals](../development/permission-profiles.md).

Trusted Python callers using standalone `BASH`, `run_command()` or `spawn()` without a context explicitly select unrestricted execution through `SandboxExecutor`. Session tools supply their originating context. `BASH` and `run_command()` capture output in private Broker records; `spawn()` callers own their returned process handle and streams.

## Live workspace

The command's working tree is the same directory edited by file tools and host editors. Read, Write, Edit, Glob and Grep run in a captured Filesystem Helper through the same executor as Shell. The helper resolves target paths, opens retained objects, stages changed content and replaces the destination. A watcher can reopen the path immediately after the edit. Commands have private writable scratch and a private home.

All modes store command output in private Broker records under the Virgo user directory. Shell receives a pipe, and results expose an opaque `output_id`. Use `read_output` to page a record owned by the originating conversation. File tools do not receive access to the private output directory. See [Filesystem Helper and output records](../development/filesystem-helper.md).

Linux requires system Bubblewrap, libseccomp and permission to create namespaces. macOS uses the system `sandbox-exec` and a launchd supervisor to track the command and its descendants. Windows uses LPAC, a restricted token, temporary ACLs and a Job Object. If the platform cannot establish the requested boundary, the operation reports an error.

Task allow rules are not a complete, identical read whitelist on every platform. Linux and macOS also expose system, interpreter and trusted worker dependencies needed to run the payload. Windows retains resources already readable through its ambient LPAC capabilities. Explicit deny rules take precedence over these reads where the backend supports them; an unsupported deny is refused before payload release.

For a Read-Only command that needs networking, request network access for that invocation. Network policy is a Boolean; directional lists are rejected. Each running process retains the permissions it received at launch.

## File protection

An explicit read deny blocks reads and writes; a write deny leaves reads available. Denies take precedence over every overlapping allow. The native compiler requires explicit deny roots to be exact existing files or directories, and refuses unsupported roots before launching the payload. Existing control metadata and the trusted execution runtime receive write-deny rules. This does not promise protection for arbitrary reserved names that do not yet exist.

The native backend permits a workspace containing an interpreter or private Virgo storage, including the user's home, when it can install the required carve-outs. Runtime files remain read-only and private storage is inaccessible. Ancestors of these roots cannot be renamed or removed to move a protected subtree into a writable path. Ordinary siblings remain usable. A workspace enclosing operating-system mount roots remains unsupported.

POSIX no longer scans the entire writable tree for hardlinks at startup. Native pathname and mount restrictions do not guarantee that a preexisting writable hardlink cannot modify an inode also named outside the granted tree. Filesystem Helper mutations use retained opens and atomic replacement; that behavior is separate from unrestricted commands operating on preexisting aliases. Windows retains its recursive ACL and hardlink validation. The live-workspace contract requires trusted host writers to preserve the filesystem boundary while a command runs; a hostile host writer can change path identities outside the sandbox.

An external Shell grant requires an existing, exact file or directory. File-level approval never substitutes its parent directory. Atomic filesystem Write/Edit require an explicitly approved destination directory. On Windows, replaced files inherit their parent directory's ACL instead of retaining custom explicit per-file ACLs.

The broker and trusted extensions retain their own OS authority. The built-in file tools use the helper even in No-Limit. Use the final revision's native results and full-suite comparison for release decisions. [Acceptance evidence](../development/execution-acceptance.md) · [Windows platform scope](windows-sandbox.md)
