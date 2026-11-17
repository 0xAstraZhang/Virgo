# Execution acceptance and remaining blockers

The product contract is [Host by default with explicit resource boundaries](../guides/sandbox.md). This record separates implementation, component checks and native evidence. It is not a claim that every acceptance condition has passed.

## Current conclusions

| Area | Evidence / status |
| --- | --- |
| Host/Core | Fresh-process tests perform real file reads/writes, complex Shell syntax and an ordinary child while rejecting any Sandbox import. No permission request tool, review record or approval is created. Source-context, grant, UI and failure-fact checks are separate from native isolation. |
| Broker files | Existing POSIX file mutation tests retain encoding, permission preservation, failed edits, pinned path replacement, hardlink rejection and concurrent unlink coverage. Windows uses its own handle implementation and still needs real Windows execution evidence. |
| macOS native MXC | **Not passed.** The verified ARM64 1.1.0 executor starts and performs an allowed write, but a host-created hardlink after launch allows the payload to change a protected outside file. The [raw report](../../evidence/execution/macos-mxc-shared-path-probe.json) records `launch_succeeded=true`, `allowed_write_succeeded=true`, `alias_isolated=false`. |
| Linux native MXC | **Not accepted.** Shared-host-path policies are refused by the product until their alias/race contract can be enforced. No local Linux isolation result is claimed. |
| Windows native MXC | **Not accepted.** Prior Windows 2022/2025 evaluation could not start ProcessContainer without PSEC. That remains independent of the unsupported shared-path contract. The removed LPAC backend is not a fallback. |
| Strict macOS product gate | Required local run: 36 passed, 32 setup errors, zero skips. The errors are unsupported native policy admission, not successful negative isolation tests. |

Local reproduction artifacts are not a substitute for latest-PR-head lint, distribution build, full-suite and cross-platform results. Consult each PR's checks for revision-bound evidence. Required native gates remain failures until real launch, positive controls and forbidden operations all pass with zero skips.

## Reproducing the native blocker

Use the official standalone [MXC v1.1.0 release](https://github.com/microsoft/mxc/releases/tag/v1.1.0). The retained probe creates only disposable resources; it invokes the adapter directly to investigate upstream behavior, bypassing the product's explicit unsupported-policy guard. This is a diagnostic path, not a user execution fallback.

```bash
uv run --no-sync python ci/probe_mxc.py --executor /absolute/path/to/mxc-exec-mac --output mxc-probe.json
```

The archive SHA-256 is `31a44963e288e3af23bc2e7e350608ed28f7c76b922ee7a7f1758a486dda37b3`; platform executor digests are pinned in `virgo.extensions.sandbox.mxc`. The CI-only installer downloads and verifies this release explicitly. Application startup never installs or updates an executor. [MXC's filesystem policy](https://github.com/microsoft/mxc/blob/v1.1.0/docs/backends/bwrap/bubblewrap-backend.md#filesystem-policy) is not a proof against arbitrary aliases or host-side races.

Virgo refuses native shared-path policies before releasing the payload. A startup scan, model compliance, wholesale network failure or an unverified launch cannot close this acceptance gap. File-level grants must not expand to directories. No private-workspace copy/writeback scheme or Host fallback has been introduced.

## Lifecycle and result interpretation

macOS follows [MXC's best-effort cleanup contract](https://github.com/microsoft/mxc/blob/v1.1.0/docs/backends/seatbelt/seatbelt-backend.md#process-cleanup). Old coalition-specific guarantees are retired. Existing detached-child, double-fork and independent-task observations remain native tests; observing one exit does not establish a universal guarantee. Missing receipts keep payload/effects unknown, and cleanup failures retain diagnostics and cannot be turned into success by result hooks. Synthetic receipt tests are protocol evidence only.

`details.execution.started` means entry into the Python tool. `details.boundary.payload_started` reports payload evidence. Neither a selected preset nor a discovered/pinned backend proves an established native boundary. File access, managed network dispatch and external trusted activity are reported separately.
