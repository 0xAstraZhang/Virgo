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

`workspace-write` exposes the originating workspace as writable and system/interpreter roots as read-only. `read-only` also makes the workspace read-only. Each command gets private writable scratch and a private `HOME`. Output logs belong to `.virgo/outputs` under the originating workspace root, including commands run from a nested directory. The broker validates the working directory and output paths before spooling and opens the output directories without following symbolic links; it retains the exact output-log descriptor. The child environment contains only `PATH`, locale/timezone/terminal variables, private home/temp paths and `VIRGO=1`; provider keys and other inherited credentials are not forwarded.

The only supported active network policy is `restricted`. Linux uses a private network namespace and a seccomp filter that denies sockets, including pathname Unix sockets, and io_uring. macOS uses a deny-by-default Seatbelt profile without a network grant. Disabling isolation is an explicit startup choice, not an approval result or automatic retry.

Foreground shell tools, model-requested background commands and child-runtime shell tools use the same launcher. The workspace comes from the originating runtime, not the currently selected conversation or a child's replacement working directory. Plan and read-only child restrictions narrow each new launch to read-only. Already running processes keep their launch envelope; mode changes do not retroactively revoke their rights.

An active sandbox also prevents host execution during shell discovery and selection. Startup configuration, PATH candidates, cached choices and `/shell` commands use executable-path checks without running version probes. The version remains `unknown`; the actual isolated launch determines whether the selected interpreter can execute. A missing backend never causes a host-side version probe. See [command shells](shells.md) for the resulting version and compatibility reporting.

`accept-all`, allow rules, model review and human approval do not disable an active sandbox. Missing launchers/libraries, unsupported platforms, a denied namespace or a rejected Seatbelt profile return `sandbox_unavailable` without running the payload natively. A successful probe alone does not establish that the actual launch succeeded: the actual envelope runs a fixed Python launcher with isolated startup and a pipe acknowledgement that confirms command exec. Startup failure is distinguished from an ordinary nonzero command exit without interpreting stderr text. Timeouts and cancellation retain their separate statuses, and potentially partial operations are not automatically replayed. Shell results include the effective mode and workspace; background job results also retain backend metadata.

## Host requirements and limits

Linux requires a root-owned system `bwrap` supporting `--disable-userns` and `--ro-bind-fd`, system `libseccomp.so.2`, and permission to create the required namespaces. Protected paths use pinned source descriptors and bubblewrap's inode-verified read-only mounts; raw symlink paths are not used as protected mount sources. An older backend without the required options fails closed. The implemented seccomp architectures are x86-64 and AArch64. macOS requires the system `/usr/bin/sandbox-exec`. Windows has no native backend in this stage: active sandbox requests fail closed, while `disabled` keeps the existing Windows executor.

The read set comprises existing system roots (`/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/opt`; also `/System/Library`, `/System/Cryptexes`, `/System/Volumes/Preboot/Cryptexes/OS`, `/Library`, `/dev` and `/private/etc` on macOS), the interpreter installations, the workspace and private scratch. It is not a full host-home mount. Tools, dependency caches, certificates or SDKs outside that set may be unavailable; extra mounts and network exceptions are not implemented. A workspace containing Virgo's user credential/session directory, a system/base interpreter installation or the sandbox launcher is rejected.

An active virtual environment nested below the workspace root, such as `.venv`, is supported. Its installed commands and packages remain usable, but its interpreter directories are read-only to both shell processes and Core `write`/`edit` tools. Interpreter-directory aliases inside the workspace must not be symbolic links; normal executable links such as `.venv/bin/python` remain supported. The workspace root itself cannot be the interpreter installation.

Existing root `.git`, `.virgo`, `AGENTS.md` and `CLAUDE.md` paths are protected from shell writes. A symbolic link at any of these metadata paths rejects the shell launch on both platforms; it cannot add a host read mount or replace the protected directory entry. Output-directory symbolic links also reject the launch before spooling. Linux implements metadata protection with read-only mounts of existing paths; it does not protect absent metadata names from creation. macOS also excludes absent metadata names.

The Core `write`/`edit` tools enforce workspace/read-only/metadata/interpreter ceilings separately at application level. On POSIX, active policies also pin each directory and the regular file with descriptors, reject symbolic-link replacement during opening, and retain the same file descriptor through editing. These Python operations are not inside the shell's OS sandbox. Windows retains application-level path checks; it has no native sandbox backend.

With an active policy, automatically loaded project instructions (`AGENTS.md`, `CLAUDE.md`, `.virgo/SYSTEM.md` and `.virgo/APPEND_SYSTEM.md`) are confined to the originating workspace. Ancestors outside that workspace are excluded, and replacing a child runtime's working directory cannot import another project's instructions. POSIX reads use descriptor-relative opens without following symbolic links, including when rebuilding prompts or creating children. Explicitly trusted user instructions remain available. This automatic-context boundary does not add general read grants or isolate the broker itself.

### Blocking process-lifetime limitation

macOS does not provide complete descendant cleanup. A descendant that detaches into a new session/process group can survive command completion, timeout or cancellation and continue writes permitted by its original workspace envelope. Seatbelt restrictions remain attached to that descendant. This is a blocking limitation of the current native-sandbox implementation; cleanup of the command's process group and private scratch does not establish that every descendant has exited. Linux's private PID namespace passes the corresponding normal-exit, timeout and cancellation acceptance cases.

This is not a sandbox for the broker, trusted Python extensions, project hooks, remote MCP servers, provider-hosted tools, or arbitrary direct use of low-level execution helpers by trusted integrations. A writable workspace still permits destructive changes to ordinary project files. Resource quotas, read-grant management, one-command privilege escalation, Windows isolation and revocation of already running jobs are not provided.

## Implementation and validation

`harness/sandbox.py` binds the session policy to the caller and enforces the file-tool ceiling. `tools/sandbox.py` builds and probes native launch envelopes and checks the actual command's startup acknowledgement. Existing shell execution, output spooling, cancellation and background-job machinery manage the command process and scratch resources, subject to the macOS descendant limitation above; there is no second agent loop.

`tests/harness/test_sandbox_e2e.py` specifies assembled-session and real-process scenarios for unavailable backends, configuration trust, shell discovery and selection, file-tool enforcement and path-replacement races, indirect and symlink writes, automatic project context, child/background ownership, network and environment isolation, existing metadata, workspace-local virtual environments, output placement, actual-launch failures, exit codes, timeouts, cancellation and detached descendants. The native CI workflow sets `VIRGO_REQUIRE_SANDBOX_TESTS=1`, making a missing native backend a failure instead of a capability skip. Passing mocked failure cases alone does not establish OS isolation; the native report and the complete cross-platform comparison report are separate validation results.
