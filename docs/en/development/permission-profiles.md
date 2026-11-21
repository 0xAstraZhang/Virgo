# Permission modes and execution

| Mode | File access | Public web tools | Shell network | Provider |
| --- | --- | --- | --- | --- |
| Read-Only | Read the project and required runtime files. Commands have private scratch storage. | Allowed | Requires an operation grant | Native sandbox |
| Workspace-Edit | Read and write the real workspace. Protected resources (`.git`, `.virgo`, `AGENTS.md`, `CLAUDE.md`, interpreter roots) are write-denied. | Allowed | Allowed | Native sandbox |
| No-Limit | Host account rights | Allowed | Allowed | Host |

## Default mode and selection

- **Supported Tier 1 platforms (Linux, macOS, updated Windows 11 with `null.sys` Feature `60692039`, and WSL2)**: default is **Workspace-Edit**. Supported Windows 11 uses the unelevated Standard AppContainer backend (`windows-appcontainer`, no Administrator privileges required); older Windows builds where `\Device\Null` lacks `S-1-15-2-1` default to **No-Limit** with a one-time notice offering **1. WSL2**, **2. No-Limit**, and **3. Update Windows** (and fail closed if a sandbox mode is explicitly selected; see [native Windows sandbox](../guides/windows-sandbox.md) and [WSL2 setup](../guides/wsl2.md)).
- Configure `execution.mode` (`read-only`, `workspace-edit`, or `no-limit`) in user settings, or pass `--permission <mode>` on the command line. Retired keys (`sandbox`, `execution.provider`, `permissions.preset`/`presets`/`mode`/`modes`/`allow`/`deny`) and retired flags (`--execution`, `--mode`, `--sandbox`) are rejected explicitly with guidance to `execution.mode` / `--permission`.
- Inspect or change modes in-session with `/permissions show`, `/permissions list`, `/permissions <id>`, or Shift+Tab in the TUI. Switching modes applies to the next task; active tasks and their queued children keep their captured immutable execution context.

## Operation approvals, high-risk commands, and saved grants

Core checks each tool call against `PermissionPolicy` (deny-first):

- **File tools** (`read`, `write`, `edit`, `glob`, `grep`) run in the Broker directly against `PermissionPolicy`. Out-of-scope file reads/writes prompt before dispatch, resolving symlinks at approval time so dialogs show the real target path. Single-file approvals work directly without requiring parent-directory grants.
- **Shell commands** run under the OS sandbox in Read-Only and Workspace-Edit and may call `request_permissions` for additional existing paths or Shell networking (`network_allowed: true`).
- **High-risk Shell commands** (`git push --force`, pushes to protected branches, `git reset --hard`, `git clean -f`, `curl | sh`, recursive deletion outside the workspace, deploy/publish, cloud resource deletion, and destructive SQL/Redis statements) require one-shot user confirmation in **all** modes. Setting `permissions.confirmHighRisk: false` disables this confirmation **only** in **No-Limit**.
- **Sandbox unavailable fallback**: If the native sandbox backend cannot start before payload release, Virgo fails closed and—in interactive sessions—offers an explicit one-shot **No-Limit** confirmation for that single command (leaving the session mode unchanged).
- **Allow once** binds a grant to one run and exact operation, consumed on dispatch (explicit `request_permissions` grants expire after 5 minutes or when the run ends).
- **Always allow** stores approved grants in `<VIRGO_HOME>/permission-grants.sqlite3` (legacy `resource-grants.sqlite3` files are removed on startup without migration). List saved grants with `/permissions grants` and remove entries with `/permissions revoke ID`. Project settings cannot install approvals.
- **Trusted extensions and MCP tools** run on **Host** outside the native Shell sandbox. Read-only MCP/external tools run directly; mutating external tools prompt in Read-Only and Workspace-Edit (and skip prompting in No-Limit). MCP tools support **Always allow** bound to the SHA-256 fingerprint of the server's expanded `command`, `args`, and `env`—any change to server config automatically invalidates prior persistent grants.
