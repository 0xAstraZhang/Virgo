# Execution and Sandbox

Fresh configurations use **Host** execution. File tools and commands use the host account's OS permissions. Virgo does not import the Sandbox Extension, discover an isolation backend, start its services, review commands, or ask for ordinary command approval on this path. `--execution host` makes the choice explicit. `/permissions show` reports the active provider.

Task instructions describe intent. For example, “do not push” is a task convention; preventing pushes technically requires restricting credentials, remote authorization or other capabilities.

## Selecting Sandbox

Select `--execution sandbox` or set `execution.provider` to `sandbox` in user settings. Install the official MXC **1.1.0** standalone executor yourself and configure its absolute path as `sandbox.executable`. Virgo checks the platform artifact digest. It never downloads, updates or falls back from MXC at task startup. Missing extensions, executors and unsupported resource policies fail closed.

**Native process support is currently blocked.** A real macOS ARM64 test started MXC successfully and completed an allowed write, then modified a protected outside file through a hardlink inserted after launch. Scanning paths before startup does not close this race. Virgo therefore rejects shared-host-path native policies before starting payloads. Installing MXC does not enable supported Shell isolation. See [acceptance evidence](../development/execution-acceptance.md). Windows additionally lacks successful PSEC startup evidence on the evaluated CI hosts. There is no legacy native backend fallback.

The optional extension retains checked file-tool access separately from the native process adapter. Successful file-tool tests do not prove Shell isolation. Neither a discovered executor nor a matching digest is an established sandbox.

## Resources and inheritance

Named presets use `readPaths`, `writePaths` and `networkAllowed`. Writable paths are also readable. Relative paths resolve from the originating workspace. `read-only` grants workspace reads, `workspace-edit` grants workspace reads and writes; both allow networking. `no-limit` is not a Sandbox preset.

Each task captures its execution provider, workspace, resource policy and revision. Children and queued background operations inherit that context. Switching visible conversations, changing cwd or choosing a preset for a later run cannot transfer another task's authority. The removed child `read_only` option cannot create a separate permission model.

File tools retain pinned directories and file identity checks, reject shared-file aliases, and stage writes into new inodes before replacement. Failed edits do not commit content; existing encoding and permission semantics are preserved. Broker storage and runtime protection cannot be waived by an operation grant.

`request_permissions` requests additional paths or networking for one exact file, process or network operation. It binds the source run, tool instance, normalized arguments, cwd, environment, policy revision and resource identities. A grant expires after five minutes or at run end, whichever happens first. Dispatch consumes it atomically; rejection, cancellation, expiry and failure do not restore it. History is audit data, not executable permission. Unsupported native/file-level grants are rejected without widening to a parent directory. An already started process keeps its launch boundary until it ends.

## Scope and results

Project context, skills, prompt templates and output tails use the originating file interface. User-owned configuration, instructions and trusted user skills are broker inputs outside that project resource boundary.

Trusted Python extensions, hooks, MCP service startup and model transport run outside Sandbox. Managed Web dispatch checks the network resource policy. External tools that cannot enforce the requested policy are refused. Do not describe the whole application as isolated.

Results distinguish the requested policy, effective file boundary, native establishment state, provider, restricted resource, payload start evidence, possible side effects and cleanup. Entering a Python tool function does not prove that a payload started. Unknown evidence remains unknown. Result hooks cannot replace recorded execution facts. Partial failures, timeouts and cleanup errors are not automatically replayed.

MXC's macOS cleanup contract is **best effort**, including normal completion. The old coalition supervisor is removed. Observing a descendant exit is useful evidence for that case, not a universal guarantee. MXC warnings or uncertain cleanup are reported as failures with diagnostics retained. Host POSIX cleanup concerns the live process group; detached descendants are not universally contained.
