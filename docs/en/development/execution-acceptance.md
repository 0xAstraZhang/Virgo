# Native execution acceptance

Install the locked dependencies with `uv sync --locked --dev`.

## Tier 1 platforms (Linux, macOS, native Windows, and Windows + WSL2)

### Linux and macOS

On Linux or macOS, run the required native sandbox and policy acceptance suites:

```sh
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q \
  tests/harness/test_sandbox_e2e.py \
  tests/harness/test_policy_v2.py \
  tests/harness/test_safety_v2.py \
  tests/harness/test_output_store.py \
  --junitxml=native-sandbox.xml
```

Verify realistic-scale cold-start (`<= 5s`) and constant per-`Read` overhead budgets (S5-04 / VER-11):

```sh
uv run --no-sync python ci/measure_permission_modes.py \
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 \
  --realistic-files 1000 --check-budgets
```

### Native Windows (Standard AppContainer)

Native Windows runs the Standard AppContainer backend (`windows-appcontainer`) unelevated without Administrator privileges on updated Windows 11 (`null.sys 10.0.26100.8972+` / WIL Feature `60692039`, where `\Device\Null` natively grants `S-1-15-2-1` `0x001201BF`) and is a **PR-blocking Tier 1 gate**:
- Stock GitHub Actions Windows Server 2022/2025 runners ship an older `null.sys` without Feature `60692039`. Production Virgo **never** mutates `\Device\Null` (unsupported Windows hosts default to **No-Limit** with WSL2 / No-Limit / Update Windows guidance and fail closed on explicit sandbox modes).
- `windows-sandbox.yml` (`Windows sandbox boundaries`) runs on `pull_request`, `push` to `main`, daily `schedule`, and `workflow_dispatch` on `windows-2022` (native) and `windows-2025` (`standard-user`). On both runners, CI first verifies the stock unsupported-Windows fallback (`ci/prepare_windows_null_device.py --verify-unsupported`), then aligns the CI Server runner's `\Device\Null` DACL with Microsoft's `null.sys 10.0.26100.9818` ACEs (`ci/prepare_windows_null_device.py`) so the full Standard AppContainer isolation suite—including `NUL`/`subprocess.DEVNULL` and the pure `*Users`-only `virgo_std` Standard User job (`ci/run_as_standard_user.py --assert-standard-user`)—runs end to end with zero skips. Reports are validated by `ci/check_native_report.py`.
- `pr-platform-comparison.yml` runs the single-revision `head` cross-platform pytest suite across `ubuntu-latest`, `windows-latest` (`windows-2025`, aligning the Server runner's `\Device\Null` via `ci/prepare_windows_null_device.py` with no sandbox exclusion), and `macos-latest`.
- `permission-timing.yml` aligns `\Device\Null` on `windows-latest` and runs `ci/measure_permission_modes.py --check-budgets` across `no-limit`, `read-only`, and `workspace-edit`.

To run native Windows acceptance manually in an unelevated PowerShell session (using a user-owned or AAP-readable native x64 Python 3.11+):

```powershell
$env:VIRGO_REQUIRE_SANDBOX_TESTS = "1"
uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
uv run --no-sync python ci/check_native_report.py windows-sandbox.xml

uv run --no-sync pytest -q `
  tests/harness/test_policy_v2.py `
  tests/harness/test_safety_v2.py `
  tests/harness/test_output_store.py

uv run --no-sync python ci/measure_permission_modes.py `
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 `
  --realistic-files 1000 --check-budgets
```

Record the OS build and account class (pure Standard User vs UAC-filtered unelevated desktop token vs Administrator) with any native Windows observations.

### Windows + WSL2 (Ubuntu)

Run the bundled acceptance script from a checkout inside the WSL2 Linux filesystem (not `/mnt/c`):

```sh
./ci/wsl2_acceptance.sh
```

Each Tier 1 platform must execute its native suite with zero failures, errors, or skips. Check the JUnit reports, cross-platform CI summary (without SHA-bound retirement files; declare retired tests and replacements in the PR description), and performance budgets for the same candidate commit.

See also: [Execution boundaries](../guides/sandbox.md) · [Native Windows sandbox](../guides/windows-sandbox.md) · [WSL2 setup](../guides/wsl2.md) · [Permission timing protocol](permission-timing.md)
