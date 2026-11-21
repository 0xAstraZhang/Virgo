# Permission modes and execution

| Mode | File access | Public web tools | Shell network | Provider |
| --- | --- | --- | --- | --- |
| Read-Only | Broker file tools default to the workspace (external paths prompt once per prefix); POSIX Shell has read-only host access excluding private credential paths, plus writable per-command scratch and session cache. | Allowed | Requires a resource grant | Native sandbox |
| Workspace-Edit | Read and write the live workspace (`.git`, `AGENTS.md`, `CLAUDE.md`, and `.venv` are open; project `.virgo` confirms on write; private credential paths are hard-denied). | Allowed | Allowed | Native sandbox |
| No-Limit | Host account rights | Allowed | Allowed | Host |

## Default mode and selection

- **Supported Tier 1 platforms (Linux, macOS, updated Windows 11 with `null.sys` Feature `60692039`, and WSL2)**: default is **Workspace-Edit**. Supported Windows 11 uses the unelevated Standard AppContainer backend (`windows-appcontainer`, no Administrator privileges required); older Windows builds where `\Device\Null` lacks `S-1-15-2-1` default to **No-Limit** with a one-time notice offering **1. WSL2**, **2. No-Limit**, and **3. Update Windows** (and fail closed if a sandbox mode is explicitly selected; see [native Windows sandbox](../guides/windows-sandbox.md) and [WSL2 setup](../guides/wsl2.md)).
- Configure `execution.mode` (`read-only`, `workspace-edit`, or `no-limit`) in user settings, or pass `--permission <mode>` on the command line. Retired settings keys (`sandbox`, `execution.provider`, `permissions.preset`/`presets`/`mode`/`modes`/`allow`/`deny`) fail explicitly with guidance to migrate to `execution.mode`, and removed legacy CLI flags (`--execution`, `--mode`, `--sandbox`) are rejected as unrecognized arguments.
- Inspect or change modes in-session with `/permissions show`, `/permissions list`, `/permissions <id>`, or Shift+Tab in the TUI. Switching modes applies to the next task; active tasks and their queued children keep their captured immutable execution context.

## Resource approvals, high-risk commands, and saved grants

Core checks each tool call against `PermissionPolicy` (deny-first):

- **File tools** (`read`, `write`, `edit`, `glob`, `grep`) run in the Broker directly against `PermissionPolicy`. Out-of-scope file reads/writes and `.virgo` writes prompt on demand before dispatch, resolving symlinks at approval time so dialogs show the real target path, and execute immediately upon approval without requiring a two-phase retry. High-sensitivity credential paths (`~/.virgo`, `~/.ssh`, `~/.aws`, etc.) are hard-denied and cannot be granted.
- **Shell commands** run under the OS sandbox in Read-Only and Workspace-Edit and may call `request_permissions` (with `tool`/`arguments` optional) for additional path prefixes or Shell networking (`network_allowed: true`), including batching `request_permissions` right before the Shell call in the same turn.
- **High-risk Shell commands** (`git push --force`, pushes to protected branches, `git reset --hard`, `git clean -f`, `curl | sh`, recursive deletion outside the workspace, deploy/publish, cloud resource deletion, and destructive SQL/Redis statements invoked via database/cache CLIs) require one-shot user confirmation in **all** modes. Setting `permissions.confirmHighRisk: false` disables this confirmation **only** in **No-Limit**.
- **Sandbox unavailable fallback**: If the native sandbox backend cannot start before payload release, Virgo fails closed and—in interactive sessions—offers an explicit one-shot **No-Limit** confirmation for that single command (leaving the session mode unchanged).
- **Allow (`y`)** grants the approved `(read_paths | write_paths, canonical_path_prefix)` or `network_allowed` resource for the **current conversation and mode** (cleared on `/permissions revoke`, conversation switch, or mode change) without binding exact file content, `st_ino`, or `os.environ` hashes. Declining (`n`) blocks that resource for the remainder of the current prompt turn and asks again on a later user turn.
- **Always allow (`a`)** persists resource grants per `(project, mode, resource, target)` in `<VIRGO_HOME>/permission-grants.sqlite3` (`resource_grants` table; legacy `grants` / `resource-grants.sqlite3` state is dropped cleanly on upgrade). List saved grants with `/permissions grants` and remove entries with `/permissions revoke ID`. Project settings cannot install approvals.
- **Trusted extensions and MCP tools** run on **Host** outside the native Shell sandbox. Read-only MCP/external tools run directly; mutating external tools prompt in Read-Only and Workspace-Edit (and skip prompting in No-Limit). MCP tools support **Always allow** bound to `f"{tool_name}:{config_fingerprint}"` (SHA-256 fingerprint of the server's expanded `command`, `args`, and `env`)—any change to server config automatically invalidates prior persistent grants.
