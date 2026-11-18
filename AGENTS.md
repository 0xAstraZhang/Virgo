# Virgo development

- Install the locked environment with `uv sync --locked --dev`. Run `uv run --no-sync ruff check`, `uv build` and `uv run --no-sync pytest -q`.
- Use the three fixed permission modes in [permission profiles](docs/en/development/permission-profiles.md). No-Limit runs directly on Host. Read-Only and Workspace-Edit load the platform Sandbox and share the real project directory with file tools.
- Exercise user selections, running and queued tasks, exact resource grants, and revocation through the CLI/TUI. Report the current and next task modes with `/permissions show`.
- On Linux and macOS, set `VIRGO_REQUIRE_SANDBOX_TESTS=1` and run `uv run --no-sync pytest -q tests/harness/test_sandbox_e2e.py`. On Windows use `tests/harness/test_windows_sandbox_e2e.py` with the same environment variable.
- Check the strict native jobs and complete cross-platform comparison at the submitted revision. Record the account, OS and actual network directions exercised. Keep refused launches visible in the results. See the [native guide](docs/en/guides/sandbox.md).
