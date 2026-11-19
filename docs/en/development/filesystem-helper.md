# Filesystem Helper and output records

The built-in Read, Write, Edit, Glob and Grep tools run in a dedicated Python process through `SandboxExecutor`. Their adapters validate arguments and render results, passing target paths unchanged to the helper. File-content and directory operations happen in that process. The helper is a standard-library-only runtime snapshot captured before model execution, including when Virgo is running from an editable checkout. The subsequent permission-policy migration removes the remaining Broker admission-time canonicalization and obsolete file-guard interfaces.

Snapshots live below a stable `virgo-runtime` directory in the operating system's per-user cache, separate from Broker-private storage and command temporary directories. Initialization rejects links, unexpected owners and non-private permissions on the managed directory; it preserves active Windows execution ACEs. Every restricted process is prevented from modifying this entire runtime namespace, including snapshots created by later executors. Native read grants cover the runtime dependencies needed by the current process. Closing an executor removes only its own snapshot and retains the parent namespace so older processes keep their protection over future instances. No-Limit keeps its explicitly authorized host access.

The Broker captures helper source bytes once when its helper adapter module loads, before the first executor can release model work. Later executors reuse those captured bytes rather than reopening a potentially modified editable installation. Changes to the helper implementation take effect after a trusted Broker restart.

## Transport and bounds

The executor launches the snapshot with isolated Python startup (`-I -S`). Request and result JSON use private stdin/stdout pipes with a four-byte length prefix. Each frame is limited to 16 MiB. Diagnostics use a separate bounded stderr stream. The payload never reads a request from a model-writable project file.

Text reads return at most 2,000 lines and approximately 50 KiB before the pagination notice. Image payloads are limited to 5 MiB. Grep skips files over 5 MiB and bounds displayed matches. Internal project-content reads return at most 8 MiB, and internal directory listings stop at 10,000 entries with an explicit truncation flag. Automatic instruction reads reject symbolic links in every path component.

The executor waits for completion and owns the same process cleanup as Shell. A write may already have created directories or committed its replacement before cancellation or an output failure is observed. Such failures retain possible-side-effect evidence and do not trigger automatic replay.

## Atomic mutations and permissions

Write and Edit open regular files through retained, no-follow directory handles. After all edit matching succeeds, they stage a new file in the destination directory and atomically replace the destination entry. The helper never switches to truncating the existing inode when replacement is denied. POSIX preserves the existing file mode and the established BOM/CRLF editing behavior.

POSIX path traversal retains every ancestor with search-only descriptors (`O_PATH` on Linux, `O_SEARCH` on macOS), keeping the all-component no-follow check without requesting ancestor directory listings. Enumeration separately opens the requested directory for reading. Parent creation first opens existing components and attempts `mkdir` only for missing components, so writing a workspace file does not request mutations to its existing ancestors.

Staging in the destination directory keeps replacement on the same filesystem and avoids making project `.virgo` metadata writable. An atomic file mutation requires permission to create and rename an entry in its parent directory. A grant for only one existing file is sufficient for a Shell command that writes that inode, but is insufficient for this helper's atomic replacement. Request the parent directory explicitly; authorization must not silently expand to it.

Replacing a hardlink entry leaves other names of its old inode unchanged. This does not establish that arbitrary Shell writes through pre-existing hardlinks are safe. During this migration, native launch-time hardlink checks and the existing Windows recursive ACL checks remain in force.

On Windows, executor initialization captures direct DOS-to-physical-volume mappings in the protected runtime snapshot, independently of model paths. The helper normalizes DOS paths lexically and opens the corresponding complete native path with `OBJ_DONT_REPARSE`. It checks each handle's NTFS volume and identity, retaining the verified parent for child opens and mutations. File operations reject filesystem reparses without requiring host DOS-device namespace queries, drive-root access or strict final-path canonicalization inside LPAC. Atomic replacement uses `FileRenameInformationEx`. Staging files inherit the destination parent's ACL, including currently active execution grants. They do not copy the old file's explicit per-file DACL or request Broker `WRITE_DAC` privileges. Applications that require preservation of custom per-file ACLs must account for this replacement behavior. Protected runtime and control paths remain subject to the native backend's restrictions.

## Private outputs

Every mode stores command and large tool output in the Virgo user directory. The child writes to a pipe; only the Broker opens the output record. Outputs no longer use project `.virgo/outputs`, and there is no filesystem-tool exception for reading private output files.

Results expose a random `output_id`. `read_output` accepts that ID and byte `offset`/`limit` arguments. The ID must resolve to an exact record owned by the originating conversation; an arbitrary path, a fabricated record or another conversation's ID is rejected. Child tasks keep their originating conversation's output ownership when the selected session changes.

These guarantees cover built-in local tools. Trusted host extensions, hooks and MCP transports retain their separately documented boundaries until their executor integrations are installed.

## Verification and test migration

`tests/tools/test_fs.py` keeps the existing editing and search cases and now launches real helper processes. Its scoped-access cases use a real `SandboxExecutor`; a missing backend is a failure, not simulated isolation.

The POSIX and Windows native suites call the same filesystem acceptance body. It exercises all five file tools, checks Read-Only denial by directly calling the executor, verifies protected metadata and runtime snapshots, and checks private output ID ownership. A process already running with workspace write access also attempts to modify a later executor's snapshot and an ordinary canary created under the shared runtime parent; ordinary workspace writes must still succeed. These checks must run on their actual platforms before release; local host-process checks alone do not establish native isolation.

The two `test_core_file_mutation_survives_hardlink_unlink_race[write/edit]` cases traced the removed Broker mutation frame and are retired. Their replacement is `test_core_file_mutation_replaces_hardlink_without_touching_source[write/edit]`: actual No-Limit helper processes must commit a new inode and leave the external source unchanged. The native hardlink and path-replacement tests continue separately. No output or algorithm test identity was retired.
