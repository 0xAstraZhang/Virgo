# MXC Windows replacement evaluation

This is an isolated evaluation, not an enabled product backend. Do not merge it
or remove the existing Windows backend based on a successful capability probe.

## Result: blocked on the available hosts

On 2026-11-17, MXC 1.1.0 could not start native ProcessContainer on either of
Virgo's existing Windows CI hosts. Both returned
`Windows ProcessContainer is unavailable: PSEC is not enabled`, with
`baseContainerApiPresent: false`. This is a measured host capability limitation,
not a mocked rejection, an inferred version mismatch, or a successful sandbox
test. No product adapter was enabled after this failed prerequisite.

| Host | Image version | Existing Virgo E2E | MXC actual startup |
| --- | --- | --- | --- |
| Windows Server 2022 Datacenter, build 20348 | `20261112.366.7` | 25 passed, zero failures/errors/skips | 0/8 started; all refused with `backend_unavailable` |
| Windows Server 2025 Datacenter, build 26100 | `20261103.298.1` | 25 passed, zero failures/errors/skips | 0/8 started; all refused with `backend_unavailable` |

Each host ran four read-only/network flag combinations with both direct Python
and PowerShell + Python. All eight identical host positive controls exited zero
and created their expected markers. All eight MXC attempts exited `0xffffffff`
without creating a marker. Request-specific probes also reported missing PSEC.
The unconfigured `--probe` itself exits zero even when its JSON says unavailable;
its process exit code is not evidence of support.

Python was 3.13.17 x64 on both hosts; PowerShell was 7.6.10 on Server 2022 and
7.6.9 on Server 2025. The existing security suites took 274.284 and 297.207 seconds,
respectively. Their success does not establish MXC's security properties.

The [evaluation run](https://github.com/0xAstraZhang/Virgo/actions/runs/44041635772)
intentionally fails its final replacement gate. Raw JUnit, logs, probes, requests,
positive controls, timings and binary identity are preserved under
[the evidence manifest](evidence/mxc-windows-2026-11-17/manifest.json), which records
the exact baseline and harness SHA and a SHA-256 for each copied artifact file.

### Measured baseline overhead

The following are **Virgo LPAC baseline numbers only**, measured through
`run_command`, including sandbox setup, output spooling, PowerShell execution and
cleanup. The payload is `Write-Output 'benchmark-ok'`. Each group has one first
sample and five subsequent serial samples; the table reports the median of those
five. The machine had already run the E2E suite, so these are not cold-boot or
cold-filesystem measurements. All 48 baseline commands succeeded.

| Host | Ordinary workspace files | Read-only | Workspace-write |
| --- | ---: | ---: | ---: |
| Server 2022 | 0 | 4.804 s | 4.826 s |
| Server 2022 | 1,000 | 5.309 s | 5.435 s |
| Server 2025 | 0 | 5.372 s | 5.324 s |
| Server 2025 | 1,000 | 5.842 s | 5.774 s |

Broker-created output files also accumulate during the groups. This is a small
diagnostic sample, not a broad performance benchmark or an isolated measurement
of ACL traversal. MXC successful-command latency, peak memory and paired speedup
are **unmeasured** because no MXC workload started. Rejection times are retained
only as diagnostic data.

The upstream archive is 533,829,612 bytes; the selected x64 `wxc-exec.exe` is
12,700,472 bytes (about 12.11 MiB). The archive includes other platforms/backends
and is not the required product installation size; the executor size alone is
not a complete dependency-footprint claim. No new runtime dependency was added
to Virgo.

### Decision

Do not replace the existing Windows backend on the basis of this experiment.
Compatibility already fails on both currently validated hosts. Boundary
equivalence, ordinary-account compatibility, paired execution overhead and net
maintenance savings remain unverified. The measured product-code reduction is
**zero**; the static replacement surface is discussed below.

Continuing requires a Windows host with native PSEC that passes the exact MXC
request probes and real launch controls. Then implement the adapter and reuse the
existing E2E scenarios, documenting backend-specific contract differences. Per
the user's instruction, keep this branch/PR unmerged and record the blocker
instead of changing hosts, downgrading isolation, or broadening the experiment.

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

## Verified baseline maintenance inventory

Physical lines (including comments and blanks), counted at the frozen baseline:

| Module | Lines | Replacement implications |
| --- | ---: | --- |
| `src/virgo/tools/windows_sandbox.py` | 727 | Main candidate for replacement; policy validation, environment filtering and error semantics must still be supplied by the adapter or explicitly verified upstream. |
| `src/virgo/tools/windows_sandbox_acl.py` | 452 | Mixed responsibility. `AclGrants` occupies 182 lines (271–452) and is launch-specific, but Core staged replacement imports the DACL reader/preparer, inheritance helper and mutex. The whole module is not removable. |
| `src/virgo/tools/windows_sandbox_files.py` | 366 | Native path validation, no-reparse handles, file identity, staged replacement and output spooling remain relevant to Core tools outside MXC. |
| `src/virgo/tools/windows_process.py` | 355 | Also used by explicit No-Limit Shell execution. Replacing the constrained backend does not remove this process implementation. |
| Total | 1,900 | Not a removable-line estimate. |

The principal replacement surface is 727 + 182 = **909 gross lines**, before
retaining necessary validation, removing additional unused imports/helpers, or
adding an adapter and packaging. This is a review scope, not a proved net saving.
The current experiment removes **zero product lines**. A net reduction requires
an implemented adapter that passes native acceptance on a supported host.

The source dependency check is reproducible with:

```bash
wc -l src/virgo/tools/windows_sandbox.py src/virgo/tools/windows_sandbox_acl.py \
  src/virgo/tools/windows_sandbox_files.py src/virgo/tools/windows_process.py
rg -n 'windows_sandbox_acl|windows_process import' src/virgo
```

## Existing acceptance coverage

The unchanged Windows suite collects **25 parameter-expanded cases**. It covers
private-host read/write denial, ambient package ACLs, read-only children,
protected metadata and runtimes, junctions/hardlinks, credentials/handles,
process access, offline IPv4/IPv6/UDP, real permitted DNS/TCP, unsupported policy
refusal, exit/timeout/cancellation cleanup, concurrent jobs, Core mutation races,
originating workspace, actual launch failure and resource release.

Some assertions intentionally describe the current implementation: LPAC token
identity, device ACL leases, exact failure codes and the interleaving with
`AclGrants._update`. A future MXC comparison must identify these differences and
preserve their observable safety properties. Deleting them or treating them as
capability skips would not establish equivalent isolation.

## Upstream compatibility prerequisites

The pinned [MXC Windows support table](https://github.com/microsoft/mxc/blob/v1.1.0/docs/backends/process-container/os-version-support.md)
lists patched Windows 11 releases; it does not certify the existing Server 2022
or Server 2025 runners. Native runtime probes and real launch results decide
this experiment. The [1.1.0 release](https://github.com/microsoft/mxc/releases/tag/v1.1.0)
removed legacy filesystem-broker/host-permission fallback and requires native
PSEC for ProcessContainer. Do not enable a fallback to preserve apparent support.

MXC's bundled Windows release also documents Required diagnostic telemetry.
An eventual redistributed native build would need an explicit packaging decision;
the pinned binary used here is upstream's unmodified release. See
[upstream telemetry documentation](https://github.com/microsoft/mxc/blob/v1.1.0/docs/telemetry.md).
