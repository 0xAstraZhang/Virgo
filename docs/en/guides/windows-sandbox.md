# Native Windows command sandbox (Experimental)

> **Status: Experimental (feature-frozen).** On Windows, we recommend running Virgo inside **WSL2**, where it uses the Tier 1 Linux sandbox (`bubblewrap` + `libseccomp2`). See [WSL2 setup](wsl2.md).

Because of the limitations below, **native Windows defaults to No-Limit** (`execution.mode = "no-limit"`). On startup, Virgo displays a hint recommending WSL2, and selecting Read-Only or Workspace-Edit on native Windows displays an explicit experimental warning.

## Known limitations of the current LPAC backend

1. **Requires an Administrator account**: The current LPAC backend modifies device DACLs (`\Device\KsecDD`) and creates an Administrators-owned coordination mutex. On non-administrator Windows 11 desktop accounts, the OS returns `WinError 1307`. Virgo runs a fast precheck before launching and fails with clear guidance toward WSL2 or No-Limit rather than surfacing a raw `WinError 1307`.
2. **~14 seconds overhead per command**: Each command creates a fresh LPAC profile/SID and walks runtime files (CPython, `.venv`, PowerShell) to apply and revoke temporary ACLs. Performance budgets do not apply to native Windows.
3. **Does not enforce private-directory read denial**: Per-launch ACL walks over `~/.virgo` were removed because they conflicted with concurrent OAuth token refresh and settings writes (`WinError 5`). Do not rely on the experimental native Windows sandbox to hide `~/.virgo` from Shell commands.
4. **Does not support `$HOME` as a workspace**: Launching Read-Only or Workspace-Edit with `$HOME` (or a directory containing `~/.virgo`) as the workspace is rejected with guidance to use WSL2 or No-Limit.
5. **Non-blocking CI**: Non-sandbox tests run on a single blocking `windows-latest` runner; `tests/harness/test_windows_sandbox_e2e.py` runs on a daily scheduled workflow (`windows-sandbox.yml`) and does not block pull requests.

## Requirements (if opting into experimental sandbox modes)

- Native x64 Python 3.11+ and PowerShell 7 on local NTFS
- Elevated / Administrator account with rights to create AppContainer profiles, device ACLs, and temporary file ACLs
- Host firewall / AppContainer network services running (Workspace-Edit requests Internet/private-network capabilities; Read-Only omits them until granted)

## Future path back to Tier 1

The existing LPAC implementation is frozen—no new features or partial refactors are added to it. Returning native Windows to Tier 1 (and restoring default Workspace-Edit) requires a clean rewrite using a **standard AppContainer** (no LPAC opt-out / no `WRITE_RESTRICTED` / no device DACL modification so standard non-admin users can run) with a **session-level SID** and inheritable ACEs, verified on a non-administrator Windows 11 desktop account with cold start <= 5s and constant per-command overhead.
