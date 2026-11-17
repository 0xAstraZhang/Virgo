# MXC Windows replacement evaluation

This is an isolated evaluation, not an enabled product backend. Do not merge it
or remove the existing Windows backend based on a successful capability probe.

## Frozen inputs and gates

- Virgo baseline: `f33be1c` (resolve and record the full SHA in each report).
- MXC: `v1.1.0`, published release archive SHA-256
  `31a44963e288e3af23bc2e7e350608ed28f7c76b922ee7a7f1758a486dda37b3`.
- Existing hosts: GitHub `windows-2022` and `windows-2025`, x64.
- First establish native ProcessContainer availability and attempt an actual
  harmless payload. Do not use WSL, a VM, permissive learning mode, host execution,
  ACL fallback, or a weaker MXC revision to turn an unsupported host into a pass.
- Run the unmodified `tests/harness/test_windows_sandbox_e2e.py` on the pinned
  Virgo baseline; retain JUnit, individual durations, raw output and environment.
- Only if actual MXC startup succeeds, continue with a product adapter and the
  same existing end-to-end cases. Backend-specific LPAC/token/ACL assertions must
  remain visible as contract differences, not be silently skipped or weakened.
- If both existing hosts cannot launch ProcessContainer, record the environment
  blocker. The user explicitly authorized this stopping condition; a patched
  Windows 11 host is then needed for replacement acceptance and paired timings.

## Failure modes to preserve

The replacement must not widen reads/writes through ambient package ACLs,
hardlinks, reparse points, protected metadata, runtime paths, inherited handles
or environment credentials. Network deny must reject actual IPv4/IPv6 and UDP
connections; network allow needs a real external positive control. A probe alone
does not prove launch isolation. Setup failures must not execute or replay the
payload. Completion, cancellation and timeout must drain descendants before
scratch removal, without terminating another job. Core file mutations remain
outside the shell sandbox and retain their independent protections.

## Measurement policy

Measure actual successful commands separately from rejected launch attempts.
Retain first-command and repeated-command samples, tool/runtime versions, workspace
size, executable/archive sizes and resource observations. An unsupported launch
has no successful-work latency or memory result and cannot be called faster.
Do not extrapolate a direct MXC executable measurement to a finished Virgo adapter.

Maintenance accounting must distinguish removable launch/token/lease code from
shared file-tool and No-Limit process support. Report gross candidates separately
from net deletions after an accepted adapter exists.
