# Windows execution status

Host is the default. Host commands retain the Windows Job Object lifecycle implementation; it is not an isolation backend.

The old LPAC token/DACL isolation implementation has been removed. Explicit Sandbox selection uses only the pinned MXC 1.1.0 `wxc-exec.exe` with `containment=processcontainer`, least privilege, no DACL fallback and no permissive audit mode. Virgo does not download it automatically or switch to Host on failure.

**MXC Windows replacement is not accepted.** The earlier evaluation on Windows Server 2022 and 2025 could not start ProcessContainer because PSEC was unavailable. Probe availability and mocked denial are not startup evidence. The shared-host-path alias contract also remains unsupported by Virgo's adapter, so native payload admission fails closed.

Windows file tools retain separate no-reparse handle traversal and fresh-file replacement with permission preservation. These broker protections do not prove native Shell containment. Required Windows acceptance must run on a real supported Windows host with successful startup, allowed operations, denied operations, and zero failures, errors or skips:

```powershell
$env:VIRGO_MXC_EXECUTABLE = 'C:\installed\wxc-exec.exe'
$env:VIRGO_REQUIRE_SANDBOX_TESTS = '1'
uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
```

The required gate remains blocking until those conditions are met. See [acceptance evidence](../development/execution-acceptance.md) and [migration](migrating-execution-policy.md).
