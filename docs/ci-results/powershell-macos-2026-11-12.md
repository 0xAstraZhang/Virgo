# macOS PowerShell native crash, 2026-11-12

## Result and scope

The residual macOS startup failure reproduces even with empty, read-only startup
profiles. The native report identifies a null dereference in .NET's named-mutex
wait. PowerShell's cold-cache telemetry initialization uses such a mutex before
executing the requested command. The Unix CI wrapper now opts out of telemetry
before starting pytest and its shell children, avoiding that unnecessary path.
The existing startup-profile protection remains necessary for the separate
profile-corruption failure. No retry, skip, or application-shell change is added.

## Evidence

- [Original PR #25 shell job](https://github.com/0xAstraZhang/Virgo/actions/runs/43209153820/job/131151907795):
  PowerShell exits `-11`, with empty stdout/stderr, during the real concurrent
  cache check; rerunning the same revision passes.
- [Uninstrumented reproduction](https://github.com/0xAstraZhang/Virgo/actions/runs/43215924431/job/131175031966):
  the 17th cache-check invocation fails with the same `-11` and empty output.
  No .NET dump or JIT configuration was set in this control. The job retained
  `pwsh-2026-11-12-121637.ips` and printed its faulting thread in the log.
- Environment: macOS 26.7.2 (25G332), arm64 runner image
  `macos-26-arm64/20261102.0473.1`, PowerShell 7.6.9, .NET 10.0.14.
- Exception: `EXC_BAD_ACCESS / SIGSEGV`, invalid address `0x20`, main thread.
  The top frame is at `libcoreclr + 0x1b6430`; register `x0` is zero.
  The caller is `CorUnix::InternalWaitForMultipleObjectsEx + 536`, followed by
  `Thread::DoAppropriateWaitWorker`, `WaitHandle_WaitOneCore`, and a managed
  static initializer. Managed JIT frames in this report are not symbolized.

The official portable runtime has the same Mach-O UUID as the failing image:
`D9F3E561-474A-3EE4-A749-DEB50084B7F5`. Disassembly resolves the fault to
`SharedMemoryProcessDataHeader::GetData()`:

```text
0x1b6430: ldr x0, [x0, #0x20]   # x0 == 0; reads address 0x20
0x1b6434: ret
```

The caller matches the [named-mutex branch in .NET 10.0.14 wait.cpp](https://github.com/dotnet/runtime/blob/v10.0.14/src/coreclr/pal/src/synchmgr/wait.cpp#L434-L442).
It obtains a `SharedMemoryProcessDataHeader*` and dereferences it after a
debug-only assertion. Thus this captured failure is a native null pointer,
rather than a Python assertion, command timeout, or inferred corrupt profile.

## Why the CI environment triggers it

[PowerShell 7.6.9 telemetry initialization](https://github.com/PowerShell/PowerShell/blob/v7.6.9/src/System.Management.Automation/utils/Telemetry.cs#L178-L195)
checks `POWERSHELL_TELEMETRY_OPTOUT` before initializing telemetry.
When `telemetry.uuid` is absent,
[`GetUniqueIdentifier()`](https://github.com/PowerShell/PowerShell/blob/v7.6.9/src/System.Management.Automation/utils/Telemetry.cs#L998-L1032)
creates the named mutex `CreateUniqueUserId` and calls `WaitOne()`.
The cache wrapper deliberately creates a fresh writable directory for each
pytest invocation. Up to eight simultaneous shells consequently enter this
cold-start path; protecting `StartupProfileData-*` does not protect telemetry.
Thread/process timing changes between runs, explaining why an unchanged retry
can succeed.

PowerShell also has an [upstream fix for incorrect telemetry mutex ownership](https://github.com/PowerShell/PowerShell/pull/28026).
That fix describes blocking and abandonment; it is not proof of the exact cause
of this native null pointer. The crash report does not establish which lower-level
runtime creation/lifetime race produced the null header. The faulting native
operation is directly observed; attribution to telemetry uses the startup source,
the cold-cache trigger, and the telemetry-disabled control. This CI mitigation
does not claim to repair .NET's general named-mutex implementation or diagnose
every older PowerShell crash without a retained report.

## Test-first validation

The real-process acceptance check was extended before changing the wrapper.
It explicitly opts the parent into telemetry, then launches PowerShell through
both comparison labels. Each Unix shell must report `CanSendTelemetry == false`,
execute the requested sentinel command, and leave no `telemetry.uuid` file.
The old wrapper fails with `True`; the fixed wrapper passes all 48 launches,
with up to eight concurrent processes. Existing checks also verify cache
permissions, writable command caches, unchanged HOME/parent environment,
temporary-directory cleanup, and propagation of test/collection failures.

Local `ci/check_comparison.py` and Ruff pass. The native stress control is
[recorded separately](https://github.com/0xAstraZhang/Virgo/actions/runs/43217028136):
two macOS runners each repeat the original 48-launch acceptance check 60 times,
with telemetry disabled and no dump/JIT overrides. Ordinary PR CI additionally
runs the acceptance check and shell tests on Ubuntu, Windows, and macOS.

To rerun locally, install real PowerShell 7 on PATH and run
`python ci/check_powershell_cache.py` in the project's Python environment.
Run it as an unprivileged user so the read-only profile protection is effective.
