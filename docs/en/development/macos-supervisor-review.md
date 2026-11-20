# Review of macOS Launchd Supervisor (`src/virgo/sandbox/macos_process.py`)

> Scope: Checklist item **S5-03** — re-evaluate the 785-line macOS launchd supervisor against Virgo's execution safety principles (**R1–R7**: "Guardrail, not fortress"). Native Windows LPAC (`windows_sandbox.py` / `windows_acl.py`, ~2,200 lines) remains frozen per §6.2 pending its future AppContainer rewrite.

## 1. Current Architecture Summary

Every macOS sandboxed Shell command currently executes through `LaunchdProcess` (`src/virgo/sandbox/macos_process.py`, 785 lines):

1. **Private XNU SPI (`_Kernel`, lines 196–341)**: Dynamically loads undocumented symbols from `libSystem.B.dylib` / `libproc.dylib` (`coalition_terminate`, `coalition_info_resource_usage`, `proc_pidinfo` flavors 17 & 20, `proc_listallpids`, `proc_signal_with_audittoken`) with hardcoded C struct sizes/offsets (`sizeof(_ProcessIdentity) == 56`, `version.offset == 32`, `0xFFFFFFFF` audit-token fields).
2. **Startup capability probe (`_ensure_capability` + `mode == 'probe'`, lines 91–125, 731–785)**: Before the first sandboxed command in a broker process, bootstraps a throwaway launchd job that double-forks a `setsid()` orphan ignoring `SIGTERM` with `signal.alarm(12)` to prove coalition-based termination works.
3. **Per-command launchd + Python supervisor round-trip (`LaunchdProcess`, lines 344–729)**:
   - Creates `/private/tmp/virgo-control-<uid>/vgo-*` (`0700`), a Unix domain socket, and a `job.plist`.
   - Runs `/bin/launchctl bootstrap user/<uid> job.plist` to spawn a separate Python `-I -S -c _SUPERVISOR` process inside a fresh launchd resource coalition.
   - Verifies `getpeereid`, `LOCAL_PEERPID`, a 256-bit random token, `coalition_terminate(0, 0) == EPERM`, and coalition active count `== 1`.
   - Passes `stdin`/`stdout`/`stderr`/`status_fd` over `SCM_RIGHTS` (`socket.send_fds`) and JSON argv/cwd/env over the socket.
   - The supervisor finally spawns `/usr/bin/sandbox-exec -f <profile> ...` with `start_new_session=True`.
   - On completion or cancellation, runs `/bin/launchctl bootout`, `/bin/launchctl print`, and polls `coalition_info_resource_usage` / `proc_listallpids` / `proc_signal_with_audittoken` until the coalition drains.

## 2. Classification Against Principles R1–R7

| Component | Lines | Threat Addressed | Classification | Assessment |
| --- | ---: | --- | --- | --- |
| Seatbelt profile enforcement (`/usr/bin/sandbox-exec`) | (in `platform.py`) | Accidental writes outside workspace, control metadata modification, private state reads, Read-Only network off | **Guardrail (R2)** | **Keep** — core macOS boundary. |
| Startup acknowledgement (`status_fd`) + no-replay semantics | ~40 | Distinguishing pre-exec failure from post-start failure (R6) | **Guardrail (R6)** | **Keep** — prevents automatic replay of started commands. |
| Process-group cleanup on timeout/cancel (`start_new_session=True` + `os.killpg`) | (shared with `HostProcess`) | Runaway tests, stuck dev servers, pipelines (accidental mistakes) | **Guardrail (R2/R3)** | **Keep** — handles normal child trees cleanly. |
| Launchd job + Python `_SUPERVISOR` + Unix socket + `SCM_RIGHTS` FD passing | ~340 | Preventing a **double-forking `setsid()` orphan ignoring `SIGTERM`** from surviving cleanup | **Adversarial (R1)** | **Prime simplification candidate.** A benign model rarely intentionally double-forks `SIG_IGN` daemons to evade cancellation; adds ~250–350 ms and multiple subprocesses (`launchctl` × 3 + extra Python) per command. |
| Undocumented XNU coalition SPI (`_Kernel`, `proc_signal_with_audittoken`, hardcoded offsets) | ~145 | Defeating PID-reuse races and `setsid()` escapes during cleanup | **Adversarial (R1/R7)** | **High maintenance risk.** Relies on Apple private SPI (`proc_info_private.h`) that can break silently on any macOS point release, causing fail-closed outages that push users to No-Limit (R3). |
| Detached-orphan startup capability probe (`_ensure_capability` + `mode == 'probe'`) | ~90 | Proving private coalition SPI still works on the host before first command | **Adversarial (R1/R3)** | **Eliminated automatically** if launchd coalitions are replaced by direct `sandbox-exec` + `HostProcess`. |
| `coalition_terminate(0, 0) == EPERM` privilege check + 256-bit socket auth token + `LOCAL_PEERPID` | ~70 | Preventing a same-UID attacker or privileged coalition helper from hijacking the supervisor socket | **Adversarial (R1)** | **Eliminated automatically** when there is no intermediate supervisor socket. |

## 3. Concrete Simplification Candidates

### Option A (Recommended for a follow-up PR): Direct `sandbox-exec` via `HostProcess`

- Spawn `/usr/bin/sandbox-exec -f <profile> ...` directly from `NativePolicy.spawn` using `subprocess.Popen(..., start_new_session=True)` wrapped in `HostProcess` (just like Linux `bwrap` and POSIX Host commands).
- Keep the existing `status_fd` startup acknowledgement pipe (passing the pipe FD via `pass_fds` directly to the Seatbelt launcher Python snippet) so pre-exec vs post-start failure classification and no-replay semantics (R6) remain 100% intact.
- Keep `HostProcess`'s `waitid(WNOWAIT)` / `kqueue` + `os.killpg(pid, SIGKILL)` reaping on normal exit and cancellation.
- **Expected impact**:
  - Deletes ~750 lines of private XNU SPI and IPC code (`macos_process.py`).
  - Removes all dependence on Apple undocumented coalition SPIs (eliminating macOS OS-upgrade fragility).
  - Cuts per-command macOS sandbox latency dramatically (removes 1 extra Python process + 3 `launchctl` invocations per command, plus the startup probe).
  - Honest tradeoff to document in `sandbox.md` (R7/R8): a command that explicitly calls `setsid()` to leave its process group is not reaped automatically on macOS (same as POSIX Host commands).

### Option B (Minimal intermediate step): Keep `LaunchdProcess` only if detached-daemon cleanup is explicitly needed

- If detached background daemons spawned inside tests turn out to be a common non-adversarial failure mode on macOS, remove `_ensure_capability` (run coalition cleanup best-effort rather than blocking startup with a double-fork probe) and keep `os.killpg` as the primary path.
- However, given Option A matches `HostProcess` and removes the fragile private XNU SPI completely, **Option A** aligns much more cleanly with R1, R3, and R7.

## 4. Non-Action Scope

Per decision record §6.2, this review does **not** modify `macos_process.py` in the current stabilization stack (keeping macOS tests and process-tree guarantees stable while landing S1–S7) and does **not** touch `windows_sandbox.py` or `windows_acl.py`. Option A should be proposed as its own focused follow-up PR with before/after `measure_permission_modes.py` measurements on macOS and an explicit security-tradeoff note per `AGENTS.md` rule #9.
