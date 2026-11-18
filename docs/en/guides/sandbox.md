# Native command sandbox

No-Limit runs on Host with the current account's permissions. Read-Only and Workspace-Edit load the Sandbox extension on selection. Both allow managed web search and reading. Shell networking starts disabled in Read-Only and enabled in Workspace-Edit. See [permission modes and approvals](../development/permission-profiles.md).

## Live workspace

The command's working tree is the same directory edited by file tools and host editors. File tools open retained objects, stage changed content and replace the destination. A watcher can reopen the path immediately after the edit. Commands have private writable scratch and a private home. In Read-Only, Core stores command output under the Virgo user directory, keyed by workspace. Workspace-Edit stores it under the project’s protected .virgo/outputs directory.

Linux requires system Bubblewrap, libseccomp and permission to create namespaces. macOS uses the system `sandbox-exec` and a launchd supervisor to track the command and its descendants. Windows uses LPAC, a restricted token, temporary ACLs and a Job Object. If the platform cannot establish the requested boundary, the operation reports an error.

For a Read-Only command that needs networking, request network access for that invocation. The native backends support on/off networking. A request limited to a single direction is refused. Each running process retains the permissions it received at launch.

## File protection

Existing workspace control metadata and nested interpreter roots remain protected. The POSIX launch check rejects a writable file when any of its links is outside the writable scope; files whose links are all inside the workspace remain usable. Package managers that hardlink from a store outside the workspace (for example pnpm's global store) still hit this check, so use No-Limit or keep the store inside the workspace. The supported live-workspace contract requires trusted host writers to preserve the filesystem boundary during a command. A hostile concurrent host writer can change what a pathname denotes between checks or after launch. The startup scan does not continuously protect against that behavior. An external Shell grant requires an existing, exact file or directory. File-level approval never substitutes its parent directory.

The broker and trusted extensions retain their own OS authority. Use the final revision's native results and full-suite comparison for release decisions. [Acceptance evidence](../development/execution-acceptance.md) · [Windows platform scope](windows-sandbox.md)
