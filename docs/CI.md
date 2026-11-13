# Cross-platform PR comparison

The `Cross-platform PR comparison` workflow runs the PR head and its base SHA on
`ubuntu-latest`, `windows-latest`, and `macos-latest`, using Python 3.13 and each
revision's locked dependencies. Linux installs zsh and curl so shell variants run.
Windows retains its native default shell; POSIX PTY tests skip where unsupported.

The `pull_request` event runs the comparison for the triggering PR, including this
configuration PR. Once the workflow is on the default branch, manual runs can
select a PR number (blank selects the newest open PR other than the
`codex/cross-platform-ci` configuration PR). The temporary configuration-branch
push trigger has been removed so branch updates cannot select an unrelated PR.
The resolve job records immutable head/base SHAs before starting the six jobs.

Recorded runs and the current-main audit:
[PR #9 on 2026-11-12](ci-results/pr-9-2026-11-12.md).
For deterministic test-rename failures and intermittent PowerShell startup crashes,
see the [2026-11-12 failure investigation](ci-results/failure-investigation-2026-11-12.md).
The separate `Shell selection E2E` workflow on main runs two shell test modules;
it does not replace this complete-suite baseline comparison.

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
New skips also fail the comparison so a platform skip cannot hide a regression.
Module-level collection skips are reported separately from individual test skips.

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
comparison and `Shell selection E2E`. The latter also runs the comparison CLI
checks and `python ci/check_powershell_cache.py`, which requires real PowerShell 7
and exercises 48 launches with up to eight concurrent processes. Normal local
Virgo sessions are not changed by this CI-only wrapper.

## Validation before changing the runner

The end-to-end check uses temporary pytest projects and invokes both CLI commands.
It must cover these failure modes before runner changes are made:

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

Run `python ci/check_comparison.py` in an environment with pytest installed.
This is a CLI end-to-end check; it does not import or mock the comparison runner.
With `pwsh` on PATH, also run `python ci/check_powershell_cache.py`.
