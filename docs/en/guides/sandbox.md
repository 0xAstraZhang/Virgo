# Native command sandbox

No-Limit runs on Host with the current account's permissions. Read-Only and Workspace-Edit load the Sandbox extension on selection. Both allow managed web search and reading. Shell networking starts disabled in Read-Only and enabled in Workspace-Edit. See [permission modes and approvals](../development/permission-profiles.md).

## Live workspace

The command's working tree is the same directory edited by file tools and host editors. File tools open retained objects, stage changed content and replace the destination. A watcher can reopen the path immediately after the edit. Commands have private writable scratch and a private home. Core owns the output handles in the originating workspace.

Linux uses Bubblewrap and a libseccomp filter. macOS uses Seatbelt and a launchd coalition supervisor. Windows uses LPAC, a write-restricted token, per-process ACL grants and a Job Object. A failed preflight or native launch reports the error to the caller. Each running process retains the resources it was granted at launch.

## File protection

Existing workspace control metadata and nested interpreter roots remain protected. The POSIX launch check rejects multiply linked writable files. The supported live-workspace contract requires trusted host writers to preserve the filesystem boundary during a command. A hostile concurrent host writer can change what a pathname denotes between checks or after launch. The startup scan does not continuously protect against that behavior. An external Shell grant requires an existing, exact file or directory. File-level approval never substitutes its parent directory.

The broker and trusted extensions retain their own OS authority. Use the final revision's native results and full-suite comparison for release decisions. [Acceptance evidence](../development/execution-acceptance.md) · [Windows platform scope](windows-sandbox.md)
