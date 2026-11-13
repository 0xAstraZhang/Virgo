# Continuous integration

Open [Cross-platform CI](https://github.com/0xAstraZhang/Virgo/actions/workflows/pr-platform-comparison.yml)
for automated checks, or [Shell diagnostics](https://github.com/0xAstraZhang/Virgo/actions/workflows/shell-selection.yml)
for a focused manual investigation. These filenames are stable. The old
`cross-platform-probe.yml` URL is not a workflow in this repository.

## What runs

| Event | Product tests | Passing result |
| --- | --- | --- |
| Code/configuration PR | Complete base and head suites on Linux, Windows and macOS | No new failures, unapproved test removals or newly skipped baseline tests |
| Code/configuration push to main | Complete current-commit suite on all three platforms | No failing tests |
| Documentation-only PR or main push | Lightweight checks; explicit full-suite skip summary | All lightweight checks pass |
| Manual PR comparison | Complete base/head suites, including documentation-only PRs | Same regression policy as automatic PR checks |
| Manual Shell diagnostics | Two shell modules on all platforms, or focused PowerShell cases on Ubuntu | All selected cases pass |

Every automatic run performs Ruff, pinned actionlint validation, and the resolution,
parallel-execution and comparison CLI acceptance checks on Linux. There are nine
jobs for a full PR,
six for a full main run, and three executing jobs for documentation-only changes.
The skipped test matrix is visible in the latter.

Only `README.md` and Markdown files under `docs/` qualify for the lightweight path.
Classification uses the exact resolved base/head commits, with rename detection
disabled so both old and new paths are considered. Missing history, failed diffs,
and empty change lists conservatively run full tests. There is no workflow-level
path filter: the final `Compare failures and coverage` check always reports the
outcome, including lightweight-check failures. This avoids the
[pending-check behavior of skipped workflows](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/skip-workflow-runs).

PR targets come from the triggering event, including fork repositories; rerunning
an old event does not switch to current live PR SHAs. Main uses the push event's
`after` commit, verified against `GITHUB_SHA`, with `before` used only for change
classification. All jobs use tools from the workflow's immutable `github.sha`.
Manual comparison resolves a supplied PR number once, or selects the newest open
PR without historical branch exclusions. The target artifact and final summary
identify the selected PR and commits.

```bash
# Run from the default branch, or add --ref <branch> to validate updated CI tools.
gh workflow run pr-platform-comparison.yml -f pr_number=28
gh workflow run shell-selection.yml
gh workflow run shell-selection.yml -f powershell_diagnostics=true
```

The complete suite already contains Shell selection/runtime coverage. The separate
Shell workflow is manual-only. PowerShell cache acceptance still runs once on each
head platform. Every native command whose exit status matters has its own workflow
step, or runs in Bash with fail-fast behavior, so a later successful command cannot
hide an earlier Windows PowerShell failure.

## Where to make changes

| Location | Responsibility |
| --- | --- |
| `.github/workflows/pr-platform-comparison.yml` | Automatic checks and manual PR comparison: resolve + checks → test → compare |
| `.github/workflows/shell-selection.yml` | Manual shell diagnostics and optional serial/parallel benchmark |
| `ci/compare_tests.py` | Stable CLI entry point and argument parsing |
| `ci/pipeline/targets.py` | Event revision resolution and documentation-only classification |
| `ci/pipeline/execution.py` | Suite invocation, normalized reports and failure diagnostics |
| `ci/pipeline/reports.py` | Report validation and approved test-name aliases |
| `ci/pipeline/comparison.py` | Regression policy, upstream-job validation and final summary |
| `ci/pipeline/io.py` | Strict JSON artifacts and workflow outputs |
| `ci/run_suite.py`, `ci/run_pytest.py` | Process partitioning and PowerShell cache isolation |
| `ci/parallel-files.json` | Reviewed parallel test groups |
| `ci/checks/` | CLI end-to-end acceptance checks for CI itself |
| `ci/benchmark_suite.py` | Same-runner serial/parallel comparison |

Workflow files handle orchestration; the Python CLI owns decisions and reports.
The pipeline uses only the standard library, so resolution and comparison do not
install the tested project's dependencies. Runner paths are resolved relative to
the CI tools checkout, including when the working directory is a different base or
head checkout. Keep the CLI options, artifact names, workflow filenames and job IDs
stable when reorganizing internals.

## Details and maintenance

- [Reading results and comparison policy](ci/results.md): artifacts, identity aliases,
  regression rules, report validation and CLI options.
- [Runner maintenance](ci/runner.md): PowerShell isolation, parallel execution,
  local acceptance commands and the failure modes they cover.
- [Serial/parallel benchmark](ci-results/parallel-benchmark-2026-11-13.md): measured
  cross-platform outcomes and timings.

## Environment and resource limits

All workflows use Python 3.13, uv 0.13.8, UTF-8 mode, and locked development
dependencies; subsequent project commands use `--no-sync`. Actions are pinned to
full commit SHAs. actionlint 1.7.12 is downloaded from its official release and
verified against the committed archive SHA-256. Dependency caches hash only the
manifest and lockfile in the checkout being installed. Linux installs zsh and curl;
Windows retains its native shells, and unsupported POSIX PTY cases remain skipped.
Runner/interpreter details and shell paths are logged. Python 3.11 is still the
package's declared minimum; expanding CI beyond 3.13 remains follow-up work.

Linux head jobs also build the sdist and wheel, install the wheel with locked
runtime dependencies in a clean environment outside the checkout, and run
`virgo --help`. Installation, PowerShell acceptance, build and artifact failures
are blocking even if a pytest report happens to be green.

New automatic runs cancel superseded runs of the same PR or main branch. Manual
runs use separate concurrency groups and do not cancel one another. Resolution and
summary jobs have five-minute limits, lightweight checks ten minutes, test jobs
twenty minutes, and manual Shell jobs thirty-five minutes. Failure summaries and uploads use `!cancelled()` so
failures remain observable without starting new work after cancellation. All
artifacts expire after fourteen days. No repository protection rules are modified.

## Historical investigations

These records describe the commits and workflow versions at their recorded dates:

- [PR #9 and the then-current main audit](ci-results/pr-9-2026-11-12.md)
- [Test renames and PowerShell startup failures](ci-results/failure-investigation-2026-11-12.md)
- [macOS PowerShell telemetry/native crash investigation](ci-results/powershell-macos-2026-11-12.md)

For the workflow audit on 2026-11-13, the pre-change reference runs are
[PR #28 full comparison](https://github.com/0xAstraZhang/Virgo/actions/runs/43378917384)
(10m09s elapsed) and its
[Shell workflow](https://github.com/0xAstraZhang/Virgo/actions/runs/43378917392)
(2m46s elapsed). Those workflows ran concurrently; their elapsed times are not
additive. The optimization PR records its actual acceptance runs and timings.
