# Command shells

[Documentation](../README.md) · [简体中文](../../zh-CN/guides/shells.md)

## Select a shell

Virgo detects supported shells at startup. `/shell` shows the cached choices, including executable paths and reported versions. The selector does not rescan the machine every time you open it.

```text
/shell bash
/shell zsh
/shell pwsh
/shell powershell
/shell "C:\Program Files\PowerShell\7\pwsh.exe"
/shell auto
```

Supported interpreters are Bash, Zsh, POSIX sh, dash, PowerShell 7+ and Windows PowerShell 5.1. CMD, Fish, WSL distributions and arbitrary interpreters are not adapters. A selected name or path is validated by starting the interpreter; it cannot include extra command-line arguments. A newly installed interpreter can be selected directly, while restarting refreshes the full catalog.

On Windows, automatic selection prefers PowerShell 7, then Windows PowerShell. On macOS/Linux it prefers Bash, then Zsh/sh. An invalid explicit configuration disables command execution and asks you to select again; it does not silently switch languages.

Selection immediately changes the current session and saves `shell.executable` in user settings. On restart, project and runtime settings can override it; `VIRGO_SHELL` takes highest precedence. Cancellation or failed validation preserves the previous selection.

## Execution behavior

POSIX interpreters use the `bash` tool name; PowerShell uses `powershell`. The model receives the actual interpreter type, version, executable path and syntax. Every command starts a separate process with stdin closed; shell state such as `cd` does not persist between calls.

| Shell | Error and startup behavior |
| --- | --- |
| Bash / Zsh | Noninteractive startup without user profiles; `errexit` and `pipefail`; `BASH_ENV`/`ENV` removed |
| sh / dash | Noninteractive startup and `errexit`; no general `pipefail` guarantee |
| PowerShell 7.4+ | `-NoProfile`, noninteractive UTF-8 output; cmdlet errors and unhandled native command failures stop execution |
| Windows PowerShell 5.1 / PowerShell 7.0–7.3 | Cmdlet errors stop execution; only the last native exit code is automatically propagated, so earlier commands need explicit `$LASTEXITCODE` checks |

Zsh's system-level startup rules still apply. PowerShell uses an encoded command without changing execution policy; a command exceeding Windows command-line limits must be moved into a script file. An expected nonzero exit code needs explicit handling in the script.

`/shell` waits for the current main run to finish. Existing background jobs and child runtimes retain the interpreter they started with; newly created runtimes use the new selection. User-configured command hooks keep their own execution contract.

## Output, timeouts and cancellation

Foreground and background stdout/stderr stream directly into files under `.virgo/outputs/`. Those files can be read while the command is still running. Displayed output is a bounded tail; results include `output_path`, `output_bytes` and `truncated`. The complete log retains original bytes, while display uses UTF-8 with replacement for invalid bytes. A log-creation failure prevents process startup.

Foreground commands default to a 300-second timeout. The maximum is 3600 seconds. Background commands without an explicit timeout can continue until completion or cancellation. Unix cancellation uses process groups. Windows 10+/Server 2016+ uses Job Objects to manage the process tree and clean up descendants on timeout, cancellation, completion or session shutdown. Job Objects are process-lifecycle management, not a permission sandbox.

## Permissions

PowerShell has independent `powershell(...)` rules; existing `bash(...)` rules do not automatically authorize it. Auto-review receives the actual shell information. Plan mode omits PowerShell and uses file tools for investigation. Dynamic PowerShell paths cannot be safely cached as “allow for this session.” Changing the interpreter does not reuse approval cached for a different interpreter.

See [permission modes](../reference/configuration.md#permissions) and [development checks](../development/testing-and-ci.md) for configuration and platform coverage.

Implementation: [shell adapters](../../../src/virgo/tools/shell.py), [shell selection](../../../src/virgo/harness/shell_commands.py), [Windows process management](../../../src/virgo/tools/windows_process.py).
