# Native command sandbox

[Configuration](../reference/configuration.md) · [简体中文](../../zh-CN/guides/sandbox.md)

## Enable and inspect

The default permission preset is `Workspace-Edit`: writable workspace, workspace scope, and network access. `Read-Only` keeps workspace scope and networking while disabling host-data writes. `No-Limit` removes Virgo's native isolation and still runs with the host account's OS authority. A missing required backend refuses the command; it never silently switches to host execution.

```bash
uv run virgo
uv run virgo --mode read-only
uv run virgo --mode no-limit
```

User settings (`$VIRGO_HOME/settings.json`, normally `~/.virgo/settings.json`) can select or customize presets:

```json
{"permissions": {"mode": "workspace-edit", "modes": {"offline": {"name": "Offline", "readOnly": false, "workspaceOnly": true, "networkAllowed": false}}}}
```

Use `/mode` to inspect/select presets and the permission editor to change their names and flags. Project configuration cannot expand user permissions. Explicit legacy `--sandbox read-only` / `--sandbox workspace-write` or `sandbox.mode` settings retain their original offline behavior; `disabled` maps to No-Limit. `sandbox.network` is rejected with guidance to use `permissions.modes.<id>.networkAllowed`. Unknown modes and keys fail configuration.

`/sandbox` displays the current Runtime's flags and backend discovery, plus a pending preset when it differs from the running task. A discovered launcher is shown as launch-unverified; inspection does not execute a probe or certify that the kernel permits isolation. Every actual launch checks the boundary before releasing the command; Windows also checks the suspended process token and Job assignment. Explicit deny rules and read-only child ceilings remain applicable.

Workspace-Edit permits network-dependent operations when their file accesses fit the policy. External SDK/cache access and protected Git metadata can still block an operation. The model can use `request_permissions` to ask for one exact operation with additional flags; approval does not change the conversation preset or automatically execute/replay it. Editing protected control paths requires an explicit No-Limit operation. Windows remains experimental outside the validated runner configurations described in its guide.

## Execution boundaries

`read_only` controls host-data writes; `workspace_only` controls exposed user-file scope. Runtime directories and existing control metadata retain their documented protections. Linux/macOS expose their system/runtime roots in workspace-scoped policies; Windows uses object ACLs with ambient LPAC reads, described in [Windows command sandbox](windows-sandbox.md). Each command gets private writable scratch and HOME. Logs remain in the originating workspace’s `.virgo/outputs`, including commands run from another permitted directory. The broker validates paths before spooling and retains the exact output descriptor. The child receives only PATH, locale/timezone/terminal variables, private home/temp paths and VIRGO=1 (plus fixed native variables on Windows); inherited provider keys are not forwarded.

With `network_allowed=false`, Linux uses a private network namespace and denies sockets with seccomp; macOS supplies no Seatbelt network grant; Windows requests no network capabilities. With networking enabled, Linux shares the host network while permitting IPv4/IPv6 socket families, and macOS grants TCP/UDP access plus the specific system resolver/routing IPC endpoints required for DNS. Arbitrary Unix sockets and process-control escape routes remain restricted. Linux exposes exact resolver configuration files without exposing host service sockets below `/run`. Windows adds Internet/private-network capabilities without changing firewall rules or loopback exemptions. Network access can reach host TCP services; it is not a domain or remote-host allowlist. Core policy separately gates Web/MCP operations, while model/provider transport remains outside the command sandbox.

Foreground, background and child-runtime Shell tools use the same launcher and originating workspace. Plan and Code Review instructions do not change these flags or remove ordinary tools. Explicit read-only child capabilities can narrow inherited permissions. Running processes retain their launch envelope; changing the selected preset does not retroactively alter their rights.

An active sandbox also prevents host execution during shell discovery and selection. Startup configuration, PATH candidates, cached choices and `/shell` commands use executable-path checks without running version probes. The version remains `unknown`; the actual isolated launch determines whether the selected interpreter can execute. A missing backend never causes a host-side version probe. See [command shells](shells.md) for the resulting version and compatibility reporting.

Allow rules and ordinary tool approval do not disable an active sandbox. Only an explicit operation-bound permission request changes that operation’s flags. Missing launchers/libraries, unsupported platforms, a denied namespace or a rejected Seatbelt profile return `sandbox_unavailable` without running the payload natively. A successful probe alone does not establish that the actual launch succeeded: Linux/macOS run a fixed Python launcher inside the actual envelope, with isolated startup and a pipe acknowledgement that confirms command exec. If the launched process cannot be observed reliably, including an acknowledgement timeout, the result is `sandbox_failed` with possible side effects; a missing acknowledgement is not proof that the payload did not run. Windows creates the actual process suspended, verifies its token and Job assignment, then resumes it once. Native setup failures before resume are reported separately. Windows has no payload-start acknowledgement: a later DLL or runtime initialization failure is reported as the process exit, like other nonzero exits. Neither path guesses from stderr text. Ordinary nonzero exits and timeouts retain `command_failed` facts, with `command_started=true` and possible side effects. Timeouts and cancellation retain their separate statuses, and potentially partial operations are not automatically replayed. Shell results include the effective mode and workspace; background job results also retain backend metadata.

## Host requirements and limits

Linux requires a root-owned system `bwrap` supporting `--disable-userns`, `--ro-bind-fd`, `--share-net` and `--remount-ro`, system `libseccomp.so.2`, and permission to create the required namespaces. Protected paths use pinned source descriptors and bubblewrap's inode-verified read-only mounts; raw symlink paths are not used as protected mount sources. An older backend without the required options fails closed. The implemented seccomp architectures are x86-64 and AArch64.

macOS requires the system `/usr/bin/sandbox-exec`, `/bin/launchctl` and `/usr/bin/env`, a user launchd domain that accepts Background jobs, and the Darwin process-identity, versioned-signal and coalition-accounting interfaces. These private interfaces are checked at runtime; missing support rejects execution. Each command uses a unique temporary job, without installing a persistent LaunchAgent or enabling restart. The Windows backend and its current validation limits are documented in [Windows command sandbox](windows-sandbox.md). An unavailable boundary fails closed; `disabled` keeps the existing Windows executor.

With workspace scope enabled, Linux/macOS expose existing system roots (`/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/opt`; macOS additionally `/System/Library`, `/System/Cryptexes`, `/System/Volumes/Preboot/Cryptexes/OS`, `/Library`, `/dev` and `/private/etc`), interpreter installations, workspace and private scratch. Other SDKs/caches can remain unavailable. Disabling workspace scope expands user-file access, subject to read-only and protected-path rules; it does not disable network isolation. POSIX explicitly hides Virgo private broker storage, and macOS supervisor state lives below the hidden `/private/tmp/virgo-control-<uid>` parent. Windows retains its documented object ACL read boundary rather than claiming identical path masking. A workspace containing Virgo credentials/session storage or a system/base interpreter installation is rejected.

An active virtual environment nested below the workspace root, such as `.venv`, is supported. Its installed commands and packages remain usable, but its interpreter directories are read-only to both shell processes and Core `write`/`edit` tools. Interpreter-directory aliases inside the workspace must not be symbolic links; normal POSIX executable links such as `.venv/bin/python` remain supported. Windows skips ungranted reparse aliases below read-only runtime roots; it rejects reparse points in the workspace. The workspace root itself cannot be the interpreter installation.

Existing root `.git`, `.virgo`, `AGENTS.md` and `CLAUDE.md` paths are protected from shell writes. A symbolic link at any of these metadata paths rejects the shell launch on Linux/macOS; it cannot add a host read mount or replace the protected directory entry. Output-directory symbolic links also reject the launch before spooling. Linux implements metadata protection with read-only mounts of existing paths; it does not protect absent metadata names from creation. macOS also excludes absent metadata names. Windows rejects workspace reparses and protects existing metadata objects through ACLs, but does not reserve absent metadata names.

On Linux/macOS, before a writable, workspace-scoped Shell launch, Virgo checks the writable workspace tree for regular files with multiple hardlinks and rejects the launch if any exist. This includes links whose other names are also inside the workspace: the link count does not identify where the other names reside. Read-only metadata and active interpreter trees are excluded; `read-only` launches can use shared files. Projects that use hardlinked writable files, for example `cp -al` copies or some package-store layouts, must replace those files with independent copies before using `workspace-write`. The traversal does not follow symbolic links and is not an atomic snapshot against concurrent trusted host changes. Native commands cannot create hardlinks from read-only sources into the writable tree. Windows setup rejects multiply linked files throughout the workspace in both active modes, including metadata, while excluding runtime roots from that rejection.

Core `read`/`glob`/`grep` now use checked reads and traversal: POSIX retains no-follow directory/file descriptors; Windows pins native no-reparse handles. Private broker paths are excluded. These are application checks, not OS isolation of the broker.

The Core `write`/`edit` tools enforce workspace/read-only/metadata/interpreter ceilings separately at application level, including on Windows. Active policies reject existing multiply linked files. They stage changes in a fresh file under protected `.virgo` and replace the destination only after validating and writing the requested content, so unlinking an alias after opening cannot redirect a mutation to the old inode. Rejected or unchanged edits do not replace the destination, and staging files are removed. On POSIX, directory and source-file descriptors prevent symbolic-link replacement during opening; the destination must still identify the opened source before commit. Replacement retains file permission bits and text encoding/newlines, but creates a new inode and does not copy ownership or extended attributes. POSIX ACLs are not copied. Windows retains the current destination DACL, or derives a new file's DACL from its destination parent; an error finalizing permissions after replacement explicitly reports that content was already committed. See [Windows commit and cleanup](windows-sandbox.md#launch-and-cleanup). Staging and destination must be on the same filesystem; cross-filesystem replacement fails without modifying the destination. These Python operations are not inside the shell's OS sandbox. Windows Core file tools retain application-level path checks without POSIX directory-descriptor traversal. The native Windows shell backend does not place those broker operations inside LPAC.

With workspace scope enabled for the originating Runtime, automatically loaded project instructions (`AGENTS.md`, `CLAUDE.md`, `.virgo/SYSTEM.md` and `.virgo/APPEND_SYSTEM.md`) are confined to its workspace. Ancestors outside that workspace are excluded, and replacing a child runtime's working directory cannot import another project's instructions. The policy comes from that Runtime even when another conversation is visible or a prompt is being rebuilt. POSIX reads use descriptor-relative opens without following symbolic links, including when creating children. Explicitly trusted user instructions remain available. This automatic-context boundary does not add general read grants or isolate the broker itself.

### Process lifetime and cleanup

Linux places commands in private PID namespaces. On macOS, a trusted supervisor starts the Seatbelt command in a separate launchd resource coalition. The broker checks that this identity is private and unprivileged before releasing a command. A bounded, harmless detached-child probe must also demonstrate the cleanup mechanism before the broker's first user command; the proof deadline is shorter than the probe's own lifetime, so natural expiry cannot count as successful cleanup.

With the broker running, normal completion, nonzero exit, timeout, cancellation and startup failure use the same macOS cleanup path. Removing the exact launchd job alone is insufficient: the broker also identifies remaining members by their saved resource coalition and signals them using kernel-checked PID versions. New sessions, process groups and reparenting do not change ordinary descendants' coalition membership. A process snapshot alone never establishes completion: the kernel must report no active coalition tasks and the job registration must be absent before writable scratch is removed. Different commands have separate identities.

Teardown has a bounded deadline. If process control or cleanup cannot be verified after command release, the result is `sandbox_failed`, the command may already have produced effects, and it must not be replayed. Unverified cleanup retains temporary resources instead of deleting them through garbage collection. Background jobs report cleanup failure even when cancellation was requested. When intentional termination is verified but the supervisor could not forward the command's wait status, timeout/cancellation remains accurate and the exit code is unknown (`null`), rather than fabricated.

Abrupt loss of the Virgo broker on macOS, such as `SIGKILL` or a process crash, is outside this verified cleanup path. The supervisor attempts removal of its job when the control connection disappears, but that best-effort request does not prove all detached descendants exited. Such descendants retain their Seatbelt restrictions and may continue operations permitted by their original envelope; temporary resources may remain. Resource quotas and a universal cleanup deadline under arbitrary process load are not provided.

This is not an OS sandbox for the broker, trusted Python extensions, project hooks, remote MCP servers, provider-hosted tools, or arbitrary low-level calls by trusted integrations. Core file/Web/MCP entry points have separate application-level checks. Trusted MCP extension setup may start and connect servers before model tool invocation; a stdio server runs outside the command sandbox and may initiate its own network activity. Invocation gating does not isolate that server's lifecycle or the whole Virgo process. A writable workspace permits destructive changes to ordinary project files. Resource quotas and revocation of already running processes are not provided.

## Implementation and validation

`harness/sandbox.py` binds the session policy to the caller and enforces the file-tool ceiling. `tools/sandbox.py` builds native launch envelopes and checks POSIX command-start acknowledgements; the Windows adapter verifies the suspended process before resume. `tools/macos_process.py` manages the temporary launchd job, authenticated descriptor transfer, actual child status and kernel-verified teardown. Its supervisor is trusted broker infrastructure outside Seatbelt; the command and its descendants receive the existing Seatbelt profile. `tools/windows_sandbox.py`, `windows_sandbox_acl.py` and `windows_sandbox_files.py` implement the Windows token, ACL and native path operations; the existing `WindowsProcess` owns its process lifetime. Existing shell execution, output spooling, cancellation and background-job machinery remain the callers; there is no second agent loop.

`tests/harness/test_sandbox_e2e.py` specifies assembled-session and real-process scenarios for unavailable backends, configuration trust, shell discovery and selection, file-tool enforcement, hardlink aliases, unlink and path-replacement races, indirect and symlink writes, automatic project context, child/background ownership, network and environment isolation, existing metadata, workspace-local virtual environments, output placement, actual-launch failures, exit codes, timeouts, cancellation, detached descendants, ordinary/detached `posix_spawn`, double-forked orphans and preservation of an independent background command. The native CI workflow sets `VIRGO_REQUIRE_SANDBOX_TESTS=1`, making a missing native backend a failure instead of a capability skip. Passing mocked failure cases alone does not establish OS isolation; the native report and the complete cross-platform comparison report are separate validation results.

## Independent permission flags: implementation contract

The command policy has three independent flags: `read_only`, `workspace_only`,
and `network_allowed`. Read-only protects host data while retaining private
writable scratch and broker-owned session/output storage. Workspace-only exposes
the originating workspace plus documented read-only system/runtime paths; it is
not a promise that the interpreter can run without system files. Network policy
applies to the managed command process tree. Host/provider transport and trusted
extensions require their separate application-level policy.

Failure modes to verify before enabling a combination: a network grant also
unblocks arbitrary Unix sockets or process control; a full-host read grant permits writes;
a file tool follows an outside symlink despite workspace-only policy; a command
changes its grant root by changing cwd; or an unsupported backend silently runs
on the host. Native acceptance must exercise all seven constrained POSIX flag
combinations with real inside/outside file reads and writes, TCP access, and arbitrary Unix
socket rejection. A separate positive-control case requires actual system DNS and
external TCP access with networking enabled. Core file-tool acceptance must exercise read/search/mutation
scope separately. Backend discovery alone is not evidence that these checks pass.

Full-host writable policies authorize host-user file changes, including aliases.
They retain direct protected-path guards but cannot promise the workspace-only
protected-inode boundary: scanning the complete host for hardlinked aliases is
not performed. Denying direct network access does not undo host-file changes or
prevent a separate trusted host process from later acting on those changes.
Windows retains its LPAC object-ACL read semantics and ambient LPAC reads. It
supports workspace-scoped flags with optional Internet/private-network
capabilities; constrained policies without workspace scope are explicitly
unsupported. No loopback exemption or firewall configuration is changed.
