# 003 — Tier 1 native sandbox backends and platform tradeoffs

[Decisions](README.md) · [Documentation](../README.md) · [简体中文](../../zh-CN/decisions/003-native-sandbox-backends.md)

**Status:** Accepted

## Context

Linux, macOS, and Windows provide fundamentally different process and filesystem isolation primitives. Chasing identical kernel-level semantics across all three OSes requires elevated privileges, daemon supervisors, syscall filters, or per-file ACL scans that violate cold-start budgets and break standard developer toolchains.

## Decision

Virgo maintains four **Tier 1** environments using each platform's simplest reliable native mechanism and documents honest platform differences:

- **Linux (`bubblewrap`)**: Runs `bwrap --unshare-all --unshare-user --disable-userns --cap-drop ALL` with `--info-fd` startup acknowledgement and no `libseccomp` runtime dependency. Mounts `/` and real `$HOME` read-only (`--ro-bind / /`) so user toolchains, `AF_UNIX`, `AF_NETLINK`, and `io_uring` work unmodified, while masking high-sensitivity credential paths (`--perms 000 --tmpfs` or empty read-only files) and mounting the workspace, session cache, and per-command `TMPDIR` writable.
- **macOS (Seatbelt + `HostProcess`)**: Probes `/usr/bin/sandbox-exec` once per executor and spawns commands directly inside a dedicated POSIX process group (`HostProcess`, reaped via `waitid(WNOWAIT)` / `kqueue` + `os.killpg`) without a background launchd supervisor. Grants read-only host access, workspace/cache/`TMPDIR` writes, hard denies on private credential paths, and write denies on `.virgo` (existing or absent).
- **Native Windows 11 (unelevated Standard AppContainer `windows-appcontainer`)**: Requires no Administrator privileges and is gated on `null.sys` Feature `60692039` (`\Device\Null` granting `ALL APPLICATION PACKAGES` `S-1-15-2-1`). Uses one AppContainer profile and SID per `(workspace, mode)` session with single-call OS-propagated `SetNamedSecurityInfoW` inheritable ACEs, `protect_readonly_path()` (`SE_DACL_PROTECTED` + explicit `ALLOW RX`) for existing unapproved `.virgo`, automatic read-only ACEs for `.venv` base Python and user-owned `PATH` toolchains, a session-local `DefineDosDeviceW` virtual drive when workspace ancestors lack `S-1-15-2-1`, an atomic write-ahead JSON lease journal (`%LOCALAPPDATA%\Virgo\sandbox-sessions\`) for startup crash recovery, and direct `CreateProcessW` + kill-on-close Job Object supervision (`WindowsProcess`).
- **Windows + WSL2**: Uses the Linux `bubblewrap` backend directly inside WSL2 as a co-equal Tier 1 environment.

## Tradeoffs and constraints

- **Unsupported Windows builds degrade visibly**: Production Virgo never mutates `\Device\Null`. Older Windows 10/11 and stock Windows Server builds without Feature `60692039` default to **No-Limit** with a one-time notice (WSL2 / No-Limit / Update Windows) and fail closed (`windows_null_device_unavailable`) if a sandbox mode is requested explicitly.
- **Honest platform limits stay accepted rather than worked around with privilege escalation**:
  - Native Windows blocks localhost/loopback (`127.0.0.1` / `[::1]`) in non-admin AppContainer (users use one-shot **No-Limit** or WSL2 for local servers/proxies) and disallows `$HOME` as a workspace.
  - Linux bind mounts and Windows NTFS/ReFS ACLs cannot deny creation of a previously absent `.virgo` inside a writable workspace from Shell (Broker file tools and untrusted project-config stripping still protect it).
  - macOS descendants that explicitly call `setsid()` escape process-group reaping, matching POSIX Host behavior.
- **Real-host acceptance required**: Every Tier 1 backend must pass native acceptance suites with `VIRGO_REQUIRE_SANDBOX_TESTS=1` and zero skips (see [Native execution & performance acceptance](../development/execution-acceptance.md)).

## Related

- [Execution boundaries & sandbox](../guides/sandbox.md)
- [Native Windows sandbox](../guides/windows-sandbox.md)
- [WSL2 setup](../guides/wsl2.md)
- [Native execution & performance acceptance](../development/execution-acceptance.md)
