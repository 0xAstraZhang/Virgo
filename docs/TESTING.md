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

Seven cases expose unresolved application behavior and retain their assertions:

- Five Bash download failures: `spawn()` recognizes only executable names `bash`
  and `zsh`, so `bash.exe` receives neither `-e` nor `-o pipefail`. Failed curl
  commands can then be hidden by later successful commands. This is not a missing
  curl dependency or a reason to skip the tests.
- `/bin/date` and `/usr/bin/date` classification: the trusted directory allowlist
  contains POSIX strings but the classifier uses native `Path` parents. Windows
  consequently rejects these forms. Resolving the Windows shell/path policy is
  an application change; the test-only fix does not relax the classifier tests.

Local macOS validation: all 1159 tests passed, including PTY and Bash/Zsh download
cases; `ruff check` passed. No test function names, parameter IDs or skip markers
were changed. The Windows corrections still require a native Windows CI run.
