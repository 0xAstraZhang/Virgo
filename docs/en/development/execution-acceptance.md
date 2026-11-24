# Native execution and performance acceptance

[Documentation](../README.md) · [Project home](../../../README.md) · [简体中文](../../zh-CN/development/execution-acceptance.md)

Install locked dependencies with `uv sync --locked --dev`. Each Tier 1 platform (Linux, macOS, native Windows, and Windows + WSL2) must execute its native isolation suite with zero failures, errors, or skips, and pass realistic-scale performance budgets for the same candidate commit.

## Native isolation suites

### Linux and macOS

Run the required native sandbox suite and validate its JUnit report (matching `.github/workflows/native-sandbox.yml`), then run the cross-platform policy suites on a real Linux or macOS host:

```sh
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q \
  tests/harness/test_sandbox_e2e.py \
  --junitxml=native-sandbox.xml
uv run --no-sync python ci/check_native_report.py native-sandbox.xml

uv run --no-sync pytest -q \
  tests/harness/test_policy_v2.py \
  tests/harness/test_safety_v2.py \
  tests/harness/test_output_store.py
```

### Native Windows (Standard AppContainer)

Native Windows runs the unelevated Standard AppContainer backend (`windows-appcontainer`) on updated Windows 11 (`null.sys 10.0.26100.8972+` / WIL Feature `60692039`, where `\Device\Null` grants `S-1-15-2-1` `0x001201BF`) and is a **PR-blocking Tier 1 gate**:

- Stock GitHub Actions Windows Server 2022/2025 runners ship an older `null.sys` without Feature `60692039`. Production Virgo **never** mutates `\Device\Null` (unsupported hosts default to **No-Limit** with guidance and fail closed on explicit sandbox modes).
- `windows-sandbox.yml` (`Windows sandbox boundaries`) runs on `pull_request`, `push` to `main`, daily `schedule`, and `workflow_dispatch` on `windows-2022` (native) and `windows-2025` (`standard-user`). Both runners first verify stock unsupported-Windows fallback (`ci/prepare_windows_null_device.py --verify-unsupported`), then align the CI runner's `\Device\Null` DACL (`ci/prepare_windows_null_device.py`) so the full Standard AppContainer isolation suite—including `NUL`/`subprocess.DEVNULL` and the pure `*Users`-only `virgo_std` Standard User job (`ci/run_as_standard_user.py --assert-standard-user`)—runs end to end with zero skips. Reports are validated by `ci/check_native_report.py`.
- `pr-platform-comparison.yml` runs the single-revision `head` cross-platform pytest suite across `ubuntu-latest`, `windows-latest` (`windows-2025`, aligning `\Device\Null` via `ci/prepare_windows_null_device.py` with no sandbox exclusion), and `macos-latest`.

To run native Windows acceptance manually in an unelevated PowerShell session (using a user-owned or AAP-readable native x64 Python 3.11+):

```powershell
$env:VIRGO_REQUIRE_SANDBOX_TESTS = "1"
uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
uv run --no-sync python ci/check_native_report.py windows-sandbox.xml

uv run --no-sync pytest -q `
  tests/harness/test_policy_v2.py `
  tests/harness/test_safety_v2.py `
  tests/harness/test_output_store.py
```

Record the OS build and account class (pure Standard User vs UAC-filtered unelevated desktop token vs Administrator) with any native Windows observations.

### Windows + WSL2 (Ubuntu)

Run the bundled acceptance script from a checkout inside the WSL2 Linux filesystem (not `/mnt/c`):

```sh
./ci/wsl2_acceptance.sh
```

This verifies `bubblewrap`, executes the required Linux native sandbox tests (`VIRGO_REQUIRE_SANDBOX_TESTS=1`), validates the JUnit report with `ci/check_native_report.py`, and checks **Workspace-Edit** performance budgets.

## Performance budgets

The `Permission mode performance` workflow (`.github/workflows/permission-timing.yml`) runs on Linux (`ubuntu-latest`), macOS (`macos-latest`), and native Windows (`windows-latest`, after aligning `\Device\Null` via `ci/prepare_windows_null_device.py`) across `no-limit`, `read-only`, and `workspace-edit`.

On all three platforms, `--check-budgets` compares a small workspace (`0` extra files) against a realistic workspace (`1000` files) using a local scripted model driving Virgo's real tools with zero external network calls and zero approval prompts:

- **Cold start & first Shell command**: `<= 5s`
- **Repeated short Shell command**: `<= 2s` and bounded O(1) scaling across workspace sizes
- **Broker `read` latency**: `<= 250ms` and O(1) scaling across workspace sizes

Each sample launches a fresh Python interpreter with an isolated user directory and workspace, recording import time, session construction time, `read` latency, first Shell command latency, repeated short Shell command latency, and edit-to-test latency. Failures retain diagnostics, and unconfirmed worker cleanup stops remaining samples immediately.

Run the budget check locally:

```sh
uv run --no-sync python ci/measure_permission_modes.py \
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 \
  --realistic-files 1000 --check-budgets --output budget.json
```

Native process and network boundary guarantees are enforced separately by `native-sandbox.yml`, `windows-sandbox.yml`, and `ci/wsl2_acceptance.sh`. When retiring tests during a refactor, declare every removed test and its replacement coverage in the commit message and PR description.

See also: [Execution boundaries](../guides/sandbox.md) · [Native Windows sandbox](../guides/windows-sandbox.md) · [WSL2 setup](../guides/wsl2.md) · [Execution interface](execution-interface.md)
