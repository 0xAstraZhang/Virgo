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

Native Windows runs the Standard AppContainer backend (`windows-appcontainer`) unelevated without Administrator privileges and is a **PR-blocking Tier 1 gate**:
- `windows-sandbox.yml` (`Windows sandbox boundaries`) runs on `pull_request`, `push` to `main`, daily `schedule`, and `workflow_dispatch` across `windows-2022` and `windows-2025`, plus a dedicated `standard-user` job on `windows-2025` that creates a pure `*Users`-only Standard User account (`virgo_std`, not in `Administrators`) and runs `tests/harness/test_windows_sandbox_e2e.py` via `ci/run_as_standard_user.py` (`--assert-standard-user` verifies `IsUserAnAdmin() == False`, `TokenElevation == 0`, `TokenElevationType == 1`, and `S-1-5-32-544` absent). Reports are validated by `ci/check_native_report.py`.
- `pr-platform-comparison.yml` runs the complete Windows pytest suite with no sandbox exclusion.
- `permission-timing.yml` runs `ci/measure_permission_modes.py --check-budgets` on `windows-latest` across `no-limit`, `read-only`, and `workspace-edit`.

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

Each Tier 1 platform must execute its native suite with zero failures, errors, or skips. Check the JUnit reports, cross-platform comparison (without SHA-bound retirement files; declare retired tests and replacements in the PR description), and performance budgets for the same candidate commit.

See also: [Execution boundaries](../guides/sandbox.md) · [Native Windows sandbox](../guides/windows-sandbox.md) · [WSL2 setup](../guides/wsl2.md) · [Permission timing protocol](permission-timing.md)
