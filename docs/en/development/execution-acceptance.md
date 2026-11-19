# Native execution acceptance

Install the locked dependencies with `uv sync --locked --dev`. On a supported Linux or macOS host run:

```sh
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q tests/harness/test_sandbox_e2e.py --junitxml=native-sandbox.xml
```

On Windows Server 2022 or 2025 run in PowerShell:

```powershell
$env:VIRGO_REQUIRE_SANDBOX_TESTS = "1"
uv run --no-sync pytest -q tests/harness/test_permission_modes_e2e.py::test_file_approval_requires_explicit_request_and_preserves_the_mode tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
```

Each platform must execute its native suite with zero failures, errors or skips. Check the JUnit reports and full cross-platform comparison for the same candidate commit. Verify that running commands see saved project edits, that mode changes preserve active task permissions, and that exact grants stop applying after revocation. Record the OS and account class with the results. On Windows record outbound, inbound and host-loopback observations separately. [POSIX live-workspace contract](../guides/sandbox.md) · [Windows setup and known limits](../guides/windows-sandbox.md)

The [permission timing protocol](permission-timing.md) compares all three modes to the pinned main revision and records the number of approval prompts.

## Complete Windows comparison

The cross-platform workflow runs each Windows revision on three separate runners:

- `windows-core` keeps the existing reviewed parallel groups and serial remainder, excluding the files assigned to the other two partitions.
- `windows-permissions` runs three complete modules in their original order, in one serial pytest process: `tests/harness/test_auth_commands.py`, `tests/harness/test_permission_modes_e2e.py`, then `tests/harness/test_sandbox_e2e.py`.
- `windows-native` continues to run the entire `tests/harness/test_windows_sandbox_e2e.py` module serially on its own host.

Core can still include native calls, such as the existing TUI approval case in its reviewed parallel group; their execution arrangement is unchanged. No additional ACL tests run concurrently on one Windows host, and no test is dropped or reordered within its module. Linux and macOS keep their existing execution plan. The Windows/macOS/Linux job budgets remain 90/40/20 minutes.

All three Windows partitions must finish and agree on platform, revision label, SHA, Python and system identity before their outcomes are combined. Missing, incomplete, duplicated or overlapping reports fail the comparison. The head native partition must execute with zero failures, errors or skips. Platform-specific skips in the other partitions retain the existing baseline/head comparison rules. A baseline that completes with test failures retains its outcomes so the existing comparison can classify existing and fixed failures; it is not treated as a missing partition. The merged full inventory still passes through the original retirement, newly skipped test and removed identity checks.

The `results-windows-latest-<revision>-core`, `-permissions` and `-native` artifacts retain each runner's raw JUnit, logs and normalized outcomes. The comparison artifact also contains `comparison-windows-latest-<revision>.json`, including every combined test outcome and each partition's duration, completeness and exit code. Windows seconds in the summary are the sum of all three runner execution durations, not elapsed workflow time or a product performance measurement. An incomplete rerun cannot reuse a previous complete aggregate.

`python ci/checks/parallel.py` exercises the partition and merge contract with real pytest subprocesses, including preservation of failures and rejection of incomplete input. Those small CI fixtures do not substitute for native platform acceptance above.
