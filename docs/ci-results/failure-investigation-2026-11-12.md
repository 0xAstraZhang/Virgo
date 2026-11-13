# CI failure investigation, 2026-11-12

Two independent conditions produce the same failed comparison job: test identity
changes and intermittent PowerShell startup failures. Re-running can change the
latter; it does not resolve an unmapped test rename. The initial observations
below are retained as evidence; the subsequent CI correction is described next.

Follow-up: the residual macOS `SIGSEGV` after startup-profile protection has now
been reproduced with a native crash report. It faults in .NET's named-mutex wait
on the cold-cache telemetry path. See the
[dedicated investigation and CI mitigation](powershell-macos-2026-11-12.md).
The older failures below did not retain native reports and cannot all be assigned
to this newly captured mechanism.

## CI correction

The comparator now explicitly maps PR #18's two replacement names to the original
cases. Its CLI acceptance check covers passing replacements, failing replacements,
unknown renames, and old/new names colliding in one suite. It reproduced the
original false disappearance before the mapping was added and passes afterward.

`ci/run_pytest.py` now isolates Unix CI runs from existing PowerShell caches and
protects the two startup-profile files against writes. Both baseline/head suites
and the shell workflow use it. Empty read-only profiles prevent the known write
race even within one job; other cache files remain writable. This implements the
upstream read-only-file workaround in a disposable directory rather than changing
the user's cache. Windows keeps its existing environment. Application shell code
and ordinary local runs are unchanged.

The cache acceptance check failed before implementation because pytest inherited
the old cache directory. After implementation it passed using the official
PowerShell 7.6.9 macOS arm64 portable archive (SHA-256 verified against the release
asset). It executed 48 real version probes/commands, up to eight concurrently,
across the base/head runner paths; verified the protected files stayed empty,
other cache writes worked, HOME and the caller's cache were preserved; and checked
cleanup and exit codes after passing tests, failing tests, and collection errors.
This validates prevention of shared startup-profile writes, not reproduction of
the historical crash from an unavailable corrupt runner cache.

## PR #18: deterministic test identity mismatch

The [failed comparison](https://github.com/0xAstraZhang/Virgo/actions/runs/43195730495/job/131110306376)
tested base `3685714ebfbb3839d5a5d76b770bfde67e85dff7` against head
`4e88e5cbe9d3defe5ace7289f766c59caff96cfc`.

| Platform | Head passed | Head failed | Head skipped |
| --- | ---: | ---: | ---: |
| Ubuntu | 1234 | 0 | 6 |
| Windows | 1216 | 0 | 24 |
| macOS | 1240 | 0 | 6 |

All three comparisons fail solely because two previously executed test names
are absent. [PR #18](https://github.com/0xAstraZhang/Virgo/pull/18/files)
changes those tests to match the new UI behavior:

| Original test | Replacement test |
| --- | --- |
| `test_encrypted_thought_label_is_display_only_live_and_in_history` | `test_unreadable_thoughts_are_hidden_live_and_in_history` |
| `test_inline_activity_reuses_thought_header_and_keeps_prompt_before_reply` | `test_inline_activity_is_separate_from_thought_folding_and_follows_prompt` |

There is also one new test, `test_live_messages_are_visible_and_folds_are_independent`.
The comparator uses the function name and parameter ID as test identity. Its alias
table does not contain these two renames. Consequently it reports three additions,
two removals, and `2 previously executed tests disappeared`; it deliberately exits
with status 1 when an executed baseline case disappears.

Validation used temporary pytest projects containing these exact two baseline
names and three head names, all with passing bodies. Both real pytest runs exited
0; three consecutive invocations of the comparison CLI each exited 1 with the
same two-disappeared-tests problem. The executed workflow's comparison script at
merge revision `29700a61538d2d2eb1296a7aac15281ecebba6a8` was also checked for this
rule and the missing aliases. This reproduces test identity handling, not the UI
behavior tested by the original cases.

Review the replacement coverage, then preserve stable case identities or add
explicit aliases for approved replacements. Do not remove the general missing-test
check: it also detects accidental loss of executed coverage.

## Historical retries: PowerShell startup instability

[PR #13 attempt 1](https://github.com/0xAstraZhang/Virgo/actions/runs/43179659281/attempts/1)
failed on macOS with four new failures and one new skip. Its
[second attempt](https://github.com/0xAstraZhang/Virgo/actions/runs/43179659281/attempts/2)
passed. Both comparison logs record the same base
`0cf0007d0fa38e6ecfb34c55c5dcdadbce445c61` and head
`6fd14423ef8f6f1f596f8ed02792dc3127bb5eca`; both macOS head logs identify image
`macos-26-arm64`, version `20261102.0473.1`, and Python 3.13.17.

The first macOS head result was 1225 passed / 4 failed / 7 skipped; the retry was
1230 passed / 0 failed / 6 skipped. Failures involved selecting `pwsh`/`powershell`
and executing PowerShell background jobs. The older assertions did not include
probe diagnostics, so this run alone cannot identify the startup failure mechanism.

Newer failures provide the missing diagnostics:

- [PR #17 Ubuntu head](https://github.com/0xAstraZhang/Virgo/actions/runs/43194509610/job/131101946573):
  seven cases fail while selecting `/usr/bin/pwsh`. The version probe exits `-6`
  with `System.IO.FileLoadException`, an invalid assembly name, and a visibly
  corrupted public-key token (`b0ﾰ`).
- [PR #16 macOS base](https://github.com/0xAstraZhang/Virgo/actions/runs/43196674708/job/131109370954):
  thirteen cases fail while selecting `/usr/local/bin/pwsh` or `powershell`, with
  probe exit `-6` and `Stack overflow.`.

These failures occur in the interpreter's version probe, before the requested
shell test behavior runs. A failed selection preserves the previous shell, which
explains the older assertions comparing `bash` with the requested PowerShell kind.

## Likely trigger and verification boundary

The signatures match the upstream
[PowerShell startup-cache corruption report](https://github.com/PowerShell/PowerShell/issues/26528)
and its [.NET runtime issue](https://github.com/dotnet/runtime/issues/121977).
Concurrent noninteractive PowerShell processes can corrupt
`StartupProfileData-NonInteractive`, causing invalid assembly names, stack
overflows, or other startup crashes. The upstream reports describe both signatures
seen here. They remained open when checked.

Virgo's `discover_shells()` in `src/virgo/tools/shell.py` launches up to eight
probes concurrently and deduplicates resolved executable paths only afterward.
Different paths or aliases can therefore start the same PowerShell installation
at once. `shell_environment()` inherits the user cache location; `VIRGO_HOME`
isolation in the tests does not isolate PowerShell's startup cache. This provides
a plausible trigger inside a single job; it does not require separate GitHub
matrix machines to share a filesystem.

Cache corruption is a strong inference from the error signatures and execution
path, not a direct examination of a failing runner's cache. The failed machines'
cache files were not retained. PowerShell was unavailable during the initial
investigation; the subsequent portable-runtime validation is recorded above.
The PR #13 rerun proves nondeterminism, but its older logs
cannot conclusively establish that it had this exact cache failure.

Recommended follow-up: capture shell versions, probe stderr and cache metadata;
give concurrently launched PowerShell processes independent writable cache
locations, or otherwise prevent concurrent writes to this specific startup cache.
On Unix, a unique `XDG_CACHE_HOME` per PowerShell process is a candidate to validate.
One cache directory per CI job alone does not eliminate concurrency within that job.
Exercise discovery and foreground/background execution through real processes,
including cleanup, before adopting the mitigation.

## Interpreting the comparison

Base and head execute on separate runners. A transient interpreter failure only
on head is classified as a new failure; the same transient only on base is
classified as fixed. PR #18 illustrates the latter with six Ubuntu baseline
PowerShell failures and none on head, despite its UI-only change.

The six matrix jobs use `continue-on-error` for pytest and preserve its results.
Their green job badges alone do not establish that all tests passed. Inspect the
comparison table and raw pytest output. The comparator also reports other failure
types: for example, the inspected PR #16 run contains search assertions and four
removed cases in addition to baseline PowerShell crashes. Those require separate
investigation and must not be dismissed as this intermittent startup issue.
