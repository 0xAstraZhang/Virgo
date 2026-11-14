# Development and CI

[Documentation](../README.md) · [简体中文](../../zh-CN/development/testing-and-ci.md)

## Local workflow

Run commands from the repository root:

```bash
uv sync --locked --dev
uv run --no-sync ruff check
uv run --no-sync pytest -q
uv build
```

For focused iteration, select the affected existing scenarios. Real POSIX pseudo-terminal tests use the `pty` marker; `uv run --no-sync pytest -q -m "not pty"` excludes them. PowerShell scenarios require the relevant interpreter and skip when it is unavailable. A local pass does not establish Windows or macOS behavior.

Prefer end-to-end coverage of observable behavior. Add a new scenario only when an important failure mode needs coverage, and describe the failure modes before implementation. Do not add tests merely to restate the code. The existing suite uses the faux provider, local HTTP services, isolated settings and terminal drivers; ordinary validation does not require real model credentials.

Tests mirror source responsibilities under `tests/ai`, `agent`, `harness`, `features`, `tools` and `cli`. Shared fixtures and doubles live in `tests/support/`; test modules should not import each other. Use the supplied `python_command` helper for cross-platform shell quoting, temporary directories for writes, and the exact checkout's source path when a child process runs outside that checkout. Keep persistent user settings and external catalog refreshes out of offline scenarios.

Retain cases that protect distinct behavior, failure modes or integration boundaries. Remove a weaker duplicate only when existing retained scenarios exercise its behavior, never merely because it is small or tests one module. The [reviewed retirement record](test-retirements.md) lists the nine removed duplicates, their retained coverage and the CI conditions for recognizing the deletion.

## Automatic checks

[Cross-platform CI](../../../.github/workflows/pr-platform-comparison.yml) runs on pull requests and pushes to `main`. It resolves immutable event revisions, runs lint/workflow/CI acceptance checks, executes product suites where needed, and publishes a final comparison check.

| Trigger | Product validation |
| --- | --- |
| Pull request | Compare the event's exact base and head commits on Linux, Windows and macOS; reject new failures, unapproved test removals and newly skipped baseline tests |
| Push to `main` | Validate the pushed head strictly on all three platforms |
| Manual dispatch | Resolve the requested PR once and run the full comparison |
| Only approved Markdown documentation paths changed | Run lightweight checks and report the full-suite skip through the final check |

The documentation-only classifier includes the two root READMEs and Markdown files under `docs/`. Rename detection is disabled for classification, so moving code into docs still considers both paths. Missing history, failed diffs, empty changes or other files conservatively require full tests. The final comparison check still runs; missing or failed prerequisites cannot become a successful documentation-only result.

CI currently uses Python 3.13 with locked dependencies. Python 3.11 remains the declared package minimum. Actions are pinned to commit SHAs; exact tool versions, timeouts and artifact retention are maintained in the workflows. Linux head jobs build the sdist/wheel and install the wheel outside the checkout before checking the CLI, so package-data omissions are visible.

## Read the result

The final summary and `comparison` artifact show new failures, existing failures, fixes, changed skips, and added/missing cases. Each `results-<platform>-<revision>` artifact contains logs, JUnit XML, normalized outcomes, interpreter data and the tested SHA. Failure details include the case, source location and traceback; locals are not dumped.

PR comparison distinguishes a regression against a passing baseline from a newly exposed failure whose baseline was skipped or absent. Both fail the check. An already failing baseline case remains visible but does not alone fail the PR comparison. Main's strict single-revision mode fails on every failure/error.

Unapproved missing executed baseline tests, newly skipped previously executed tests, collection errors, incomplete runs and malformed/mismatched artifacts fail validation. Test identity uses function/method name plus parameter ID, independent of module path. Duplicate identities are rejected. Approved historical renames are explicit aliases in [`reports.py`](../../../ci/pipeline/reports.py); unknown renames are not silently accepted, and aliases cannot hide a regression.

### Reviewed duplicate retirements

Intentional duplicate removal requires an explicit entry in `ci/pipeline/reports.py::APPROVED_RETIREMENTS`, scoped to the exact reviewed baseline SHA, test identity and original module. It applies only if the removed case passed in that baseline and every named retained scenario passed in both base and head. A failing baseline case, missing/skipped/failing retained coverage, another baseline or an unrelated same-name case cannot use the exemption. An old case still present in head receives the ordinary failure/skip checks.

The identity remains in `removed`; `retired` records its retained coverage in both JSON and the readable summary. This is separate from identity aliases and does not reduce expected counts or omit tests from execution. The [retirement record](test-retirements.md) explains each entry. If a PR's base changes before merge, review the mapping against that base and update the exact SHA explicitly. Once merged, later baselines no longer contain these cases and the old exception cannot apply.

## CI code map

| Path | Responsibility |
| --- | --- |
| `ci/compare_tests.py` | Stable `resolve`, `classify`, `run`, `compare` CLI |
| `ci/pipeline/targets.py` | Immutable revision resolution and changed-path classification |
| `ci/pipeline/execution.py` | Running suites and producing reports |
| `ci/pipeline/reports.py` | Report integrity, approved identity aliases and reviewed duplicate retirements |
| `ci/pipeline/comparison.py` | Regression policy and final summary |
| `ci/pipeline/io.py` | Strict JSON and workflow outputs |
| `ci/run_suite.py`, `ci/run_pytest.py` | Suite partitioning and process environment |
| `ci/parallel-files.json` | Reviewed parallel groups |
| `ci/checks/` | End-to-end acceptance of CI's CLI behavior |

Keep workflow filenames, CLI options, artifacts and job identities stable when reorganizing internals. The decision/reporting pipeline uses the standard library and runs from the immutable CI-tools checkout, separately from the tested base/head checkout.

## Runner maintenance

On Unix, `ci/run_pytest.py` gives each pytest process a private `XDG_CACHE_HOME`, creates protected read-only PowerShell startup-profile files, and disables PowerShell telemetry before launching the child. This prevents concurrent startup-profile writers and cold-cache telemetry initialization from triggering known native startup failures. It leaves other caches writable, keeps `HOME` unchanged, cleans up temporary directories, and propagates pytest failure. The protection requires an unprivileged runner. Windows preserves its normal environment.

This is a CI process-environment mitigation; it does not add retries or alter normal Virgo sessions. `ci/checks/powershell_cache.py` requires real `pwsh` and exercises concurrent launches and environment cleanup. The runner rationale is also tracked by the upstream [PowerShell startup-profile issue](https://github.com/PowerShell/PowerShell/issues/26528).

Two allowlisted groups of isolated tests run in separate processes, serially within each group. The remaining suite runs serially with normal discovery, so new tests and shell/PTY tests stay serial unless reviewed. Missing allowlisted files in older baselines are ignored; duplicate manifest entries fail. Use `--execution serial` on the runner for diagnosis.

Before changing the runner or comparison policy, exercise the existing CLI checks:

```bash
uv run --no-sync python ci/checks/resolution.py
uv run --no-sync python ci/checks/parallel.py
uv run --no-sync python ci/checks/comparison.py
uv run --no-sync python ci/checks/powershell_cache.py
```

They drive real temporary projects and catch wrong revisions, missing/incomplete results, false-green prerequisites, identity collisions, lost tests, and runner failures. Expand coverage before changing a new failure boundary rather than after the implementation.

Retirement acceptance must also catch unrelated deletions, failing baseline cases, new skips, and retained coverage that is missing, skipped or failing. It must reject reuse against another baseline SHA or an unrelated same-name test, and detect a removed identity disappearing from either the readable or JSON comparison report.

## Manual diagnostics

The separate [Shell diagnostics workflow](../../../.github/workflows/shell-selection.yml) is manual-only:

```bash
gh workflow run pr-platform-comparison.yml -f pr_number=<number>
gh workflow run shell-selection.yml
gh workflow run shell-selection.yml -f powershell_diagnostics=true
gh workflow run shell-selection.yml --ref <branch> -f benchmark_suite=true
```

The benchmark runs serial and parallel suites on the same runner and commit and checks identical individual outcomes. Use its artifacts when reviewing an allowlist change. Keep one-off timings and incident investigation in the associated PR; update this guide only with conclusions needed for future maintenance.
