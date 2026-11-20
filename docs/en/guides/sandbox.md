# Execution boundaries and sandbox

> Virgo's sandbox is a **guardrail** against occasional model mistakes. It keeps their effects inside the current workspace, protects Virgo's private data and project control files, and asks before out-of-scope or high-risk operations. It is **not** a security boundary against malicious code, deliberate evasion, or prompt-injection attack chains. Run Virgo in a container or VM when working with untrusted repositories or content.

On Windows, we recommend running Virgo inside WSL2, where it uses the same sandbox as Linux. The native Windows sandbox is experimental, slower to start and limited in scope. See [WSL2 setup](wsl2.md) and [experimental native Windows sandbox](windows-sandbox.md).

## What the guardrail covers

1. **Workspace scoping and control metadata protection**: By default, **Workspace-Edit** allows writing only inside the live project workspace. Control files (`.git`, `.virgo`, `AGENTS.md`, `CLAUDE.md`) and interpreter roots are write-protected. Broker file tools (`read`, `write`, `edit`, `glob`, `grep`) enforce `PermissionPolicy` directly—denying writes to protected names even when they do not yet exist—with final-component `O_NOFOLLOW` and atomic temp-file replacement.
2. **Private state and credential protection**: Virgo's private directory (`~/.virgo` or `$VIRGO_HOME`) is read- and write-denied on Linux and macOS (including when the workspace itself is `$HOME` on POSIX). **Read-Only** disables Shell networking by default; **Workspace-Edit** enables Shell networking.
3. **High-risk command confirmation**: High-cost or irreversible Shell commands (`git push --force`, pushes to protected branches, `git reset --hard`, `git clean -f`, `curl | sh`, recursive deletion outside the workspace, deploy/publish, cloud resource deletion, and destructive SQL/Redis commands) require explicit one-shot user confirmation in **all** modes (including **No-Limit**, unless `permissions.confirmHighRisk` is set to `false` in No-Limit).
4. **Visible failure without silent downgrade**: If the native sandbox backend cannot be established, Virgo fails closed with an error, offers an explicit one-shot **No-Limit** option in interactive sessions (leaving the session mode unchanged), and never silently falls back to Host or automatically replays a command that may have started.

## What is out of scope

- Resisting a model actively attempting to evade the guardrail.
- Defending against crafted attack chains (TOCTOU races, pre-planted symlink/hardlink chains, case-folding tricks, malicious packages/dependencies, malicious concurrent host writers, or OS/kernel bugs).
- Defending against prompt-injection attack chains (though read-denying private state, write-protecting control files, and Read-Only network isolation still mitigate common exfiltration and persistence impact).
- Identical isolation strength across all operating systems.

## Platform backends and honest differences

- **Linux (Tier 1 — `bubblewrap` + `libseccomp2`)**: Requires system `bwrap`, `libseccomp.so.2`, and unprivileged user namespaces. Isolates mounts, network namespace (when networking is off), and syscalls while allowing anonymous Unix stream `socketpair(AF_UNIX, SOCK_STREAM, 0)` so Python `asyncio` and test runners work normally. Existing control files (and readable targets of symlinked control names such as `CLAUDE.md -> AGENTS.md`) are bind-mounted read-only; absent control names and unlinking a symlinked control entry itself are bind-mount limitations on Linux, while Broker file tools and untrusted project settings stripping still protect them.
- **macOS (Tier 1 — Seatbelt)**: Uses `/usr/bin/sandbox-exec` with a launchd job supervisor for process-tree cleanup. Seatbelt write-denies control metadata paths both when they exist and when they do not yet exist.
- **Windows + WSL2 (Tier 1)**: Recommended on Windows; uses the Linux `bubblewrap` + `libseccomp2` backend inside WSL2.
- **Native Windows (Experimental)**: Feature-frozen LPAC backend; defaults to **No-Limit**. See [experimental native Windows sandbox](windows-sandbox.md).

## Broker tools, output storage, and trusted integrations

- **File tools** run in the Broker directly against `PermissionPolicy` (no separate OS sandbox helper process), keeping cold start under 5 seconds and per-Read latency constant regardless of workspace size.
- **Command outputs** are piped to the Broker's session-persisted `OutputStore` under `<VIRGO_HOME>/outputs/session-<id>` (readable via `read_output` after session resume, cleaned up on session trash and by a 7-day TTL; untruncated foreground commands leave no spool files behind).
- **Trusted user integrations** (Python extensions, command hooks, and stdio MCP servers) run on **Host**; trust is established when you install them or trust a project (`extensions.trustedProjects` / `--trust-project-extensions`). Untrusted project `hooks`, `mcpServers`, `.virgo/mcp.json`, `providers`, and `extensions` are ignored before merging onto user settings.

See [Permission modes and approvals](../development/permission-profiles.md) · [Execution acceptance](../development/execution-acceptance.md) · [WSL2 setup](wsl2.md) · [Native Windows sandbox (experimental)](windows-sandbox.md)
