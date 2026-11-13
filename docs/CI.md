# Continuous integration

Open [Cross-platform CI](https://github.com/0xAstraZhang/Virgo/actions/workflows/pr-platform-comparison.yml)
for automated checks, or [Shell diagnostics](https://github.com/0xAstraZhang/Virgo/actions/workflows/shell-selection.yml)
for a focused manual investigation. These filenames are stable. The old
`cross-platform-probe.yml` URL is not a workflow in this repository.

## What runs

| Event | Product tests | Passing result |
| --- | --- | --- |
| Code/configuration PR | Complete base and head suites on Linux, Windows and macOS | No new failures, lost executed tests or newly skipped baseline tests |
| Code/configuration push to main | Complete current-commit suite on all three platforms | No failing tests |
| Documentation-only PR or main push | Lightweight checks; explicit full-suite skip summary | All lightweight checks pass |
| Manual PR comparison | Complete base/head suites, including documentation-only PRs | Same regression policy as automatic PR checks |
| Manual Shell diagnostics | Two shell modules on all platforms, or focused PowerShell cases on Ubuntu | All selected cases pass |

Every automatic run performs Ruff, pinned actionlint validation, and the resolution
and comparison CLI acceptance checks on Linux. There are nine jobs for a full PR,
six for a full main run, and three executing jobs for documentation-only changes.
The skipped test matrix is visible in the latter. Previously each PR ran eleven
jobs, including redundant Shell tests and three copies of the comparison self-check.

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
summary jobs have five-minute limits, lightweight checks ten minutes, and test or
Shell jobs twenty minutes. Failure summaries and uploads use `!cancelled()` so
failures remain observable without starting new work after cancellation. All
artifacts expire after fourteen days. No repository protection rules are modified.

## Reading results

The comparison job's summary and `comparison` artifact contain the outcome table,
new failures, existing failures, fixed failures, changed skips, and missing/new tests.
Each `results-<platform>-<revision>` artifact contains pytest output, JUnit XML,
normalized results, interpreter information, and the tested SHA.

Comparison identifies tests by function/method name and parameter ID, ignoring the
module path so moving a test does not manufacture a new failure. Duplicate keys
are rejected as ambiguous. The two renamed parameter IDs in PR #9's
`test_compaction_does_not_enable_native_search` are explicitly mapped to their
previous names. The Harness simplification's four renamed Todo, question and
sub-agent cases also map to their previous names so changed expectations remain
comparable; other renames are reported as added/removed tests.
PR #18's unreadable-thought and inline-activity replacement cases are also mapped
to their original names. PR #16's startup refresh, picker cancellation, and two
parameterized metadata-precedence replacements are mapped likewise. Their new
expectations reflect the feature change: refresh on restart with cached fallback,
Esc closing only the picker, and explicit configuration overriding fetched fields.
The comparison CLI check covers passing and failing replacements, unknown names,
and collisions between old and new names. An approved replacement can still fail as a regression;
an unknown rename or an alias collision still fails the comparison.

A failed PR test is classified as a regression only if the baseline passed it.
Failures that were skipped or absent in the baseline are listed separately as
newly exposed failures, and also fail the comparison. Existing failures remain
visible but do not fail the comparison. A disappearing executed baseline test,
an ambiguous result, an incomplete run, or a missing artifact fails the comparison.
A previously executed baseline case becoming skipped also fails the comparison.
A newly added test that is platform-skipped is listed as added, not a regression.
Module-level collection skips are reported separately from individual test skips.
Main's single-revision mode fails on every failed/error test; it does not apply the
PR allowance for existing failures.

The summary validates upstream job outcomes and every expected report. Invalid
JSON (including duplicate keys), unexpected/duplicate matrix records, empty test
sets, incomplete execution, invalid statuses/counts, mismatched exit codes and
wrong SHAs fail with a readable summary. Missing or invalid targets also fail.
The producer's diagnostics remain visible even when its report is rejected.

`ci/compare_tests.py` exposes `resolve`, `classify`, `run`, and `compare` commands.
`classify --target target.json [--project .]` adds the full-test decision and reason.
`compare` retains the default base/head behavior and accepts `--mode head` for a
single strict revision; workflow targets select the mode automatically. Workflow
calls also pass `--resolve-result`, `--checks-result`, and `--test-result`, so
skipped/failed prerequisites cannot produce a successful final check.

## PowerShell startup isolation

Both revisions use `ci/run_pytest.py`, from the same comparison-tools checkout.
On Unix it gives pytest a fresh `XDG_CACHE_HOME` and pre-creates empty, read-only
`powershell/StartupProfileData-Interactive` and `StartupProfileData-NonInteractive`
files. This avoids inheriting corrupt profiles and prevents concurrent probes or
commands in one job from overwriting them. It also sets
`POWERSHELL_TELEMETRY_OPTOUT=true` for the Unix pytest child: a fresh cache would
otherwise make concurrent shells initialize a telemetry UUID through a named
mutex that can crash inside .NET. See the
[macOS native-crash investigation](ci-results/powershell-macos-2026-11-12.md)
for the captured fault and validation. Other caches remain writable, `HOME`
is unchanged, and the temporary directory is removed when pytest exits, including
test failures and collection errors. Run this wrapper as an unprivileged user;
it fails explicitly if the startup files remain writable. Windows keeps its
existing environment and process behavior.

This is a CI environment mitigation for
[PowerShell's startup-profile race](https://github.com/PowerShell/PowerShell/issues/26528),
not a retry or skip policy. It applies equally to the complete baseline/head
comparison, main validation and manual `Shell diagnostics`. Each head platform
and manual Shell job runs `python ci/check_powershell_cache.py`, which requires real PowerShell 7
and exercises 48 launches with up to eight concurrent processes. Normal local
Virgo sessions are not changed by this CI-only wrapper.

## Validation before changing the runner

The end-to-end check uses temporary pytest projects and invokes both CLI commands.
It must cover these failure modes before runner changes are made:

- A malformed, empty, incomplete, duplicate or wrong-SHA report is accepted as green.
- A main failure is incorrectly allowed as an existing PR failure.
- A failed/skipped prerequisite is hidden by the documentation-only decision.
- A rename into docs, a root configuration file, missing history or a manual run
  is incorrectly classified as documentation-only.
- Event SHAs drift to live API values, or fork/manual targets resolve incorrectly.
- A test moved between modules is incorrectly treated as added or removed.
- Existing failures hide a new failure, or a fixed failure remains marked failing.
- A skipped/uncollected baseline case becomes a failure and is mislabeled a regression.
- A parameter rename or duplicate function name silently mismatches test cases.
- Collection errors, missing reports, and runner failures produce a false green result.
- A previously executed test disappears or starts being skipped without being reported.
- An approved test replacement hides a regression or collides with its old name.
- A corrupt inherited PowerShell startup cache or concurrent writers crash probes.
- Cold-cache PowerShell processes initialize telemetry and enter its named-mutex
  path, including when the parent explicitly enables telemetry.
- Cache protection prevents real shell execution, changes HOME, leaks directories,
  swallows pytest failures, or differs between base and head.

Run `python ci/check_resolution.py` and `python ci/check_comparison.py` in an environment with pytest installed.
This is a CLI end-to-end check; it does not import or mock the comparison runner.
With `pwsh` on PATH, also run `python ci/check_powershell_cache.py`.

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
