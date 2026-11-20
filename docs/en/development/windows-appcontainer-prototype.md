# Stage 1 Prototype Validation: Native Windows Standard AppContainer

> **Status: Stage 2 Implemented (`src/virgo/sandbox/`)** — The Stage 2 production rewrite (`windows-appcontainer` backend) has replaced the frozen LPAC backend in `src/virgo/sandbox/`, restored Tier 1 status and default **Workspace-Edit** on native Windows, and retired the temporary `experiments/windows_appcontainer/` prototype directory. The historical Stage 1 empirical validation report below is preserved intact for reference. See [Native Windows command sandbox](../guides/windows-sandbox.md).

> **Stage 1 Verdict: Conditional Go** — The core **Standard AppContainer + session-level single SID + OS-propagated inheritable ACE** architecture passed every performance, isolation, ecosystem-compatibility, crash-recovery, and network-isolation threshold **without Administrator rights**, device DACL mutations, or Windows Firewall edits. However, **four concrete Stage 2 conditions** identified empirically on Windows 11 26H2 (ancestor-directory `C:\Users` ACL friction affecting `pwsh`/`pytest`/`git`, non-AAP/non-user-owned runtime installs, localhost/loopback blocking without Admin, and verification on a pure non-Administrators Standard User account) must be addressed or explicitly designed around before promoting native Windows back to Tier 1 and restoring default **Workspace-Edit**.

> Scope: Stage 1 independent prototype validation (`experiments/windows_appcontainer/`). No production files under `src/virgo/`, `tests/`, `.github/`, or `pyproject.toml` were modified; native Windows remains on the frozen experimental LPAC backend with default **No-Limit** (`execution.mode = "no-limit"`). See [Experimental native Windows sandbox](../guides/windows-sandbox.md) and [Execution boundaries](../guides/sandbox.md).

---

## 1. Executive Summary & Go / No-Go Verdict

Virgo's frozen native Windows LPAC backend requires an Administrator account (`WinError 1307` on non-admin desktop accounts due to `\Device\KsecDD` DACL edits and an `O:BAG:BA` mutex), adds ~14 seconds per command from per-launch Python ACL walks, and cannot protect `~/.virgo` without causing `WinError 5` conflicts against host OAuth/settings writes.

This Stage 1 prototype (`experiments/windows_appcontainer/`) validates a clean-slate replacement using **Standard AppContainer** (`CreateProcessW` + `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`, retaining `ALL APPLICATION PACKAGES` / `S-1-15-2-1`, with **no** LPAC opt-out `0x2000F`, **no** `CreateRestrictedToken`/`WRITE_RESTRICTED`, **no** `\Device\KsecDD` or `\Device\Null` edits, **no** `O:BAG:BA` mutex, **no** WindowStation/Desktop DACL mutations, and **no** Windows Firewall rule writes) paired with a **session-level single AppContainer SID** and single-call OS-propagated `SetNamedSecurityInfoW` inheritable ACEs.

### Empirical Scorecard vs. Stage 1 Thresholds

| Criterion | Target / Budget | Measured Empirical Result (Windows 11 26H2, Build `26300.9974`) | Status |
| --- | --- | --- | --- |
| **Unelevated execution** | Runs without Admin / UAC elevation; zero device/mutex/firewall edits | `IsUserAnAdmin() == False`, `TokenElevation == 0`, Medium IL (`S-1-16-8192`); existing LPAC precheck rejected (`windows_admin_required`); Standard AppContainer verified (`TokenIsAppContainer == 1`, `aap_granted == True`, `restricted_sids == []`). **Caveat:** account is a UAC-filtered split token, not a pure Standard User. | **Conditional Pass** |
| **Crypto / RNG / TLS / NUL / Pipes / Job kill** | 100% pass without `\Device\KsecDD` or `\Device\Null` DACL edits | `os.urandom`, `secrets`, `BCryptGenRandom` (`NTSTATUS=0`), `ssl.create_default_context()` (61 X.509 / 59 CA certs), `hashlib`, `NUL`/`DEVNULL`, 256 KB stdout + 128 KB stderr simultaneous pipes (`38.537 ms`, exact match), Job Object grandchild kill (`active=0`, both PIDs dead). | **Pass** |
| **Toolchain usability** | CPython, `.venv`, `pwsh`, `powershell.exe`, `git`, `node`, `uv`, `pytest` usable inside AppContainer | `cmd.exe`, user-owned CPython `3.13.18` + `.venv`, `uv 0.13.9` (`venv`/`run`), monolithic `node v24.13.1`, and user-owned `git` inside an existing repo all succeeded (`rc=0`). **Blockers surfaced:** (1) `C:\Users` lacking `S-1-15-2-1` broke `pwsh`/`powershell` default `Get-Location`, `pytest` collection (`rc=2`), and `git init`/outside-repo `git`; (2) non-AAP/non-user-owned runtimes (`E:\Users\zhang\miniconda3`, junctioned `E:\Program Files\Git`, `C:\Program Files\nodejs` `node_modules`) fail `WRITE_DAC` (`WinError 5`) unelevated. | **Conditional Pass** |
| **Session cold start** | `<= 5,000 ms` at realistic scale (1,000–10,000 files + `.venv`) | **10 files (31 total):** `90.002 ms` (ws+venv) / `315.975 ms` (incl. `3,659`-entry user Python). **1,000 files (1,021 total):** `128.716 ms` / `368.742 ms`. **10,000 files (10,021 total):** `625.865 ms` / `835.501 ms` (**6×–8× under budget**). | **Pass** |
| **Per-command launch overhead** | `<= 1,000 ms`, O(1) across 10 / 1,000 / 10,000 files | `cmd.exe /c exit 0` (30 cmds/scale): `16.884 ms` / `17.311 ms` / `17.229 ms` median (`CreateProcessW` alone: `1.260`–`1.610 ms`). `.venv python -c "print(1)"`: `40.791 ms` / `41.118 ms` / `41.696 ms` median (**~24× under budget, flat O(1)**). | **Pass** |
| **Teardown & residual ACEs** | Clean single-call revocation, zero residual ACEs | 10,000-file teardown: `601.461 ms` (ws+venv) / `790.260 ms` (all); **0 residual ACEs** across all 9 benchmark sessions (including files created during the session). `SE_DACL_PROTECTED` subdir edge case characterized. | **Pass** |
| **Private directories & outside writes** | Block reads of `~/.virgo`, `~/.ssh`, user profile & all outside-workspace writes | 11/11 private reads and outside-workspace writes blocked (`PermissionError` / `WinError 5` / `Errno 13`) by default NTFS DACLs without touching `~/.virgo`. System `hosts` readable via AAP as expected. | **Pass** |
| **Control metadata protection (VER-01)** | Block writes/renames/deletes on `.git`, `.virgo`, `AGENTS.md`, `CLAUDE.md` while keeping reads + `git status` | **Kernel discovery:** `ACCESS_DENIED_ACE` targeting `S-1-15-2-*` is ignored by Windows (`mode="deny_ace"` failed completely). Using `mode="protected_readonly"` (`PROTECTED_DACL_SECURITY_INFORMATION` + explicit `RX`) with `allow_delete_child=False` blocked **7/7 writes, 4/4 renames, 6/6 deletes, and `git add` (`rc=128`)** while preserving reads (4/4) and plain `git status`/`diff`/`log` (`rc=0`). Non-existent protected names remain creatable (honest NTFS limitation). | **Pass (with known NTFS limit)** |
| **Host ecosystem compatibility** | Host `git` + atomic saves (`os.replace`) succeed with zero `WinError 5` while sandbox ACEs are active | **6/6 host operations passed** (`os.replace` on `app.py`, `AGENTS.md`, and user settings; host `git status`/`add`/`commit`), zero `WinError 5`. | **Pass** |
| **Crash recovery (`TerminateProcess`)** | Deterministic cleanup of lingering ACEs & profile; idempotent | After hard `TerminateProcess` (51 workspace + 3,659 `user_python` lingering ACEs + profile), `recover_stale_sessions()` cleaned 100% in **`254.985 ms`** (`0` residual ACEs, profile lookup `HRESULT 0x80070002`, state removed, 2nd call returned `[]`). | **Pass** |
| **Network isolation** | Read-Only blocks all outbound; Workspace-Edit allows TCP/DNS/HTTPS/`git`/`pip`/`uv`; zero admin firewall writes | Read-Only (`caps=()`): **100% blocked** (TCP `WinError 10013`, DNS `Errno 11001`, UDP/loopback `TimeoutError`) even with `FlClash` TUN active. Workspace-Edit (`internetClient` + `privateNetworkClientServer`): TCP, DNS, UDP, HTTPS (200), `git ls-remote`, `pip`, `uv` all succeeded. **Caveat:** loopback (`127.0.0.1` / `[::1]`) remains blocked (`TimeoutError`) even with `internetClientServer`. | **Pass (with loopback caveat)** |

### Verdict: **Conditional Go**

Proceed to the Stage 2 production rewrite PR **subject to four explicit design/verification gates** before restoring Tier 1 status and switching native Windows' default from **No-Limit** to **Workspace-Edit**:

1. **Resolve ancestor-directory ACL friction under `C:\Users\<user>`** (prototype a `subst` / `DefineDosDeviceW` virtual workspace drive or equivalent low-complexity fix so `pwsh`, `powershell.exe`, `pytest`, and `git` work cleanly inside workspaces under `%USERPROFILE%`).
2. **Handle non-AAP / non-user-owned runtime roots cleanly (R6)** via a pre-launch accessibility probe (`S-1-15-2-1` presence or `WRITE_DAC` check) with actionable guidance toward user-owned or AAP-inheriting runtime installs rather than opaque `0xC0000135` / exit `103` failures.
3. **Document localhost/loopback blocking honestly (R7/R8)** and rely on Virgo's explicit one-shot **No-Limit** fallback (R6) for workflows requiring local loopback dev servers, databases, or local HTTP proxies (`127.0.0.1`).
4. **Verify on a pure non-Administrators Standard User account** (`TokenElevationType == 1`, `*Users` only, not a member of `BUILTIN\Administrators`) in addition to UAC-filtered desktop accounts.

---

## 2. Test Environment

All measurements were collected in a single end-to-end execution of `experiments/windows_appcontainer/run_all.py` (`2026-11-20T13:51:51Z` to `2026-11-20T13:57:58Z`, `366.505 s` total, exit code `0`, `post_run_cleanliness_audit.clean == true`).

### Host OS, Filesystem, Services & Network

| Property | Observed Value |
| --- | --- |
| **OS / Platform** | Windows 11 Pro 26H2 (`platform`: `Windows-11-10.0.26300-SP0`, `EditionID`: `Professional`, registry `ProductName`: `Windows 10 Pro` [standard Win11 registry compatibility value]) |
| **Build / UBR / Lab** | `CurrentBuildNumber: 26300`, `UBR: 9974` (`10.0.26300.9974`), `BuildLabEx: 26100.1.amd64fre.ge_release.240331-1435` |
| **Architecture** | `AMD64` (64-bit Python) |
| **Volumes & Filesystems** | `C:\` (`OS`, **NTFS**, `fs_flags=0x3e72eff`, persistent ACLs `true`); `E:\` (`Date`, **NTFS**, `fs_flags=0x3e72eff`, persistent ACLs `true`) |
| **WindowStation / Desktop** | `WinSta0\exebox-ZRVRYZUHVPUQP3ZNIBQTIEAPS6` (harness custom desktop; `WinSta0\Default` openable with `READ_CONTROL`) |
| **Firewall / Filtering Services** | `BFE` (`RUNNING`, PID `5088`), `MpsSvc` (`RUNNING`, PID `5088`) |
| **Proxy Configuration** | Env vars (`HTTP_PROXY`/`HTTPS_PROXY`/etc.): `{}` (empty); WinHTTP: `Direct access (no proxy server)`; WinINET: `ProxyEnable=0` (inactive `ProxyServer=127.0.0.1:7890`) |
| **Active Network / TUN State** | `WLAN` (`Intel Wi-Fi 6 AX201 160MHz`, Up, default route `0.0.0.0/0 -> 192.168.0.1`); `vEthernet (WSL (Hyper-V firewall))` (Up); **`FlClash` (`Meta Tunnel`, Up, `100 Gbps`)** with processes `FlClash`, `FlClashCore`, `FlClashHelperService` and 79 routes (`next_hop` `0.0.0.0` and `198.18.0.2`). `C:\ProgramData\clash-verge-service` exists on disk, but neither `clash_verge_service` nor `clash-verge-service` exists (`WinError 1060`). |

### Account Class & Privilege Posture (Honest Split-Token Distinction)

| Check | Observed Value |
| --- | --- |
| **Account / SID** | `n1ght\zhang` (`S-1-5-21-2422618206-1863484983-4098800063-1001`) |
| **`shell32.IsUserAnAdmin()`** | `False` |
| **`TokenElevation` / `TokenElevationType`** | `0` / `3` (`TokenElevationTypeLimited` — UAC-filtered unelevated split token) |
| **Mandatory Integrity Level** | `Mandatory Label\Medium Mandatory Level` (`S-1-16-8192`) |
| **Key `whoami /groups` entries** | `BUILTIN\Administrators` (`S-1-5-32-544`) = **`Group used for deny only`**; `S-1-5-114` (Local account & member of Administrators) = **`Group used for deny only`**; `BUILTIN\Users` (`S-1-5-32-545`), `Authenticated Users` (`S-1-5-11`), `Everyone` (`S-1-1-0`), `INTERACTIVE` (`S-1-5-4`), `LOCAL` (`S-1-2-0`), `S-1-5-113`, `S-1-5-64-36` enabled |
| **`whoami /priv`** | `SeChangeNotifyPrivilege` (**Enabled**); `SeShutdownPrivilege`, `SeUndockPrivilege`, `SeIncreaseWorkingSetPrivilege`, `SeTimeZonePrivilege` (Disabled). Zero admin privileges (`SeSecurityPrivilege`, `SeTakeOwnershipPrivilege`, `SeRestorePrivilege`, `SeDebugPrivilege` absent). |
| **`net user zhang`** | Local Group Memberships: `*Administrators`, `*Users` |
| **Existing LPAC Precheck** | `precheck_experimental_sandbox()` rejected with `ExecutionUnavailable` (`reason="windows_admin_required"`, `dimension="windows_experimental_sandbox"`). |

> **Honest Account-Class Note:** The test process ran completely unelevated at Medium IL with `BUILTIN\Administrators` marked deny-only (`IsUserAnAdmin() == False`), and Windows enforced standard non-admin boundaries everywhere (confirmed empirically by `CreateFileW(WRITE_DAC)` returning `WinError 5` on every `Administrators`- or `SYSTEM`-owned directory). However, because `n1ght\zhang` belongs to local `Administrators` (`TokenElevationType == 3` UAC split token) rather than being a pure `*Users`-only Standard User (`TokenElevationType == 1`), Stage 2 must include a confirmation pass on a **pure non-Administrators Standard User account** before Tier 1 promotion.

### Tested Toolchains & Fixture Sizes

| Tool | Version | Path | Notes |
| --- | --- | --- | --- |
| **Host Python (`PATH`)** | WindowsApps stub | `C:\Users\zhang\AppData\Local\Microsoft\WindowsApps\python.exe` | Not used for execution |
| **Host CPython (Miniconda)** | `Python 3.13.11` (`pip 25.3`) | `E:\Users\zhang\miniconda3\python.exe` (`C:\Users\zhang\miniconda3` is a junction to `E:\...`) | Secondary drive, `AAP=False`, `Owner=Administrators`, `WRITE_DAC -> WinError 5` |
| **User-owned CPython** | `Python 3.13.18` | `%USERPROFILE%\.virgo_ac_proto_fixture_*\user_python\cpython-3.13-windows-x86_64-none\python.exe` | Bootstrapped via `uv python install 3.13`: **`3,659` unique filesystem entries** touched by `SetNamedSecurityInfoW` / `scan_residual_aces()` (note: the recorded run's `os.walk()` helper reported `6,778` files / `533` dirs / `7,311` entries because `uv` creates a minor-version NTFS directory junction `cpython-3.13-windows-x86_64-none -> cpython-3.13.18-windows-x86_64-none` that `os.walk()` traversed twice, whereas NTFS inheritance and `scan_residual_aces()` prune reparse points) |
| **Fixture `.venv`** | `Python 3.13.18`, `pytest 9.2.0`, `uv 0.13.9`, `pip` | `%USERPROFILE%\.virgo_ac_proto_fixture_*\.venv\Scripts\python.exe` | `1,078` files, `116` dirs, `1,194` entries |
| **PowerShell 7 (`pwsh`)** | `7.6.10` | `C:\Program Files\PowerShell\7\pwsh.exe` | `AAP=True` |
| **Windows PowerShell 5.1** | `5.1.26100.9818` | `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe` | `AAP=True` |
| **Git for Windows** | `2.53.0.windows.1` | `C:\Program Files\Git\cmd\git.exe` (junction -> `E:\Program Files\Git`) + user-owned copy in fixture | Junction target on `E:\` has `AAP=False`, `Owner=Administrators`, `WRITE_DAC -> WinError 5` |
| **Node.js** | `v24.13.1` | `C:\Program Files\nodejs\node.exe` + user-owned copy in fixture | `C:\Program Files\nodejs` has `Protected=True`, `AAP=False`, `Owner=SYSTEM`, `WRITE_DAC -> WinError 5` |
| **`uv` / `pytest`** | `uv 0.13.9` / `pytest 9.2.0` | Installed in fixture `.venv\Scripts\` (neither globally installed on host) | User-owned, `WRITE_DAC=True` |

Scale benchmark synthetic workspaces (populated with `.git`, `.virgo/config.json`, `AGENTS.md`, `CLAUDE.md` plus nested Python source modules):
- **`small_10`**: 10 requested source files (`31` total files, `15` dirs, `46` entries)
- **`medium_1000`**: 1,000 requested source files (`1,021` total files, `51` dirs, `1,072` entries)
- **`large_10000`**: 10,000 requested source files (`10,021` total files, `211` dirs, `10,232` entries)

### Default Host DACL Audit (`ALL APPLICATION PACKAGES` / `S-1-15-2-1`)

| Path | Owner SID | `Protected` | `S-1-15-2-1` (AAP) | `S-1-15-2-2` (LPAC) | Unelevated `WRITE_DAC` | Notable Existing ACEs |
| --- | --- | :---: | :---: | :---: | :---: | --- |
| `C:\Windows\System32` | TrustedInstaller (`S-1-5-80-...`) | `True` | **Yes** (`RX`) | Yes (`RX`) | — | TrustedInstaller, SYSTEM, Admins, Users (`RX`), AAP (`RX`), LPAC (`RX`) |
| `C:\Program Files\PowerShell\7` | SYSTEM (`S-1-5-18`) | `False` | **Yes** (`I, RX`) | Yes (`I, RX`) | — | Inherits standard `Program Files` AAP + LPAC (`RX`) |
| `C:\Program Files\Git` (junction) | Admins (`S-1-5-32-544`) | `False` | **Yes** (on junction) | Yes | — | Junction resolves to `E:\Program Files\Git` |
| `E:\Program Files\Git` (target) | Admins (`S-1-5-32-544`) | `False` | **No** | No | **No** (`WinError 5`) | Admins (`F`), SYSTEM (`F`), Authenticated Users (`M`), Users (`RX`) |
| `C:\Program Files\nodejs` | SYSTEM (`S-1-5-18`) | `True` | **No** | No | **No** (`WinError 5`) | MSI installer broke inheritance: Users (`RX`), Auth Users (`RX`), Admins (`F`), SYSTEM (`F`) |
| `E:\Users\zhang\miniconda3` | Admins (`S-1-5-32-544`) | `False` | **No** | No | **No** (`WinError 5`) | Admins (`F`), SYSTEM (`F`), Authenticated Users (`M`), Users (`RX`) |
| `C:\` | TrustedInstaller | `True` | **No** | No | — | Admins (`F`), SYSTEM (`F`), Users (`RX`), Auth Users (`AD`/`M`), 1 capability SID (`S,RD,X,RA`) |
| `C:\Users` | SYSTEM (`S-1-5-18`) | `True` | **No** | No | — | SYSTEM (`F`), Admins (`F`), Users (`RX`), Everyone (`RX`), 1 capability SID (`S,RD,X`) |
| `C:\Users\zhang` | SYSTEM (`S-1-5-18`) | `True` | **No** | No | — | SYSTEM (`F`), Admins (`F`), `N1GHT\zhang` (`F`), 1 capability SID (`S,X`) |
| `C:\Users\zhang\.ssh` | `n1ght\zhang` | `False` | **No** | No | — | Inherits SYSTEM (`F`), Admins (`F`), `N1GHT\zhang` (`F`) |
| `C:\Users\zhang\.virgo` | *(does not exist on host)* | — | — | — | — | Tested via fresh `%USERPROFILE%\.virgo_ac_private_canary_*` inheriting `C:\Users\zhang` DACL |
| `C:\Users\zhang\Desktop` / `Documents` | `n1ght\zhang` | `False` | **No** | No | — | Inherits `C:\Users\zhang` DACL (plus pre-existing `CodexSandboxUsers:(RX)` unrelated to AppContainer) |
| Repo root & fixture user roots | `n1ght\zhang` | `False` | **No** | No | **Yes** (`0`) | Inherits `N1GHT\zhang:(F)`, enabling unelevated single-call `SetNamedSecurityInfoW` |

Two host DACL observations proved decisive in the experiments:
1. **`C:\Users` and `C:\Users\zhang` do NOT grant `S-1-15-2-1`** (they are `SYSTEM`-owned with `Protected=True`). Standard AppContainer processes can open granted workspaces underneath via `SeChangeNotifyPrivilege` (bypass traverse checking), **but** tools that explicitly `stat()` or normalize ancestor directories (`C:\Users`, `workspace.parent`) receive `WinError 5` / `Access Denied`.
2. **Non-`C:\Program Files` or custom-MSI toolchains frequently lack `S-1-15-2-1` and user `WRITE_DAC`**: secondary-drive installs (`E:\Users\zhang\miniconda3`, junctioned `E:\Program Files\Git`) and MSI installers that break inheritance (`C:\Program Files\nodejs`) cannot be modified unelevated (`WRITE_DAC` -> `WinError 5`).

---

## 3. Verification Area 1 — Non-Admin Execution & Toolchain Usability

### Standard AppContainer Token & Launch Verification

Every process launch in `experiments/windows_appcontainer/appcontainer.py` (`run_in_appcontainer`) uses `CreateProcessW` directly (not `CreateProcessAsUserW`) with:
- Creation flags: `CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED`
- Exactly 3 startup attributes: `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` (`0x20002`), `PROC_THREAD_ATTRIBUTE_JOB_LIST` (`0x2000D`, `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), and `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` (`0x20009`)
- **Zero** LPAC opt-out (`0x2000F`), **zero** `CreateRestrictedToken` / `WRITE_RESTRICTED`, **zero** `\Device\KsecDD` or `\Device\Null` DACL edits, **zero** `O:BAG:BA` mutex, **zero** WindowStation/Desktop DACL mutations.

Before calling `ResumeThread`, `verify_suspended_process()` inspected every child token and passed on 100% of launches:
- `in_job == True` (`IsProcessInJob`)
- `is_appcontainer == True` (`TokenIsAppContainer == 1`)
- `package_sid` matches the session AppContainer SID (`TokenAppContainerSid`)
- `capability_sids` matches the requested capabilities exactly (`TokenCapabilities`)
- `restricted_sids == []` (`TokenRestrictedSids` empty — confirming no `WRITE_RESTRICTED` token)
- `aap_granted == True` and `everyone_only_denied == True` (`AccessCheck` against SDDL with `S-1-15-2-1` granted and Everyone-only SDDL denied — proving genuine Standard AppContainer semantics, unlike LPAC).

### Toolchain Usability Matrix

| Tool & Variant | Command Tested | Runtime ACE Needed? | Exit Code | Elapsed (ms) | Outcome & Observed Quirks |
| --- | --- | --- | ---: | ---: | --- |
| **`cmd.exe`** | `cmd.exe /c "cd && echo cmd_ok>cmd_out.txt && type cmd_out.txt"` | No (`System32` has AAP) | `0` | `28.077` | **Pass.** Correct workspace CWD and read/write. |
| **Host CPython (Miniconda, no ACE)** | `E:\Users\zhang\miniconda3\python.exe -c "import sys; print(sys.version)"` | Needed, but `WRITE_DAC -> WinError 5` | `3221225781` (`0xC0000135`) | `20.755` | **Fail (`STATUS_DLL_NOT_FOUND`).** `E:\Users\zhang\miniconda3` lacks AAP and is owned by `Administrators`; child cannot load `python313.dll`. |
| **`.venv` backed by Miniconda** | `host_backed_venv\Scripts\python.exe -c "import sys; ..."` | Base Python lacks AAP (`WRITE_DAC -> WinError 5`) | `103` | `17.493` | **Fail.** Venv launcher printed `did not find executable at 'E:\Users\zhang\miniconda3\python.exe': Access denied`. |
| **User-owned CPython (no ACE)** | `user_python\...\python.exe -c "import sys; print(sys.version)"` | Yes (omitted here) | `3221225781` (`0xC0000135`) | `24.469` | **Fail as expected** (`STATUS_DLL_NOT_FOUND` without runtime ACE — proves AppContainer denies user-profile dirs by default). |
| **User-owned CPython (with RO ACE)** | `user_python\...\python.exe -c "<write+read workspace file, print sys.prefix>"` | **Yes** (single RO grant, `230.677 ms` once/session) | `0` | `113.218` | **Pass.** Python `3.13.18`, workspace read/write verified (`hello_user_py`). |
| **`.venv` backed by user CPython** | `.venv\Scripts\python.exe -c "<import pytest, write/read workspace>"` | **Yes** (`user_python` + `.venv` RO, `73.716 ms`) | `0` | `578.797` | **Pass.** `sys.prefix == .venv`, `sys.base_prefix == user_python`, imported `pytest 9.2.0`, wrote/read `venv_out.txt`. |
| **`pwsh 7.6.10` (`desktop=None`)** | `pwsh.exe -NoProfile -Command "Write-Output 'ok'"` | No (`Program Files\PowerShell\7` has AAP) | `3221225794` (`0xC0000142`) | `22.712` | **Fail (`STATUS_DLL_INIT_FAILED`)** under harness custom desktop `WinSta0\exebox-...` lacking Low-IL SACL. |
| **`pwsh 7.6.10` (`desktop="auto"`, default & fullpath)** | `pwsh.exe -NoProfile -Command "<check LOC, ECWD, Set-Location, pipeline, fullpath write>"` | No (`WinSta0\Default`, 0 DACL edits) | `0` | `473.872` | **Partial / Quirk.** Launches, pipeline `1..5 \| Measure-Object -Sum` (`15`) and full-path read/write work, **but** `(Get-Location).Path` silently falls back to `C:\` (`Set-Location` error: `Access to the path 'C:\Users' is denied.`). `[Environment]::CurrentDirectory` stays on workspace. |
| **`pwsh 7.6.10` (`New-PSDrive` workaround)** | `New-PSDrive -Name WS -PSProvider FileSystem -Root ([Environment]::CurrentDirectory); Set-Location WS:\; Set-Content 'pwsh_rel.txt' ...` | No | `0` | `414.308` | **Pass.** Relative file read/write works cleanly (`LOC=WS:\`, `SUM=15`, `FILE=pwsh_7.6.10_15`). |
| **`powershell.exe 5.1` (`desktop="auto"`, default & fullpath)** | `powershell.exe -NoProfile -Command "<check LOC, ECWD, Set-Location, fullpath write>"` | No (`System32` has AAP) | `0` | `1481.003` | **Partial / Same Quirk.** Version `5.1.26100.9818`, full-path read/write works, **but** `LOC=C:\` and `Set-Location` returns Access Denied on `C:\Users`. |
| **`powershell.exe 5.1` (`New-PSDrive` workaround)** | `New-PSDrive -Name WS ...; Set-Location WS:\; Set-Content 'winps_rel.txt' ...` | No | `0` | `947.879` | **Pass.** Relative read/write works cleanly (`LOC=WS:\`, `FILE=winps_5.1.26100.9818`). |
| **Stock `git.exe` (junctioned to `E:\`)** | `C:\Program Files\Git\cmd\git.exe --version` | Junction target `E:\Program Files\Git` lacks AAP (`WRITE_DAC -> WinError 5`) | `1` | `24.374` | **Fail.** Wrapper printed `error launching git:` because `E:\Program Files\Git\mingw64\bin` has `AAP=False`. |
| **User-owned `git.exe` (with RO ACE)** | `git --version`, `status --short`, `add`, `commit`, `diff`, `log` inside existing repo | **Yes** (`user_git` RO, `25.433 ms`) | `0` (all 6) | `33.767`–`652.324` | **Pass inside existing repo.** `--version` (`2.53.0.windows.1`, `651.110 ms`), `status` (`34.735 ms`), `add` (`40.859 ms`), `commit` (`652.324 ms`), `diff` (`47.322 ms`), `log` (`33.767 ms`) all succeeded. |
| **User-owned `git init` under `C:\Users\...`** | `user_git\cmd\git.exe init -b main` in fresh dir under `C:\Users\zhang\...` | Yes (`user_git` RO) | `1` | `140.607` | **Fail (ancestor ACL quirk).** `error: cannot lock ref 'HEAD': unable to create directory for C:/Users/zhang/.../.git/HEAD` (Git's `safe_create_leading_directories` hits `EACCES` probing `C:/Users`). |
| **Stock `node.exe` (`Program Files`, no ACE)** | `C:\Program Files\nodejs\node.exe -e "<crypto.randomBytes, workspace fs, read npm/package.json>"` | No (monolithic binary) | `0` | `49.864` | **Partial / Quirk.** `v24.13.1`, `crypto.randomBytes(16)` and workspace `fs` read/write succeeded! **However**, opening `C:\Program Files\nodejs\node_modules\npm\package.json` failed with `EPERM` because `C:\Program Files\nodejs` is `Protected=True, AAP=False`. |
| **User-owned `node.exe` copy (with RO ACE)** | `user_node\node.exe -e "<crypto.randomBytes, workspace fs>"` | Yes (`0.351 ms`) | `0` | `1654.026` | **Pass.** `v24.13.1`, crypto + workspace fs OK (`create_ms=1429.002 ms` on first Defender scan of freshly copied exe). |
| **`uv 0.13.9` (`.venv` with RO ACE)** | `uv --version`, `uv venv uv_sub_venv --python ... --no-python-downloads`, `uv run --no-project ...` | Yes (`.venv` + `user_python` RO) | `0` (all 3) | `2578.735` / `290.396` / `340.665` | **Pass.** `--version` (`uv 0.13.9`, initial `create_ms=2501.690 ms` first-run AV scan), `uv venv` (`290.396 ms`), `uv run` (`340.665 ms`, `uv_run_ok 3.13.18`). |
| **`pytest 9.2.0` (4 variants under `C:\Users\...`)** | `python -m pytest -q mini_suite` (plain; + `pyproject.toml`; + `--confcutdir=<ws>`; + `-p no:cacheprovider`) | Yes (`.venv` + `user_python` RO) | `2` (all 4) | `751.226`–`764.214` | **Fail (ancestor ACL blocker).** Collection crashed with `PermissionError: [WinError 5]` in `pathlib` `os.stat()` on `workspace.parent` (`C:\Users\zhang\.virgo_ac_proto_exp1_*`) across all 4 flag variants. With cacheprovider enabled, also emitted `PytestCacheWarning: [WinError 5]` creating `pytest-cache-files-*`. |

### Key Toolchain Implementation Discoveries

1. **Custom headless desktop (`0xC0000142`)**: When Virgo or a test harness runs under a non-Default custom desktop (here `WinSta0\exebox-ZRVRYZUHVPUQP3ZNIBQTIEAPS6`) that lacks a Low-IL SACL (`S:(ML;;NW;;;LW)`), GUI/console processes loading `user32.dll` (`pwsh`, `powershell.exe`, `git`) crash with `0xC0000142` (`STATUS_DLL_INIT_FAILED`) if `lpDesktop` is `None`. Passing `lpDesktop = "WinSta0\\Default"` when `Default` is openable (`desktop="auto"`) resolves this completely with **zero** WindowStation/Desktop DACL mutations.
2. **Windows `LOCALAPPDATA` auto-rewriting**: Inside an AppContainer child, Windows automatically appends `\Packages\<ProfileName>\AC` to whatever `LOCALAPPDATA` value the parent passes. Passing the host's base `LOCALAPPDATA` (`build_appcontainer_env`) lets Windows map child `LOCALAPPDATA` cleanly to `<AC>` while `USERPROFILE`/`HOME` -> `<AC>\Home`, `APPDATA` -> `<AC>\Home\AppData\Roaming`, and `TEMP`/`TMP`/`TMPDIR` -> `<AC>\Temp`.

### Crypto, RNG, TLS, `NUL`, Pipes & Job Object Verification (No Device DACL Edits)

Executed inside Standard AppContainer with **zero** mutations to `\Device\KsecDD`, `\Device\Null`, or any global mutex:

| Primitive | Verified Observation | Exit / Status |
| --- | --- | --- |
| `os.urandom(32)` | Returned `32` random bytes | **Pass** (`rc=0`, combined script `242.054 ms`) |
| `secrets.token_hex(32)` | Returned `64` hex chars | **Pass** |
| Direct `bcrypt.dll!BCryptGenRandom` (`BCRYPT_USE_SYSTEM_PREFERRED_RNG`) | `NTSTATUS = 0`, output buffer nonzero | **Pass** |
| `ssl.create_default_context()` | Loaded Windows cert store: `{"x509": 61, "crl": 0, "x509_ca": 59}` | **Pass** |
| `hashlib.sha256(b"virgo-appcontainer-test")` | `767be2eb7366b672d576c51332f1e0453eb0926a2e028bf583368d9ac0a6945b` | **Pass** |
| Direct `open("NUL", "wb")` & `subprocess.DEVNULL` | Direct write returned `10` bytes; nested child with `DEVNULL` exited `0` | **Pass** |
| Nested child separate stdout/stderr pipes | `stdout="OUT_OK"`, `stderr="ERR_OK"` | **Pass** |
| Simultaneous large pipes (>64 KB) | `262,144` bytes stdout + `131,072` bytes stderr, `exact_match=true` (`38.537 ms`) | **Pass** |
| Job Object process-tree kill (`KILL_ON_JOB_CLOSE`) | Child PID `25316` spawned sleeping grandchild PID `35076`; on `3.0 s` timeout (`3021.708 ms`), `timed_out=true`, `returncode=1`, `job_active_after_drain=0`, `child_alive_after_timeout=false`, `grandchild_alive_after_timeout=false` | **Pass** |

Residual ACE audit after Phase A and Phase B sessions: **`0` residual ACEs** across all 7 checked roots (`phase_a_workspace`, `phase_a_host_backed_venv`, `phase_b_workspace`, `phase_b_user_python`, `phase_b_venv`, `phase_b_user_node`, `phase_b_user_git`).

---

## 4. Verification Area 2 — Session-Level Single SID + Inheritable ACE Performance & Scalability

Experiment 2 (`experiments/windows_appcontainer/exp_scale.py`) tested 3 workspace scales (`small_10`: 31 files / 15 dirs; `medium_1000`: 1,021 files / 51 dirs; `large_10000`: 10,021 files / 211 dirs), each paired with a real `.venv` (`1,078` files / `116` dirs / `1,194` entries) and user-owned CPython (`3,659` unique NTFS entries propagated by `SetNamedSecurityInfoW`; raw `os.walk()` in the recorded JSON double-counted the `uv` minor-version junction as `6,778` files / `533` dirs) plus 4 existing protected metadata targets (`.git`, `.virgo`, `AGENTS.md`, `CLAUDE.md`). Each scale ran **3 fresh AppContainer sessions** (`samples = 3`), executing **10 native commands + 10 Python commands per sample** (`30` total launches per command type per scale).

Every grant, write-restriction, and revocation used **one `SetNamedSecurityInfoW` call per root** (no Python per-file ACL walk).

### Session Cold-Start Timings (Budget: `<= 5,000 ms`)

#### Per-Sample Raw Cold-Start Breakdown (ms)

| Scale (Total Files) | Sample | Profile Create | Grant Workspace | Grant `.venv` (1,078f) | Grant `user_python` (3,659 entries) | Deny Protected (4 targets) | Cold Start (`ws + .venv + denies`) | Cold Start (All incl. `user_python`) |
| --- | :---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **Small (31f)** | `s0` | `7.797` | `3.150` | `60.764` | `188.876` | `3.630` | `75.341` | `264.217` |
| **Small (31f)** | `s1` | `8.771` | `5.716` | `72.364` | `250.616` | `3.152` | `90.002` | `340.618` |
| **Small (31f)** | `s2` | `25.681` | `6.019` | `104.417` | `177.549` | `2.309` | `138.426` | `315.975` |
| **Medium (1,021f)** | `s0` | `8.686` | `52.706` | `61.629` | `193.768` | `4.999` | `128.020` | `321.788` |
| **Medium (1,021f)** | `s1` | `8.505` | `57.177` | `60.537` | `240.026` | `2.496` | `128.716` | `368.742` |
| **Medium (1,021f)** | `s2` | `7.724` | `58.784` | `66.185` | `260.070` | `2.679` | `135.372` | `395.442` |
| **Large (10,021f)** | `s0` | `8.029` | `528.741` | `86.422` | `209.636` | `2.673` | `625.865` | `835.501` |
| **Large (10,021f)** | `s1` | `8.048` | `546.218` | `88.328` | `204.225` | `2.505` | `645.099` | `849.324` |
| **Large (10,021f)** | `s2` | `9.483` | `510.142` | `71.201` | `190.210` | `2.639` | `593.466` | `783.676` |

*(Per-target `deny_write_path` calls were consistently sub-millisecond to low single-digit milliseconds across all 9 samples: `.git` `1.634`–`3.601 ms`, `.virgo` `0.175`–`0.522 ms`, `AGENTS.md` `0.114`–`0.851 ms`, `CLAUDE.md` `0.085`–`0.194 ms`.)*

#### Cold-Start Summary (min / median / max in ms vs. `5,000 ms` Budget)

| Scale | Profile Create (min / med / max) | Grant Workspace (min / med / max) | Grant `.venv` (min / med / max) | Deny Protected (min / med / max) | Cold Start `ws + .venv` (min / med / max) | Cold Start All incl. `user_python` (min / med / max) | Headroom vs. `5,000 ms` |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| **Small (31f)** | `7.797` / **`8.771`** / `25.681` | `3.150` / **`5.716`** / `6.019` | `60.764` / **`72.364`** / `104.417` | `2.309` / **`3.152`** / `3.630` | `75.341` / **`90.002`** / `138.426` | `264.217` / **`315.975`** / `340.618` | **15.8×** (all) |
| **Medium (1,021f)** | `7.724` / **`8.505`** / `8.686` | `52.706` / **`57.177`** / `58.784` | `60.537` / **`61.629`** / `66.185` | `2.496` / **`2.679`** / `4.999` | `128.020` / **`128.716`** / `135.372` | `321.788` / **`368.742`** / `395.442` | **13.6×** (all) |
| **Large (10,021f)** | `8.029` / **`8.048`** / `9.483` | `510.142` / **`528.741`** / `546.218` | `71.201` / **`86.422`** / `88.328` | `2.505` / **`2.639`** / `2.673` | `593.466` / **`625.865`** / `645.099` | `783.676` / **`835.501`** / `849.324` | **6.0×** (all) / **8.0×** (ws+venv) |

OS-propagated `SetNamedSecurityInfoW` scaled cleanly at ~`53 ms` per 1,000 workspace files (`57.177 ms` for 1,021 files; `528.741 ms` for 10,021 files), and runs **once per session** instead of once per command.

### Per-Command Launch Overhead (Budget: `<= 1,000 ms`, O(1) Scaling)

Measured across `30` sequential commands per scale (3 fresh sessions × 10 commands each):

| Scale (Total Files) | `cmd.exe /c exit 0` Total (min / **median** / mean / max ms) | `cmd.exe` `CreateProcessW` Only (min / **median** / max ms) | `.venv python -c "print(1)"` Total (min / **median** / mean / max ms) | Headroom vs. `1,000 ms` |
| --- | --- | --- | --- | ---: |
| **Small (31f)** | `15.490` / **`16.884`** / `17.532` / `29.452` | `1.165` / **`1.260`** / `11.040` | `37.877` / **`40.791`** / `41.159` / `44.648` | **24.5×** (Python) |
| **Medium (1,021f)** | `15.915` / **`17.311`** / `17.568` / `20.211` | `1.186` / **`1.610`** / `2.832` | `38.506` / **`41.118`** / `41.468` / `46.047` | **24.3×** (Python) |
| **Large (10,021f)** | `15.948` / **`17.229`** / `17.325` / `19.565` | `1.157` / **`1.363`** / `1.929` | `39.125` / **`41.696`** / `41.635` / `44.920` | **24.0×** (Python) |

Per-command latency is **strictly O(1)** with workspace size: increasing the workspace by 323× (31 -> 10,021 files) moved median `cmd.exe` launch by `+0.345 ms` (`16.884 -> 17.229 ms`) and median `.venv python` launch by `+0.905 ms` (`40.791 -> 41.696 ms`). Compared to the frozen LPAC backend (~`14,000 ms` per command), this is a **~335× per-command speedup**.

### Session Teardown Timings & Residual ACE Audit

| Scale | Sample | Revoke Protected (ms) | Revoke Workspace (ms) | Revoke `.venv` (ms) | Revoke `user_python` (ms) | Delete Profile (ms) | Teardown `ws + .venv` (ms) | Teardown All (ms) | Residual ACEs (incl. new file) |
| --- | :---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **Small (31f)** | `s0` / `s1` / `s2` | `2.004` / `1.999` / `4.870` | `1.346` / `1.551` / `2.567` | `105.785` / `80.939` / `90.663` | `219.667` / `194.199` / `195.985` | `3.742` / `3.782` / `3.219` | `112.877` / `88.270` / `101.320` (**med `101.320`**) | `332.544` / `282.469` / `297.305` (**med `297.305`**) | **`0`** |
| **Medium (1,021f)** | `s0` / `s1` / `s2` | `2.510` / `2.317` / `2.365` | `64.226` / `51.138` / `48.368` | `72.445` / `59.955` / `80.950` | `216.133` / `203.798` / `200.217` | `4.699` / `3.778` / `4.977` | `143.880` / `117.189` / `136.660` (**med `136.660`**) | `360.013` / `320.987` / `336.877` (**med `336.877`**) | **`0`** |
| **Large (10,021f)** | `s0` / `s1` / `s2` | `2.166` / `2.262` / `2.698` | `525.892` / `528.953` / `525.611` | `64.434` / `67.191` / `69.940` | `194.119` / `199.780` / `188.100` | `3.649` / `3.923` / `3.213` | `596.142` / `602.328` / `601.461` (**med `601.461`**) | `790.260` / `802.108` / `789.562` (**med `790.260`**) | **`0`** |

- **Files created during the session**: Every sample created `created_during_session_<idx>.txt` from inside AppContainer (`created_file_during_session_ok == true` on 9/9 samples). A single root `SetNamedSecurityInfoW` revocation cleanly removed the inherited session SID from all pre-existing AND newly created files (`total_residual_aces_across_all_samples == 0`).
- **NTFS `SE_DACL_PROTECTED` edge case** (`se_dacl_protected_edge_case`):
  1. **`protected_before_grant`** (inheritance disabled on a subdirectory *before* workspace grant): `SetNamedSecurityInfoW` on workspace root **skipped** it (`sid_propagated_by_root_grant == false`). Inside AppContainer, reading and writing `inside_before.txt` both failed with `[Errno 13] Permission denied`.
  2. **`protected_after_grant`** (inheritance disabled with `copy_inherited=True` *after* workspace grant): the session SID became an explicit ACE (`sid_present_before_revoke == true`). Root-only revocation left **`2` residual paths** (`protected_after_grant` and `protected_after_grant\inside_after.txt`), which cleared completely once explicitly revoked (`residual_paths_after_explicit_cleanup == []`). Because the AppContainer profile is deleted at teardown, a stray SID on a user-protected subdirectory cannot grant access to future sessions, but Stage 2 should document this NTFS inheritance caveat honestly.

---

## 5. Verification Area 3 — Guardrail Boundaries, Ecosystem Compatibility & Crash Recovery

### Private Directories & Outside-Workspace Write Isolation

All 12 probes ran inside a writable Workspace-Edit AppContainer session with **zero** ACL edits to `%USERPROFILE%`, `~/.ssh`, or any private directory:

| Probe | Target | Allowed? | Exact Exception / Error Code |
| --- | --- | :---: | --- |
| Read private canary dir | `%USERPROFILE%\.virgo_ac_private_canary_*` *(default user-profile ACL; `%USERPROFILE%\.virgo` absent on host)* | **No** | `PermissionError`: `[WinError 5]` (`errno=13`) |
| Read private canary file | `...\secret.txt` | **No** | `PermissionError`: `[Errno 13] Permission denied` |
| Read `~/.ssh` dir | `C:\Users\zhang\.ssh` | **No** | `PermissionError`: `[WinError 5]` (`errno=13`) |
| Read SSH private key | `C:\Users\zhang\.ssh\id_ed25519` | **No** | `PermissionError`: `[Errno 13] Permission denied` |
| Read `Desktop` dir | `C:\Users\zhang\Desktop` | **No** | `PermissionError`: `[WinError 5]` (`errno=13`) |
| Read `Documents` dir | `C:\Users\zhang\Documents` | **No** | `PermissionError`: `[WinError 5]` (`errno=13`) |
| Read `~/.gitconfig` | `C:\Users\zhang\.gitconfig` | **No** | `PermissionError`: `[Errno 13] Permission denied` |
| Read system hosts *(AAP control)* | `C:\Windows\System32\drivers\etc\hosts` | **Yes** (`64` B) | Expected: `System32` grants `S-1-15-2-1 (RX)` |
| Write `%USERPROFILE%` root | `C:\Users\zhang\.virgo_ac_escape_*.txt` | **No** | `PermissionError`: `[Errno 13] Permission denied` |
| Write private canary dir | `...\escape.txt` | **No** | `PermissionError`: `[Errno 13] Permission denied` |
| Write sibling user dir | `%USERPROFILE%\.virgo_ac_sibling_canary_*\escape.txt` | **No** | `PermissionError`: `[Errno 13] Permission denied` |
| Write `C:\Windows\Temp` | `C:\Windows\Temp\virgo_ac_escape_*.txt` | **No** | `PermissionError`: `[Errno 13] Permission denied` |

Because `%USERPROFILE%` does not grant `ALL APPLICATION PACKAGES`, Standard AppContainer blocks private credential and user-file reads **by default** — completely eliminating the per-launch `~/.virgo` ACL walks that caused `WinError 5` conflicts in the LPAC backend.

### Control Metadata Write Protection (VER-01): Three-Way NTFS Kernel Comparison

Testing VER-01 on `.git`, `.virgo/config.json`, `AGENTS.md`, and `CLAUDE.md` uncovered **two critical Windows kernel security behaviors** proved by a 3-way empirical comparison:

1. **Windows ignores `ACCESS_DENIED_ACE` targeting AppContainer SIDs (`S-1-15-2-*`)**: In `ntoskrnl!SepNormalAccessCheck`, the package-SID access pass ignores explicit `ACCESS_DENIED_ACE` entries for `S-1-15-2-*`. Under `deny_mode="deny_ace"`, **every** write, rename, file unlink, and `git add` on protected files **succeeded** despite a canonically ordered explicit `DENY` ACE before `ALLOW`!
2. **`FILE_DELETE_CHILD` (`0x40`) on the workspace root bypasses child read-only delete/rename protection**: Switching to `deny_mode="protected_readonly"` (`PROTECTED_DACL_SECURITY_INFORMATION`, converting non-session ACEs to explicit entries so host users keep Full Control and granting the session SID explicit `ALLOW (RX) 0x001200A9`) blocked all writes, **but** when the workspace root granted `FILE_DELETE_CHILD` (`allow_delete_child=True`, mask `0x001301FF`), **all 4 renames** (`AGENTS.md`, `CLAUDE.md`, `.virgo`, `.git`) and **both file unlinks** (`AGENTS.md`, `CLAUDE.md`) STILL succeeded!
3. **Working VER-01 combination**: `deny_mode="protected_readonly"` **plus** `allow_delete_child=False` (`WRITE_NO_DELETE_MASK 0x001201BF` with `OI|CI` + inherit-only `DELETE 0x00010000` with `OI|CI|IO` on the workspace root) **blocked 100% of writes, renames, deletes, and `git add`**, while keeping normal workspace create/edit/rename/delete, protected-target reads, and plain `git status`/`diff`/`log` fully functional.

| Probe Category & Target | `deny_mode="deny_ace"` (`allow_delete_child=False`) | `deny_mode="protected_readonly"`, `allow_delete_child=False` **(Recommended)** | `deny_mode="protected_readonly"`, `allow_delete_child=True` |
| --- | --- | --- | --- |
| **Normal workspace file** (create, edit, rename, delete) | Allowed | **Allowed** (`renamed_read="hello"`, `deleted_ok=True`) | Allowed |
| **Reads** (`AGENTS.md`, `CLAUDE.md`, `.virgo/config.json`, `.git/HEAD`) | 4/4 Allowed | **4/4 Allowed** | 4/4 Allowed |
| **Writes** (7 probes: write/append `AGENTS.md`, truncate `CLAUDE.md`, write/create in `.virgo`, write `.git/config`, create `.git/hooks/pre-commit`) | **7/7 Allowed (BYPASSED)** | **7/7 Blocked** (`PermissionError`, `Errno 13`) | **7/7 Blocked** (`PermissionError`, `Errno 13`) |
| **Renames** (4 probes: `AGENTS.md`, `CLAUDE.md`, `.virgo`, `.git`) | **4/4 Allowed (BYPASSED)** | **4/4 Blocked** (`PermissionError`, `WinError 5`) | **4/4 Allowed (BYPASSED via `0x40`)** |
| **Deletes** (6 probes: unlink `AGENTS.md`, `CLAUDE.md`, `.virgo/config.json`, `.git/HEAD`; `rmdir .virgo`; `rmtree .git`) | 4/6 Allowed (`rmdir .virgo` `WinError 145` non-empty; `rmtree .git` hit RO object `WinError 5`) | **6/6 Blocked** (`PermissionError`, `WinError 5`) | `unlink AGENTS.md` & `CLAUDE.md` **Allowed (BYPASSED)**; inner `.virgo`/`.git` deletes blocked (`WinError 5`/`145`) |
| **Plain `git status --short` / `--no-optional-locks status` / `-c core.fsmonitor=false --no-optional-locks status`** | `rc=0` | **`rc=0`** (` M app.py`, `?? .virgo/`) | `rc=0` |
| **`git diff` & `git log --oneline -n 3`** | `rc=0` | **`rc=0`** | `rc=0` |
| **`git add app.py`** (mutates `.git/index.lock`) | **`rc=0` (BYPASSED)** | **Blocked (`rc=128`**: `fatal: Unable to create '.../.git/index.lock': Permission denied`) | Blocked (`rc=128`) |
| **Residual ACEs after teardown** | `0` | **`0`** | `0` |

> **Honest Limitation — Non-Existent Protected Names:** In `ws_nonexistent` (where `CLAUDE.md` and `.virgo` did not exist at session setup), creating `CLAUDE.md` and `.virgo/evil.json` from inside AppContainer **both succeeded** (`allowed=true`, `claude_md_exists_after=true`, `virgo_evil_exists_after=true`). NTFS ACLs apply only to existing filesystem objects — the exact same class of limitation documented for Linux `bubblewrap` read-only bind-mounts in [sandbox.md](../guides/sandbox.md). Virgo's Broker file tools (`PermissionPolicy`) and untrusted project-config stripping (R5) continue to defend absent protected names.

### Host Ecosystem Compatibility While Session ACEs Are Active

With an active Workspace-Edit session holding workspace grants and `protected_readonly` DACLs on `.git`, `.virgo`, `AGENTS.md`, and `CLAUDE.md`, all **6/6 host operations** succeeded with **zero `WinError 5`**:

| Host Operation (while sandbox active) | Result | Details |
| --- | :---: | --- |
| Atomic editor save (`os.replace`) over `app.py` | **Pass** | `ok=True` |
| Atomic editor save (`os.replace`) over protected `AGENTS.md` | **Pass** | `ok=True` (preserved host Full Control ACEs unaffected) |
| Host `git status --short` | **Pass** | `rc=0` (`M AGENTS.md`, ` M app.py`, `?? .virgo/`) |
| Host `git add app.py AGENTS.md` | **Pass** | `rc=0` |
| Host `git commit -m "host commit while sandbox active"` | **Pass** | `rc=0` (`[main 932205a]`) |
| Background atomic user-settings save (`os.replace`) | **Pass** | `ok=True` |

Residual ACEs after teardown: **`0`**.

### Crash Recovery After Win32 `TerminateProcess`

`experiments/windows_appcontainer/state.py` writes an atomic (`fsync` + `os.replace`) write-ahead JSON state journal **before** mutating any NTFS ACL. Each journal records `session_id`, `profile_name`, `sid_str`, `owner_pid`, `owner_create_time` (100 ns `FILETIME` from `GetProcessTimes` to defeat PID reuse), `created_at`, `granted_roots`, `denied_paths` (including pre-session `was_protected` and base64-encoded `original_explicit_aces` for exact restoration), and `capabilities`.

Verified crash-recovery sequence:
1. A separate host worker (`PID 12352`) called `AppContainerSession.setup()` (`VirgoProto.0138d2bfa6f843ae`), persisted state, applied grants (`ws_crash` + `user_python`) and 4 protected denies, and was hard-killed via Win32 `TerminateProcess` (`proc.kill()`, `terminate_exit_code=1`, `worker_alive_after_kill=false` — no Python `finally` or `atexit` ran).
2. **Pre-recovery audit**: confirmed `state_file_exists=true`, `profile_folder_exists=true`, **`51`** lingering workspace ACEs, and **`3,659`** lingering `user_python` ACEs.
3. **Recovery (`recover_stale_sessions()`)**: detected `owner_alive=false`, revoked 2 granted roots first (fail-closed) then restored all 4 protected paths (`UNPROTECTED_DACL_SECURITY_INFORMATION`), deleted the AppContainer profile, and unlinked the state file in **`254.985 ms`** (`status="recovered"`, `errors=[]`).
4. **Post-recovery audit**: `state_file_exists=false`, `profile_folder_exists=false`, `GetAppContainerFolderPath` returned **`HRESULT 0x80070002`** (`ERROR_FILE_NOT_FOUND`), `residual_aces_workspace=[]`, `residual_aces_user_python=[]`, `fully_clean=true`.
5. **Idempotency**: calling `recover_stale_sessions()` a second time returned `[]`.

---

## 6. Verification Area 4 — Network Isolation

Experiment 4 (`experiments/windows_appcontainer/exp_network.py`) ran with `is_user_an_admin=false`, `firewall_rules_modified=false`, and `sandbox_proxy_env_cleared=true`, while the host's `FlClash` TUN (`Meta Tunnel`) was active. Host-side loopback listeners were verified reachable from the host before starting (`127.0.0.1:56565` -> `LOOPBACK_V4_OK`, `[::1]:56566` -> `LOOPBACK_V6_OK`, `127.0.0.1:54111` UDP -> `UDP_OK:ping`).

### Socket & Protocol Isolation Matrix

| Probe | Read-Only (`caps=()`, SIDs `[]`) | Workspace-Edit (`internetClient`, `privateNetworkClientServer`; SIDs `S-1-15-3-1`, `S-1-15-3-3`) | Comparison (`+ internetClientServer`; SIDs `S-1-15-3-1`, `-2`, `-3`) |
| --- | --- | --- | --- |
| **TCP `1.1.1.1:443`** | **Blocked** (`PermissionError`, **`WinError 10013`** / `WSAEACCES`) | **Allowed** (`peer=['1.1.1.1', 443]`) | **Allowed** (`peer=['1.1.1.1', 443]`) |
| **DNS `getaddrinfo("example.com", 443)`** | **Blocked** (`gaierror`, **`Errno 11001`**) | **Allowed** (`['198.18.1.71']` via host TUN DNS) | **Allowed** (`['198.18.1.71']`) |
| **TCP `github.com:443`** | **Blocked** (`gaierror`, **`Errno 11001`**) | **Allowed** (`peer=['198.18.0.91', 443]`) | **Allowed** (`peer=['198.18.0.91', 443]`) |
| **UDP `1.1.1.1:53`** (raw DNS packet) | **Blocked** (`TimeoutError: timed out`) | **Allowed** (`sent=29`, `recv_bytes=45`) | **Allowed** (`sent=29`, `recv_bytes=45`) |
| **HTTPS `https://pypi.org/simple/`** | *(not reachable)* | **Allowed** (HTTP `200`, `256` B read) | **Allowed** (HTTP `200`, `256` B read) |
| **HTTPS `https://example.com`** | *(not reachable)* | **Allowed** (HTTP `200`, `256` B read) | **Allowed** (HTTP `200`, `256` B read) |
| **Loopback TCP `127.0.0.1:<port>`** | **Blocked** (`TimeoutError`) | **Blocked** (`TimeoutError`) | **Blocked** (`TimeoutError`) |
| **Loopback TCP `[::1]:<port>`** | **Blocked** (`TimeoutError`) | **Blocked** (`TimeoutError`) | **Blocked** (`TimeoutError`) |
| **Loopback UDP `127.0.0.1:<port>`** | **Blocked** (`TimeoutError`) | **Blocked** (`TimeoutError`) | **Blocked** (`TimeoutError`) |

### Real Developer CLIs in Workspace-Edit (`internetClient` + `privateNetworkClientServer`)

| CLI Check | Exit Code | Elapsed (ms) | Observed Output / Notes |
| --- | ---: | ---: | --- |
| `user_git\cmd\git.exe ls-remote https://github.com/octocat/Hello-World.git HEAD` *(inside git repo)* | **`0`** | `2939.727` | `7fd1a60b01f91b314f59955a4e4d4e80d8edf11d	HEAD` |
| `user_git\cmd\git.exe ls-remote ...` *(outside git repo under `C:\Users`)* | `128` | `34.141` | `fatal: failed to stat 'C:/Users/zhang/.virgo_ac_proto_exp4_*': Permission denied` (ancestor ACL quirk) |
| Stock junctioned `C:\Program Files\Git\cmd\git.exe ls-remote ...` | `1` | `27.223` | `error launching git:` (non-AAP `E:\Program Files\Git` target) |
| `.venv\Scripts\python.exe -m pip index versions packaging --disable-pip-version-check` | **`0`** | `2552.123` | Resolved `packaging (26.3)` from PyPI over HTTPS |
| `.venv\Scripts\uv.exe pip compile - --no-header` (stdin `packaging`) | **`0`** | `2444.753` | `packaging==26.3` (`Resolved 1 package in 2.13s`) |

### Network Isolation Key Findings

1. **Zero Admin rights, zero Firewall rule writes**: Windows Filtering Platform enforces AppContainer capabilities natively in kernel mode when `BFE` and `MpsSvc` are running.
2. **VPN / TUN robustness**: Even with `FlClash` TUN actively intercepting host routing (note the `198.18.x.x` Fake-IP addresses in Workspace-Edit), Read-Only AppContainer (`caps=()`) still blocked **100%** of outbound TCP, UDP, and DNS before packets left the socket layer.
3. **Loopback isolation caveat**: Windows blocks `127.0.0.1` and `[::1]` connections from AppContainer even with `internetClient`, `privateNetworkClientServer`, and `internetClientServer`. Exempting a package SID (`CheckNetIsolation LoopbackExempt`) requires Administrator rights, so loopback blocking must be treated as an honest Windows Standard AppContainer limitation for non-admin users. Residual ACEs after Experiment 4: **`0`**.

---

## 7. Phase 2 Rewrite Design Blueprint & Honest Platform Differences (R7 / R8)

### Architectural Blueprint for Stage 2 Production Rewrite

1. **Session lifecycle & profile scoping**:
   - Allocate one AppContainer profile/SID per `(session, workspace, mode_capability_set)` — i.e., a Read-Only profile (`capabilities=()`) and, when Workspace-Edit or a network grant is active, a Workspace-Edit profile (`capabilities=("internetClient", "privateNetworkClientServer")`).
   - Persist a write-ahead atomic JSON journal (`%LOCALAPPDATA%\Virgo\sandbox-sessions\<id>.json` including `owner_pid`, `owner_create_time` `FILETIME`, `granted_roots`, and `denied_paths` snapshots) **before** calling `SetNamedSecurityInfoW`.
   - Tear down fail-closed: revoke granted roots first, restore `protected_readonly` targets (`UNPROTECTED_DACL_SECURITY_INFORMATION` + original explicit ACEs), delete the AppContainer profile, and unlink the journal only on zero errors. Run `recover_stale_sessions()` automatically on Broker startup.
2. **ACL masks & VER-01 mechanism**:
   - Workspace root (writable): grant `WRITE_NO_DELETE_MASK` (`0x001201BF`, `OI|CI`) + inherit-only `DELETE` (`0x00010000`, `OI|CI|IO`). **Never** grant `FILE_DELETE_CHILD` (`0x40`).
   - Runtime roots & Read-Only workspace: grant `READ_EXECUTE_MASK` (`0x001200A9`, `OI|CI`).
   - Existing protected targets (`.git`, `.virgo`, `AGENTS.md`, `CLAUDE.md`): **do not use `ACCESS_DENIED_ACE`** (ignored for `S-1-15-2-*`). Use single-call `protected_readonly` (`PROTECTED_DACL_SECURITY_INFORMATION`, preserving all non-session ACEs as explicit entries + explicit `ALLOW (RX) 0x001200A9` for the session SID).
3. **Resolving `C:\Users` ancestor-directory friction (Stage 2 prerequisite spike)**:
   - Because `C:\Users` is `SYSTEM`-owned (`Protected=True`, `AAP=False`), tools that `stat()`/`NormalizeThePath` upward from `%USERPROFILE%\...` (`pwsh`, `powershell.exe`, `pytest`, `git init`, outside-repo `git`) hit `WinError 5`.
   - **Primary candidate to prototype first (R7 simplest mechanism)**: map a session-local unprivileged DOS drive (`DefineDosDeviceW` / `subst`-equivalent, e.g., `<drive>:\` -> workspace root) and set child `cwd` to `<drive>:\`. Because `<drive>:\` has no `C:\Users` parent component, a single OS-native mechanism may simultaneously fix `pwsh`/`powershell` `Get-Location`, `pytest` upward `stat()`, and `git` ancestor walks without tool-specific shims. Also verify why `pytest` `cacheprovider` raised `PytestCacheWarning: [WinError 5]` on `pytest-cache-files-*` (e.g., `TemporaryDirectory` / `chmod` interaction inside AppContainer).
   - **Fallback if `DefineDosDeviceW` does not bypass all cases**: combine `GIT_CEILING_DIRECTORIES=<workspace>`, PowerShell `New-PSDrive` startup prologue, and targeted `pytest` rootdir handling — and measure empirically before choosing.
4. **Runtime root discovery & fail-closed precheck (R6)**:
   - Resolve symlinks/junctions (`Path.resolve()`) on interpreter/tool roots (`sys.base_prefix`, active `.venv`, `git`, `node`, `pwsh`).
   - For each resolved root, check: (a) does its DACL already contain `S-1-15-2-1 (RX)`? If yes, zero ACL work needed; (b) otherwise, does `CreateFileW(..., WRITE_DAC)` succeed? If yes, include it in session read-only grants; (c) if neither holds (secondary-drive admin-owned Miniconda, junctioned `E:\Program Files\Git`, `Protected=True` `C:\Program Files\nodejs`), **fail closed with an actionable diagnostic** naming the exact non-AAP path and suggesting user-owned installs (`uv python install`, `%LOCALAPPDATA%\Programs\Python`) or AAP-inheriting `C:\Program Files` installs (or one-shot No-Limit).
5. **Process supervision, desktop & environment**:
   - Retain `CreateProcessW` (`CREATE_SUSPENDED`), `Verify -> Resume`, `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, and Virgo's startup-acknowledgement / no-replay semantics (R6).
   - Keep `desktop="auto"` (`WinSta0\Default` fallback without DACL edits) and `build_appcontainer_env()` base-`LOCALAPPDATA` handling.
6. **Non-existent protected names & loopback caveat**:
   - Do **not** pre-create phantom `CLAUDE.md` or `.virgo` files/directories inside clean user repositories (violates R3/R7 and pollutes `git status`); rely on Broker file tools (`PermissionPolicy`) + untrusted project-config stripping (R5), matching Linux `bwrap`.
   - Surface a clear hint when loopback (`127.0.0.1`/`[::1]`) fails inside Windows AppContainer, allowing the user to opt into a one-shot **No-Limit** invocation (R6) for local dev servers/databases/proxies.
7. **Stage 2 acceptance gate**:
   - Run the full native acceptance suite on both a UAC-filtered account AND a **pure non-Administrators Standard User account** (`TokenElevationType == 1`, `*Users` only) before switching native Windows' default from **No-Limit** back to **Workspace-Edit**.

### Honest Cross-Platform Comparison (R7 / R8)

| Dimension | Linux (Tier 1: `bubblewrap` + `libseccomp2`) | macOS (Tier 1: Seatbelt `/usr/bin/sandbox-exec`) | Native Windows (Stage 1 Standard AppContainer Prototype) |
| --- | --- | --- | --- |
| **Account / Privileges Required** | Unprivileged user (needs unprivileged user namespaces) | Unprivileged user | Unelevated Medium-IL user (**no Admin**, no device/firewall edits); pure Standard User pass pending in Stage 2 |
| **Filesystem Isolation Mechanism** | Mount namespace (`bwrap`) | Kernel MAC path profile (Seatbelt) | Per-session AppContainer SID + single-call OS-inherited NTFS DACLs (`SetNamedSecurityInfoW`) |
| **Cold Start & Per-Command Overhead** | Sub-second cold start, O(1) per-command | Sub-second cold start, O(1) per-command | `626`–`836 ms` cold start at 10,000 files + `.venv`; `~17 ms` native / `~41 ms` Python O(1) per-command |
| **Private State (`~/.virgo`, `~/.ssh`)** | Read/write denied via mount namespace (even if workspace is `$HOME`) | Read/write denied via Seatbelt profile (even if workspace is `$HOME`) | Read/write denied by default NTFS ACLs (`%USERPROFILE%` lacks `S-1-15-2-1`); `$HOME` as workspace still disallowed (would grant entire profile) |
| **Existing Control Metadata (VER-01)** | Read-only bind mounts | Seatbelt path write-deny | `protected_readonly` (`PROTECTED_DACL` + `RX`) + omitting `FILE_DELETE_CHILD (0x40)` — blocks write, rename, delete, `git add` |
| **Non-Existent Protected Names** | Not blocked by `bwrap` bind mounts (defended by Broker + R5 config stripping) | Blocked by Seatbelt path rules | Not blocked by NTFS object ACLs (defended by Broker + R5 config stripping) |
| **Read-Only Network Isolation** | Network namespace (`--unshare-net`) blocks all sockets | Seatbelt network deny | AppContainer `capabilities=()` blocks TCP (`WinError 10013`), DNS (`Errno 11001`), UDP, and loopback (`TimeoutError`) via WFP |
| **Workspace-Edit Outbound Network** | Allowed (host netns) | Allowed | Allowed (`internetClient` + `privateNetworkClientServer`) for TCP/DNS/UDP/HTTPS/`git`/`pip`/`uv` |
| **Localhost / Loopback (`127.0.0.1`)** | Allowed in Workspace-Edit | Allowed in Workspace-Edit | **Blocked (`TimeoutError`)** in all non-admin AppContainer modes (requires Admin `CheckNetIsolation` or one-shot No-Limit) |
| **Process-Tree Cleanup** | `HostProcess` (`start_new_session` + `os.killpg`) | `HostProcess` (`start_new_session` + `os.killpg`; `setsid()` escapes noted) | Kernel Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) — reliably kills children and grandchildren |
| **Crash Cleanup** | Ephemeral mount namespace (kernel reaps automatically) | Ephemeral profile (kernel reaps automatically) | Write-ahead JSON journal + startup `recover_stale_sessions()` (`~255 ms`, 0 residual ACEs, idempotent) |
| **Workspace `SE_DACL_PROTECTED` Subdirs** | N/A | N/A | Pre-protected subdirs are skipped by root grant (inaccessible); post-grant protected subdirs retain explicit SID until explicitly revoked |

### Execution Safety Review Alignment (R1–R8)

- **R1 / R2 (Guardrail, not a fortress)**: Bounds accidental writes outside the workspace, credential reads (`~/.ssh`, `~/.virgo`), control-metadata edits (`.git`, `.virgo`, `AGENTS.md`, `CLAUDE.md`), and Read-Only exfiltration. Does **not** promise defense against malicious code, adversarial evasion, pre-planted reparse/symlink attacks, `SE_DACL_PROTECTED` tampering, or prompt-injection attack chains (**R8**).
- **R3 (Friction & performance)**: Eliminates the ~14 s/command LPAC regression (`~17`–`41 ms` O(1) per command, `< 0.85 s` cold start at 10,000 files), avoiding the friction that pushes users to **No-Limit** — provided Stage 2 resolves the `C:\Users` ancestor-dir friction for `pwsh`/`pytest`/`git`.
- **R4 / R5 (Scope & trust)**: Applies OS sandboxing strictly to Shell commands; Broker file tools enforce `PermissionPolicy` directly, and trusted user integrations remain on Host.
- **R6 (Fail closed, no silent fallback/replay)**: Preserves suspended-process token verification before `ResumeThread` and requires explicit fail-closed errors for non-AAP/non-user-owned runtimes or unavailable WFP services (`BFE`/`MpsSvc`).
- **R7 (Simplest mechanism)**: Replaces the frozen Admin-only LPAC + per-file handle-walk + device-DACL backend (`windows_sandbox.py` / `windows_acl.py` / `windows_files.py`) with Standard AppContainer + single-call OS inheritance, documenting loopback, absent protected names, and `SE_DACL_PROTECTED` honestly.

---

## 8. Reproducibility

All prototype modules and experiment harnesses live under `experiments/windows_appcontainer/`:

- Core prototype: `__init__.py`, `appcontainer.py`, `acl.py`, `state.py` (Python stdlib + `ctypes` only; zero third-party runtime dependencies).
- Experiment suite: `exp_env.py`, `exp_toolchain.py`, `exp_scale.py`, `exp_guardrails.py`, `exp_network.py`, `run_all.py`.

### Running the Full Validation Suite (PowerShell, Unelevated)

From the repository root using a real native x64 Python 3.11+ interpreter (not the WindowsApps store stub; e.g., `E:\Users\zhang\miniconda3\python.exe` on this test host):

```powershell
# Full 5-stage suite (~6 minutes including one-time temporary fixture bootstrap;
# removes all temp dirs, ACEs, and VirgoProto* profiles automatically on exit)
& "<path-to-native-python.exe>" experiments\windows_appcontainer\run_all.py `
  --output .\prototype_results.json
```

Run individual phases as needed:

```powershell
# Host environment & baseline DACL telemetry only (fast, read-only)
& "<path-to-native-python.exe>" experiments\windows_appcontainer\run_all.py --only env

# Run a targeted subset (e.g., guardrails and network only)
& "<path-to-native-python.exe>" experiments\windows_appcontainer\run_all.py --only exp3,exp4
```

### Post-Run Cleanliness Verification

`run_all.py` exits `0` only if its built-in `post_run_cleanliness_audit` confirms `clean == true`:
- Zero `VirgoProto*` directories under `%LOCALAPPDATA%\Packages\`
- Zero `.virgo_ac_*` temporary directories under `%USERPROFILE%\`
- Zero stray custom AppContainer package SID ACEs (`S-1-15-2-*` excluding well-known `S-1-15-2-1` and `S-1-15-2-2`) on `%USERPROFILE%`, the host Python root, or the repository root.

*(In the recorded validation run, `run_all.py` completed in `366.505 s` — including `258.474 s` of one-time fixture bootstrap — exited `0`, and passed `post_run_cleanliness_audit.clean == true`.)*
