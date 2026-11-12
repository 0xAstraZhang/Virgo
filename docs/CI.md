# Cross-platform PR comparison

The `Cross-platform PR comparison` workflow runs the PR head and its base SHA on
`ubuntu-latest`, `windows-latest`, and `macos-latest`, using Python 3.13 and each
revision's locked dependencies. Linux installs zsh and curl so shell variants run.
Windows retains its native default shell; POSIX PTY tests skip where unsupported.

Every pull request runs automatically once this workflow is on the default branch.
For a manual run, select the workflow and enter a PR number (blank selects the
newest open PR). The `codex/cross-platform-ci` push trigger bootstraps the workflow
before it has been merged; it ignores its own configuration PR when choosing a target.
The resolve job records immutable head/base SHAs before starting the six jobs.

Recorded runs: [PR #9 on 2026-11-12](ci-results/pr-9-2026-11-12.md).

## Reading results

The comparison job's summary and `comparison` artifact contain the outcome table,
new failures, existing failures, fixed failures, changed skips, and missing/new tests.
Each `results-<platform>-<revision>` artifact contains pytest output, JUnit XML,
normalized results, interpreter information, and the tested SHA.

Comparison identifies tests by function/method name and parameter ID, ignoring the
module path so moving a test does not manufacture a new failure. Duplicate keys
are rejected as ambiguous. The two renamed parameter IDs in PR #9's
`test_compaction_does_not_enable_native_search` are explicitly mapped to their
previous names; other renames are reported as added/removed tests.

A failed PR test is classified as a regression only if the baseline passed it.
Failures that were skipped or absent in the baseline are listed separately as
newly exposed failures, and also fail the comparison. Existing failures remain
visible but do not fail the comparison. A disappearing executed baseline test,
an ambiguous result, an incomplete run, or a missing artifact fails the comparison.
New skips also fail the comparison so a platform skip cannot hide a regression.
Module-level collection skips are reported separately from individual test skips.

## Validation before changing the runner

The end-to-end check uses temporary pytest projects and invokes both CLI commands.
It must cover these failure modes before runner changes are made:

- A test moved between modules is incorrectly treated as added or removed.
- Existing failures hide a new failure, or a fixed failure remains marked failing.
- A skipped/uncollected baseline case becomes a failure and is mislabeled a regression.
- A parameter rename or duplicate function name silently mismatches test cases.
- Collection errors, missing reports, and runner failures produce a false green result.
- A previously executed test disappears or starts being skipped without being reported.

Run `python ci/check_comparison.py` in an environment with pytest installed.
This is a CLI end-to-end check; it does not import or mock the comparison runner.
