# Virgo development

- Install the locked development environment with `uv sync --locked --dev`.
- Run `uv run --no-sync ruff check` for lint and `uv build` for distribution builds.
- Select affected existing end-to-end scenarios for local iteration; the complete suite is `uv run --no-sync pytest -q`.
- Native POSIX isolation uses `VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q tests/harness/test_sandbox_e2e.py` on a supported Linux/macOS host.
- Native Windows isolation uses `uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml` on Windows. The dedicated workflow requires executed cases with zero failures, errors or skips; a Linux skip or mocked rejection is not isolation evidence.
- Before changing a security boundary, describe its failure modes and define any necessary end-to-end reproduction. Keep approvals, Plan instructions and OS permissions separate; unavailable boundaries fail closed.
- Inspect `/permissions show` during acceptance: configured and effective boundaries must be distinct, and backend discovery is not proof of a successful native launch. Host execution requires explicit `--permission no-limit`, a user preset, or an exact approved operation exception. See [permission profiles](docs/en/development/permission-profiles.md).
- Check the latest PR head's native workflows and complete cross-platform comparison. Individual suite jobs can succeed while the comparison gate reports regressions.
- See [development and CI](docs/en/development/testing-and-ci.md) for CI commands and report policy, and [native command sandbox](docs/en/guides/sandbox.md) for platform boundaries.
