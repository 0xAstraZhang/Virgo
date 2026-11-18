# Native execution acceptance

Install the locked dependencies with `uv sync --locked --dev`. On a supported Linux or macOS host run:

```sh
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q tests/harness/test_sandbox_e2e.py --junitxml=native-sandbox.xml
```

On Windows Server 2022 or 2025 run in PowerShell:

```powershell
$env:VIRGO_REQUIRE_SANDBOX_TESTS = "1"
uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
```

Each platform must execute its native suite with zero failures, errors or skips. Check the JUnit reports and full cross-platform comparison for the same candidate commit. Verify that running commands see saved project edits, that mode changes preserve active task permissions, and that exact grants stop applying after revocation. Record the OS and account class with the results. On Windows record outbound, inbound and host-loopback observations separately. [POSIX live-workspace contract](../guides/sandbox.md) · [Windows setup and known limits](../guides/windows-sandbox.md)
