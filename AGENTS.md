# Development

- Install locked dependencies: `uv sync --locked --dev`.
- Lint changed Python files with `uv run --no-sync ruff check <paths>`.
- Run affected existing tests with `uv run --no-sync pytest -q <paths>`. Prefer assembled-session or real-process acceptance for execution changes.
- Native acceptance must run on real Linux/macOS/Windows hosts with `VIRGO_REQUIRE_SANDBOX_TESTS=1`; see `docs/en/development/execution-acceptance.md`. A skipped suite or a policy-generation check does not establish isolation.
- Keep execution-boundary documentation in English and Chinese consistent with implemented behavior. Do not claim native guarantees from Broker-only fixtures.
- Preserve startup acknowledgement, cancellation, process-tree cleanup and fail-closed behavior when changing execution. Never replay a possibly started payload automatically.
