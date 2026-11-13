# CI runner maintenance

[CI overview](../CI.md) · [Result policy](results.md)

## PowerShell startup isolation

Both revisions use `ci/run_pytest.py`, from the same comparison-tools checkout.
On Unix it gives pytest a fresh `XDG_CACHE_HOME` and pre-creates empty, read-only
`powershell/StartupProfileData-Interactive` and `StartupProfileData-NonInteractive`
files. This avoids inheriting corrupt profiles and prevents concurrent probes or
commands in one job from overwriting them. It also sets
`POWERSHELL_TELEMETRY_OPTOUT=true` for the Unix pytest child: a fresh cache would
otherwise make concurrent shells initialize a telemetry UUID through a named
mutex that can crash inside .NET. See the
[macOS native-crash investigation](../ci-results/powershell-macos-2026-11-12.md)
for the captured fault and validation. Other caches remain writable, `HOME`
is unchanged, and the temporary directory is removed when pytest exits, including
test failures and collection errors. Run this wrapper as an unprivileged user;
it fails explicitly if the startup files remain writable. Windows keeps its
existing environment and process behavior.

This is a CI environment mitigation for
[PowerShell's startup-profile race](https://github.com/PowerShell/PowerShell/issues/26528),
not a retry or skip policy. It applies equally to the complete baseline/head
comparison, main validation and manual `Shell diagnostics`. Each head platform
and manual Shell job runs `python ci/checks/powershell_cache.py`, which requires real PowerShell 7
and exercises 48 launches with up to eight concurrent processes. Normal local
Virgo sessions are not changed by this CI-only wrapper.

## Live diagnostics and controlled parallel execution

The suite runner streams each test result and complete failure traceback to the
Actions log, tagged with its execution group. Every result artifact also contains
`failures.md` (test name, source file/line, assertion or exception traceback),
per-group logs/XML, merged JUnit XML, and the existing strict normalized report.
The source line in JUnit identifies the test definition; the traceback identifies
the precise failing expression. Locals are not dumped. Each group reports its
slowest 30 test phases. Collection errors and abnormal exits remain blocking.

`ci/parallel-files.json` explicitly lists two reviewed groups of isolated AI and
in-process TUI modules. Each group runs serially in its own Python process with
the existing PowerShell cache isolation. After both finish, normal pytest discovery
runs with those files excluded: new files, shell/PTY tests and other scenarios
therefore remain serial by default. There are no retries, new dependencies, or
changes to test assertions. Missing allowlisted files in older PR bases are ignored;
duplicate manifest entries are rejected. The merged report retains all test cases
and the existing comparison still rejects ambiguous identities.

Use `compare_tests.py run --execution serial` to disable parallel execution for
investigation. Before changing the allowlist, use the manual benchmark:

```bash
gh workflow run shell-selection.yml --ref <branch> -f benchmark_suite=true
```

It runs complete serial and parallel suites on the same runner/commit, asserts
identical individual outcomes, and uploads `benchmark-<os>` artifacts and timings.
The normal PowerShell isolation acceptance (48 real launches) and shell scenarios
also execute. `ci/checks/parallel.py` exercises the CLI with real passing/failing
and collection-error projects; it verifies partition completeness and actionable
failure diagnostics before the production suite runs.

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

Run the existing CLI end-to-end checks from the repository root:

```bash
uv sync --locked --dev
uv run --no-sync ruff check
uv run --no-sync python ci/checks/resolution.py
uv run --no-sync python ci/checks/parallel.py
uv run --no-sync python ci/checks/comparison.py
# Requires real PowerShell 7 (pwsh) on PATH:
uv run --no-sync python ci/checks/powershell_cache.py
```

These checks invoke the real CLI in temporary projects; they do not import or mock
the pipeline modules. Workflows invoke the same checks before running product tests.

