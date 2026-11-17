# Virgo development

- Install the locked development environment with `uv sync --locked --dev`.
- Run `uv run --no-sync ruff check` for lint and `uv build` for distribution builds.
- Select affected existing end-to-end scenarios for local iteration; the complete suite is `uv run --no-sync pytest -q`.
- Required native POSIX acceptance sets `VIRGO_MXC_EXECUTABLE` to an explicitly installed MXC executor and uses `VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q tests/harness/test_sandbox_e2e.py` on a supported Linux/macOS host.
- Required native Windows acceptance sets `VIRGO_MXC_EXECUTABLE` and `VIRGO_REQUIRE_SANDBOX_TESTS=1`, then uses `uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml` on Windows. The dedicated workflow requires executed cases with zero failures, errors or skips; a Linux skip or mocked rejection is not isolation evidence.
- Before changing a security boundary, describe its failure modes and define any necessary end-to-end reproduction. Keep approvals, task instructions and OS permissions separate; unavailable boundaries fail closed.
- Inspect `/permissions show` during acceptance: configured and effective boundaries must be distinct, and backend discovery is not proof of a successful native launch. Host execution is the fresh-configuration default: no Sandbox imports/probes/services or ordinary command review/approval. Sandbox is explicit, MXC-only, and fails closed; operation grants add resources without switching to Host. See [permission profiles](docs/en/development/permission-profiles.md).
- Check the latest PR head's native workflows and complete cross-platform comparison. Individual suite jobs can succeed while the comparison gate reports regressions.
- See [development and CI](docs/en/development/testing-and-ci.md) for CI commands and report policy, and [native command sandbox](docs/en/guides/sandbox.md) for platform boundaries.

- Current native MXC acceptance is blocked by the shared-path alias contract; Windows also lacks PSEC startup evidence. Keep failures visible and separate from Host/Core conclusions. See [execution acceptance](docs/en/development/execution-acceptance.md).
