# Execution acceptance and remaining blockers

The product contract is [Host by default with explicit resource boundaries](../guides/sandbox.md). This record separates implementation, component checks and native evidence. It is not a claim that every acceptance condition has passed.

## Current conclusions

| Area | Evidence / status |
| --- | --- |
| Host/Core | Fresh-process tests perform real file reads/writes, complex Shell syntax and an ordinary child while rejecting any Sandbox import. No permission request tool, review record or approval is created. Source-context, grant, UI and failure-fact checks are separate from native isolation. |
| Broker files | Existing POSIX file mutation tests retain encoding, permission preservation, failed edits, pinned path replacement, hardlink rejection and concurrent unlink coverage. Windows uses its own handle implementation; the retained Windows 2022/2025 reports pass three NTFS broker alias cases. Native process acceptance remains separate and blocked. |
| macOS native MXC | **Not passed.** The verified ARM64 1.1.0 executor starts and performs an allowed write, but a host-created hardlink after launch allows the payload to change a protected outside file. The [raw report](../../evidence/execution/macos-mxc-shared-path-probe.json) records `launch_succeeded=true`, `allowed_write_succeeded=true`, `alias_isolated=false`. |
| Linux native MXC | **Not accepted.** Shared-host-path policies are refused by the product until their alias/race contract can be enforced. No local Linux isolation result is claimed. |
| Windows native MXC | **Not accepted.** Prior Windows 2022/2025 evaluation could not start ProcessContainer without PSEC. That remains independent of the unsupported shared-path contract. The removed LPAC backend is not a fallback. |
| Strict macOS product gate | Required local run: 36 passed, 32 setup errors, zero skips. The errors are unsupported native policy admission, not successful negative isolation tests. |
| Prompt effect | User-selected Codex `gpt-6.1-sol`, medium effort: [3/3 tasks correct, zero approvals, no observed intent violations or boundary attempts](../../evidence/execution/instruction-summary.json). Full [calls, outputs, time and token records](../../evidence/execution/macos-codex-instructions.json) are retained. This small Host task set is not native isolation, general model-quality or paired performance evidence; the paused Virgo/Codex evaluation was not resumed. |
| Performance | Raw [Host](../../evidence/execution/macos-host-samples.json) and [Sandbox](../../evidence/execution/macos-sandbox-samples.json) samples are retained. Failed operations have no successful latency. Host and Sandbox have different authority; these samples cannot support a same-permission backend comparison or a performance improvement claim. |
| Maintenance | [Inventory](../../evidence/execution/maintenance.json) separates exact moved lines, retained protection, removed/rewritten lines, new modules and external dependencies. Movement alone does not establish a maintenance reduction. |

Local worktree artifacts identify their exact source file digests and workload. They are not a substitute for latest-PR-head lint, distribution build, full-suite and cross-platform results. Consult each PR's checks for revision-bound evidence. Required native gates remain failures until real launch, positive controls and forbidden operations all pass with zero skips.

## Pinned cross-platform native reports

The retained [raw reports and archive digests](../../evidence/execution/native-60df626/summary.json) test behavior revision `60df62697f98949ead0c92669098ec84136e2d4e`. Linux/macOS each record 36 passing broker/protocol cases and 32 native admission errors, with no skips. Windows 2022/2025 each record 3 passing NTFS broker alias cases and 13 native admission errors, with no skips. None of these totals is a native isolation pass.

The upstream probes are independent: macOS 26.7.2 starts and reproduces the alias escape; Linux rejects `network.ingress.default=allow` as unsupported; both Windows hosts report `backend_unavailable` because PSEC is not enabled. Refusal to admit the shared-path product policy remains an additional barrier on all platforms. This report does not change or expand the requested network policy to obtain a successful launch.

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

## Measurement and instruction protocol

The [earlier revision samples](../../evidence/execution/945bdfa/) remain separate; do not combine different source revisions. Run providers sequentially on an otherwise idle host. Keep the full JSON rather than selecting a best run:

```bash
uv run --no-sync python ci/measure_startup.py --samples 5 --repeat 5 --output host.json
uv run --no-sync python ci/measure_startup.py --execution sandbox --executor /absolute/path/to/executor --samples 5 --repeat 5 --output sandbox.json
uv run --no-sync python ci/measure_maintenance.py --baseline 4c8520fb3faa262e5f502ff808cd5fd4350bf9bd --output maintenance.json
```

Samples include import/construction time, first/repeated execution, process peak memory and the installed interpreter/environment/package/executor footprint. Failures remain samples, with separate elapsed-failure fields. The installed development environment footprint is not a minimal production dependency estimate. Sources, lockfile, tool script and platform are identified; collecting on another version requires a separate report.

The instruction manifest freezes model, effort, task, initial files and authority. The runner preserves all calls, files, answers, approvals, token usage and time. File-state correctness and directly observable intent violations are scored; Shell intent still requires review of retained calls, not a command classifier. No prompts or permissions may be changed between compared runs. This evidence remains separate from tests that deliberately violate task instructions through tool calls.

The prior [Anthropic attempt](../../evidence/execution/macos-instruction-attempt.json) stopped for missing credentials. The user then selected the existing Codex login; the new manifest changes the model while retaining tasks, medium effort and Host authority. Run it explicitly with `uv run --no-sync python ci/evaluate_instructions.py --auth-file /absolute/path/to/user/auth.json --output instructions.json`. Credentials stay outside results. These are separate attempts, not a comparison between models.
