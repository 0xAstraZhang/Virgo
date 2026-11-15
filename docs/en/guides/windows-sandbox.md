# Windows command sandbox

[Native command sandbox](sandbox.md) · [简体中文](../../zh-CN/guides/windows-sandbox.md)

## Validation status

The Windows backend is under development. At revision `351c33ee135a1653fef0363e69139b0f35694fdb`, both real Windows acceptance jobs reached the suspended-process checks, but PowerShell subsequently exited with `0xC0000142` (`STATUS_DLL_INIT_FAILED`). All 18 cases stopped in fixture setup. This is not a successful isolation result; the backend has no validated supported Windows environment at that revision. See [the exact native run](https://github.com/0xAstraZhang/Virgo/actions/runs/43736336047).

The following sections describe the implemented request and verification path. They do not turn the failed end-to-end run into a support claim.

## Permission model

Approval permits the Core tool to request execution. It cannot change an active sandbox into an ordinary process. Work mode and child restrictions determine the policy ceiling independently. The originating Runtime supplies the workspace; changing the selected conversation or a command's working directory does not select a new grant root.

| Mechanism | Implemented responsibility | Scope limit |
| --- | --- | --- |
| LPAC | A fresh AppContainer identity, with `ALL_APPLICATION_PACKAGES` access disabled; the process receives the `registryRead` capability | Windows still has ambient `ALL RESTRICTED APPLICATION PACKAGES` grants. This is object ACL isolation, not a filesystem namespace or an exact read-path allowlist. |
| Write-restricted token | `CreateRestrictedToken(DISABLE_MAX_PRIVILEGE \| WRITE_RESTRICTED)` with one independent, random restricting SID | The extra check restricts writes. It does not by itself confine reads or networking. No Everyone or logon SID is added as an alternative write grant. |
| Temporary file ACLs | Per-command read/execute or write ACEs for the workspace, selected runtimes and private profile; explicit mutation denials for read-only/protected objects | This requires ACL-edit authority on the affected objects and modifies their DACLs temporarily. It does not mount or copy a private filesystem. |
| AppContainer network policy | No network capability is supplied; startup requires the Base Filtering Engine and Windows Firewall services to be running | No allowlisted domains, proxy-based boundary or unrestricted active network mode is implemented. Real network acceptance must pass before this is claimed as validated. |
| Job Object | Atomic process assignment, descendant lifetime tracking and termination | A Job is not the file, network or process-access security boundary. |

`read-only` makes the workspace read-only to the command while retaining a private writable home/temp area. The broker also retains the exact output-log handle under the originating workspace's `.virgo/outputs`. Reading host data remains subject to Windows LPAC ACLs and explicitly granted runtime/workspace reads; it is not equivalent to the POSIX mount list.

## Host and path prerequisites

Discovery requires a native x64 process on the Windows 10 version family and the native APIs used by the backend. This is a preliminary API check, not a promise that every Windows 10/11 or Server release works. The observed runners used Windows Server 2022 Datacenter build 20348 and Server 2025 Datacenter build 26100, x64 CPython 3.13.17. Neither run completed the security scenarios. Desktop Windows, ARM64/emulation and standard-account installations have not been validated.

Active Windows execution accepts PowerShell 7 (`pwsh.exe`). Windows PowerShell 5.1, Git Bash and WSL are rejected by this backend. Selecting `disabled` retains the existing Windows shell executor and does not claim isolation.

The broker needs `READ_CONTROL` and `WRITE_DAC` on each object to which it applies a temporary grant, including the selected PowerShell installation and Python interpreter/virtual-environment roots. An installation under `Program Files` can therefore fail for an ordinary account. The implementation does not elevate the broker or install a privileged service. CI execution under an administrator account cannot establish ordinary-account compatibility.

Grant and output paths must use absolute, direct local NTFS drives with persistent ACL support. UNC paths, substituted/mapped drives, device paths, alternate data streams and invalid/reserved filename components are rejected. The native file layer translates a checked drive to its local volume device and uses `NtCreateFile` with no-reparse semantics; ACL changes use retained object handles rather than reopening pathnames.

Workspace junctions/reparse points and multiply linked workspace files reject setup, including for `read-only`. Interpreter roots must also be direct paths. Descendant reparse aliases in a read-only runtime installation are skipped without granting or traversing their targets. Existing root `.git`, `.virgo`, `AGENTS.md`, `CLAUDE.md` and interpreter paths receive mutation denials. Absent metadata names are not reserved by this Windows implementation.

Only the workspace, selected PowerShell/Python installations and private profile receive explicit per-command filesystem grants. SDKs, other interpreters and toolchains outside those paths may be inaccessible unless Windows already grants the LPAC identity access. There is no configuration for additional runtime roots in this implementation.

## Launch and cleanup

Foreground Shell, background commands and child Agents use the existing `SandboxPolicy.spawn` and `WindowsProcess` path. The backend creates the actual command suspended, places it in its Job as part of creation, and checks that process's token before resuming it once. Checks cover the package SID, restricting SID, exact capability set and native LPAC access semantics. The latter uses `AccessCheck` against AAP/ARAP descriptors; the unsupported class-46 token query observed on both runners is not used.

Only explicit stdin/stdout handles are inherited. A case-insensitive environment allowlist retains executable search and locale settings, supplies fixed Windows runtime variables and points home/temp variables at the private profile. It does not forward provider keys, proxy credentials or arbitrary inherited variables. These measures do not hide credentials intentionally placed inside the granted workspace.

Normal root exit, timeout and cancellation drain the same Job, including descendants that outlive the root. Cleanup waits for the root process to signal and for kernel Job accounting to report zero active processes before releasing ACL/profile resources. Concurrent commands use different identities; ACL cleanup removes only the current command's ACEs from the current DACL instead of restoring an old whole-ACL snapshot. Startup/cleanup failure is reported and cannot cause an ordinary-process fallback or automatic replay.

Abrupt broker death closes its Job handle and requests kernel termination through `KILL_ON_JOB_CLOSE`, but Python ACL/profile cleanup does not run after a broker crash. Temporary ACEs and an AppContainer profile can remain. There is no recovery journal or persistent cleanup service. The identities are unique per command and are not reused. Resource quotas and protection against a privileged administrator changing the host's security configuration are not provided.

Core `write`/`edit`, trusted extensions, hooks, remote tools and the broker run outside LPAC. Core file mutations retain their separate application checks and fresh-file staging. The Windows Core path does not provide the POSIX descriptor-relative traversal guarantee; this backend does not claim to OS-sandbox those Python operations.

## Distribution and maintenance

No third-party dependency, driver, compiled helper, service, account provisioning or firewall-rule installer is added. The ordinary Python package contains `windows_sandbox.py`, `windows_sandbox_acl.py` and `windows_sandbox_files.py`; `ctypes` calls the installed Windows system DLLs. `windows_process.py` continues to own process lifetime.

This avoids a separate native binary distribution, but the project owns the Windows ABI declarations, object/handle lifetimes, per-command ACL traversal and concurrent cleanup logic. The broker walks the granted trees for each launch. Changes to this boundary require the real Windows acceptance workflow, not only import/lint checks or a mocked denial.

## Verification and references

The 18 scenarios in `tests/harness/test_windows_sandbox_e2e.py` were committed before the production backend. They cover positive workspace access, outside/ambient ACL access, read-only and metadata limits, reparse/hardlink paths, credential/handle inheritance, descendant tokens and process access, live TCP/UDP access, timeout/cancellation, background and child-runtime origin, non-replay and resource cleanup. The dedicated workflow runs the exact PR head on `windows-2022` and `windows-2025` and rejects any failure, error or skip. A correction to its LPAC verifier replaced an unsupported token query with native `AccessCheck`; the original 18 scenarios and assertions were retained.

References used for the implementation:

- [Microsoft: launch an AppContainer / LPAC](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)
- [Microsoft: restricted tokens](https://learn.microsoft.com/en-us/windows/win32/secauthz/restricted-tokens) and [CreateRestrictedToken](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-createrestrictedtoken)
- [Microsoft: AccessCheck](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-accesscheck) and [CreateProcessAsUserW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw)
- [Microsoft: Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [Codex Windows implementation at the inspected revision](https://github.com/openai/codex/tree/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/windows-sandbox-rs): its legacy restricted-token and elevated-account/firewall paths use a different permission model. Virgo does not claim that proxy environment variables or Job Objects provide network isolation.
