# Test portability

The suite exercises real tool execution, local HTTP servers, session flows and
Textual/PTY interactions without live model credentials. See the development
section in [README](../README.md) for commands and the test layout.

## Cross-platform fixtures and assertions

- Compare filesystem output with native `Path` strings, or normalize both sides
  as paths. Glob patterns still use `/`; rendered paths use the host separator.
- Read persisted UTF-8 data with `encoding="utf-8"`. Do not rely on the runner's
  locale or `PYTHONUTF8` to make test-side decoding correct.
- Specify fixture line endings when asserting exact text. `read` preserves the
  bytes' line endings, while `Path.write_text` normally translates newlines.
- Check POSIX mode bits only on POSIX. Keep credential merge, logout and malformed
  file checks running on Windows; mode bits do not test Windows ACL security.
- For shell-independent scenarios (process output, timeout, environment,
  background notification and hook feedback), use the `python_command` fixture.
  It creates a UTF-8 script and invokes the current interpreter through the real
  production shell. Shell-specific download tests retain their Bash/Zsh syntax
  and strict failure assertions.

Before implementing the command fixture, its failure modes were identified as:
incorrect quoting of interpreter/script paths containing spaces; shell expansion
of embedded Python punctuation; locale-dependent script encoding; scripts being
overwritten across multiple calls; and bypassing the production process path.
The existing tool/session scenarios must continue to verify exit status, timeout,
working directory, output truncation, inherited environment, idle notification,
image preservation and termination after hook feedback. Use a script directory
containing spaces to exercise quoting on every run.

## PR #9 CI diagnosis

The [three-platform comparison](https://github.com/0xAstraZhang/Virgo/actions/runs/43145296715)
at head `8b6f1ed63d75097ed904bdfdabd5331fa3fe5eec` recorded 18 Windows failures,
1123 passes and 18 skips. Linux and macOS each passed all 1159 cases.
The [earlier probe](https://github.com/0xAstraZhang/Virgo/actions/runs/43142469218)
had one additional failure because the test decoded UTF-8 history with the
Windows default encoding; the later workflow enabled UTF-8 mode globally.

Eleven of the 18 failures came from test assumptions: one POSIX mode assertion,
two session-picker path assertions, two glob/grep path assertions, one newline
fixture, one background command, two hook commands, and two basic process-tool
tests. The history decoding issue is also fixed for runs without UTF-8 mode.

At that revision, seven cases exposed unresolved application behavior and retained their assertions:

- Five Bash download failures: `spawn()` recognizes only executable names `bash`
  and `zsh`, so `bash.exe` receives neither `-e` nor `-o pipefail`. Failed curl
  commands can then be hidden by later successful commands. This is not a missing
  curl dependency or a reason to skip the tests.
- `/bin/date` and `/usr/bin/date` classification: the trusted directory allowlist
  contains POSIX strings but the classifier uses native `Path` parents. Windows
  consequently rejects these forms. Resolving the Windows shell/path policy is
  an application change; the test-only fix does not relax the classifier tests.

## Validation of the correction

[Three-platform CI run](https://github.com/0xAstraZhang/Virgo/actions/runs/43147759020)
tested commit `b310ebf6cc328838f6f4ad476624c0bfae856f4e` with Python 3.13 and locked
dependencies:

| Runner | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Ubuntu | 1159 | 0 | 0 |
| macOS | 1159 | 0 | 0 |
| Windows | 1134 | 7 | 18 |

All eleven test-assumption failures now pass on Windows. The seven remaining
failures are exactly the application cases above. Existing Windows skips remain
the ten POSIX PTY cases, seven unavailable zsh variants and one readline case.
The workflow enables `PYTHONUTF8=1`; the explicit UTF-8 history read also removes
the test's dependency on that setting, but this run does not exercise a legacy
Windows locale.

Local macOS validation also passed all 1159 tests (94.14 s), including PTY and
Bash/Zsh download cases; `ruff check` passed. No test function names, parameter
IDs or skip markers were changed. Application source and dependencies are unchanged.

## PR #10 Windows corrections

The [complete baseline comparison](https://github.com/0xAstraZhang/Virgo/actions/runs/43158741262)
tested main `811bc02` and PR head `83ba93f`. Both Windows revisions recorded the
same 12 failures, 1169 passes and 20 skips. Each failure's traceback was checked
against the selected-shell behavior introduced on main:

| Failing test (parameter) | Diagnosis and correction |
| --- | --- |
| `test_failed_download_does_not_read_existing_file` (`bash-timeout-28`, `bash-error-22`) | Legacy `spawn()` did not recognize `bash.exe`, so later successful commands hid curl failures. Normalize executable case and `.exe` before adding `-e -o pipefail`. Keep exact curl exit codes, unchanged old content and absent consumer assertions. |
| `test_failed_download_in_pipeline_is_not_reported_as_success` (`bash`) | The same missing flags hid the pipeline failure. Keep exit 22 and the absent consumer assertion. |
| `test_download_publishes_only_after_success` (`bash-partial-18`) | The same missing flags allowed a partial download to replace the old file. Keep exit 18, original content and temporary-file cleanup assertions. |
| `test_date_query_forms_are_read_only` (`/bin/date`, `/usr/bin/date`) | The POSIX classifier compared native Windows path parents to POSIX directories. Use `PurePosixPath` for command names and parents in both permission checks; filesystem confinement still uses native paths. |
| `test_background_download_failure_does_not_read_old_file` (`bash`) | The test patched the legacy module, but jobs use the session's selected interpreter. Bind the session to the fixture's exact Bash/Zsh executable. Keep exit 28, failed status and no stale consumption. This also ensures the Zsh variant actually runs Zsh. |
| `test_background_job_notifies_idle_session` | The faux model called `bash` while the Windows session exposed `powershell`. Use the selected tool and quote the Python command for that interpreter. Keep idle wakeup, pending delivery and output checks. |
| `test_command_hooks` | The hard-coded Bash tool never reached the hook on Windows. Target the selected command tool, and quote the hook's Python invocation for the native hook shell, including paths with spaces. Keep forbidden-command denial and allowed-command output. |
| `test_auto_review_executes_internal_changes_without_asking` | The POSIX `touch` scenario requires an explicitly selected Bash session. Preserve file writes and the assertion that neither review nor human approval occurs. PowerShell commands still require review. |
| `test_subagent_system_prompt_lists_only_its_own_tools` | The expected explore tool list assumes Bash. Select Bash explicitly and retain exact equality between prompt and model tool lists. |
| `test_plan_mode_exposes_read_only_tools_and_changes_back_on_switch` | The expected plan tool list assumes Bash. Select Bash explicitly and retain tool filtering, mode transition and message-history assertions. PowerShell remains unavailable in plan mode. |

The correction keeps all existing test identities, assertions and skip rules.
Additional regressions check executable suffix/case handling and reject nonstandard,
traversal and Windows-style executable paths in the POSIX classifier. Existing
PowerShell integration tests continue to cover execution, background jobs,
automatic review and plan-mode denial. No permissions are broadened to make tests pass.

The CI workflow and comparison rules are retained. The updated
[PR #10 description](https://github.com/0xAstraZhang/Virgo/pull/10) records the final
tested commit, three-platform totals and comparison run.

## Comparison revision binding

Before changing revision resolution, the failure cases are: a queued PR run or
historical rerun selecting newer base/head commits; using the base repository for
a fork's head; a manual PR input overriding event identity; missing or malformed
event data falling back to live state; and breaking explicit/latest-PR manual
selection or the target artifact/job outputs.

`python ci/check_resolution.py` exercises the real resolver CLI in subprocesses
with a local `gh` executable fixture. It checks event A while the API already
returns B, repeats A to model a rerun, checks same-repository and fork targets,
and verifies both `target.json` and `GITHUB_OUTPUT`. No GitHub credentials or
network access are required.

Automatic `pull_request` runs use the event payload's base/head SHAs, repository
names and PR identity, including on reruns. Missing event data fails resolution
without querying the live PR. Manual `workflow_dispatch` runs resolve the selected
PR from the API at startup and retain the existing latest-PR fallback. The
workflow runs the resolution end-to-end checks before producing its target.
