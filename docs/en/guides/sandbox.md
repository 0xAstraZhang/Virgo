# Native command sandbox

[Configuration](../reference/configuration.md) · [简体中文](../../zh-CN/guides/sandbox.md)

## Enable and inspect

Native isolation is opt-in. The default `disabled` preserves existing execution; it does not claim OS protection. Select an envelope at startup:

```bash
uv run virgo --sandbox workspace-write
uv run virgo --sandbox read-only
```

User settings (`$VIRGO_HOME/settings.json`, normally `~/.virgo/settings.json`) can set:

```json
{"sandbox": {"mode": "workspace-write", "network": "restricted"}}
```

Project `sandbox` settings are ignored with a notice. An explicit startup override wins over user settings. Unknown modes, keys and network values fail configuration. `/sandbox` displays the requested mode and discovered launcher; it does not change permissions or certify that the host kernel permits isolation. Every launch probes the actual envelope before starting the payload.

## Execution boundaries

`workspace-write` exposes the originating workspace as writable and system/interpreter roots as read-only. `read-only` also makes the workspace read-only. Each command gets private writable scratch and a private `HOME`. The broker retains its exact output-log descriptor. The child environment contains only `PATH`, locale/timezone/terminal variables, private home/temp paths and `VIRGO=1`; provider keys and other inherited credentials are not forwarded.

The only supported active network policy is `restricted`. Linux uses a private network namespace and a seccomp filter that denies sockets, including pathname Unix sockets, and io_uring. macOS uses a deny-by-default Seatbelt profile without a network grant. Disabling isolation is an explicit startup choice, not an approval result or automatic retry.

Foreground shell tools, model-requested background commands and child-runtime shell tools use the same launcher. The workspace comes from the originating runtime, not the currently selected conversation or a child's replacement working directory. Plan and read-only child restrictions narrow each new launch to read-only. Already running processes keep their launch envelope; mode changes do not retroactively revoke their rights.

`accept-all`, allow rules, model review and human approval do not disable an active sandbox. Missing launchers/libraries, unsupported platforms, a denied namespace or a rejected Seatbelt profile return `sandbox_unavailable` without running the payload natively. Ordinary nonzero exits remain ordinary command failures; they are not inferred to be sandbox denials, and potentially partial operations are not automatically replayed. Shell results include the effective mode and workspace; background job results also retain backend metadata.

## Host requirements and limits

Linux requires a root-owned system `bwrap` supporting `--disable-userns`, system `libseccomp.so.2`, and permission to create the required namespaces. The implemented seccomp architectures are x86-64 and AArch64. macOS requires the system `/usr/bin/sandbox-exec`. Windows has no native backend in this stage: active sandbox requests fail closed, while `disabled` keeps the existing Windows executor.

The read set comprises existing system roots (`/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/opt`; also `/System/Library`, `/System/Cryptexes`, `/System/Volumes/Preboot/Cryptexes/OS`, `/Library`, `/dev` and `/private/etc` on macOS), the interpreter installations, the workspace and private scratch. It is not a full host-home mount. Tools, dependency caches, certificates or SDKs outside that set may be unavailable; extra mounts and network exceptions are not implemented. A workspace containing Virgo's user credential/session directory or a sandbox/interpreter installation is rejected rather than exposing writable trusted runtime files.

Existing root `.git`, `.virgo`, `AGENTS.md` and `CLAUDE.md` paths are protected from shell writes. Linux implements this with read-only mounts of existing paths; it does not protect absent metadata names from creation. macOS also excludes absent metadata names. The Core `write`/`edit` tools enforce workspace/read-only/metadata ceilings separately at application level; those Python operations are not inside the shell's OS sandbox.

This is not a sandbox for the broker, trusted Python extensions, project hooks, remote MCP servers, provider-hosted tools, or arbitrary direct use of low-level execution helpers by trusted integrations. A writable workspace still permits destructive changes to ordinary project files. Resource quotas, read-grant management, one-command privilege escalation, Windows isolation and revocation of already running jobs are not provided.

## Implementation and validation

`harness/sandbox.py` binds the session policy to the caller and enforces the file-tool ceiling. `tools/sandbox.py` builds and probes native launch envelopes. Existing shell execution, output spooling, cancellation and background-job machinery own the process lifecycle; there is no second agent loop.

`tests/harness/test_sandbox_e2e.py` specifies assembled-session and real-process scenarios for unavailable backends, configuration trust, file-tool enforcement, indirect and symlink writes, child/background ownership, network and environment isolation, existing metadata, exit codes and timeouts. The native CI workflow sets `VIRGO_REQUIRE_SANDBOX_TESTS=1`, making a missing native backend a failure instead of a capability skip. Passing mocked failure cases alone does not establish OS isolation.
