# Native execution acceptance

Install the locked dependencies with `uv sync --locked --dev`.

## Tier 1 platforms (Linux, macOS, and Windows + WSL2)

On Linux or macOS, run the required native sandbox and policy acceptance suites:

```sh
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q \
  tests/harness/test_sandbox_e2e.py \
  tests/harness/test_policy_v2.py \
  tests/harness/test_safety_v2.py \
  tests/harness/test_output_store.py \
  --junitxml=native-sandbox.xml
```

Verify realistic-scale cold-start (<= 5s) and constant per-`Read` overhead budgets (S5-04 / VER-11):

```sh
uv run --no-sync python ci/measure_permission_modes.py \
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 \
  --realistic-files 1000 --check-budgets
```

On **Windows + WSL2 (Ubuntu)**, run the bundled acceptance script from a checkout inside the WSL2 Linux filesystem (not `/mnt/c`):

```sh
./ci/wsl2_acceptance.sh
```

Each Tier 1 platform must execute its native suite with zero failures, errors, or skips. Check the JUnit reports, cross-platform comparison (without SHA-bound retirement files; declare retired tests and replacements in the PR description), and performance budgets for the same candidate commit.

## Experimental native Windows (non-blocking)

Native Windows LPAC tests run on a daily scheduled workflow (`windows-sandbox.yml`) rather than blocking PRs. Non-sandbox tests run on a single blocking `windows-latest` runner in `pr-platform-comparison.yml`. To run the experimental native Windows LPAC suite manually on an Administrator Windows Server 2022/2025 host in PowerShell:

```powershell
$env:VIRGO_REQUIRE_SANDBOX_TESTS = "1"
uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
```

Record the OS and account class (Administrator vs non-administrator desktop account) with any native Windows observations.

See also: [Execution boundaries](../guides/sandbox.md) · [WSL2 setup](../guides/wsl2.md) · [Experimental native Windows sandbox](../guides/windows-sandbox.md) · [Permission timing protocol](permission-timing.md)
