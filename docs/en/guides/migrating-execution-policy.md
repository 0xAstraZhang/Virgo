# Execution-policy migration

[Documentation](../README.md) · [简体中文](../../zh-CN/guides/migrating-execution-policy.md)

Task instructions and execution permissions are now independent. Plan and Code Review are
optional plugins with normal tool access. They request plans/reports through prompts; they do
not impose a read-only permission ceiling. Existing conversation content and plans remain readable.

## Update configuration

| Previous entry | Current entry |
| --- | --- |
| `/mode plan` / `/mode default` | `/plan` / `/plan off` |
| `workMode=plan`, `--work-mode plan` | Use `plan: true` / `--plan`; still selects planning instructions when the Plan plugin is enabled |
| `/mode` | Deprecated alias for `/permissions`; lists/selects editable **permission** presets |
| `--mode text|json` | `--format text|json`; the old `--mode` preset spelling is a deprecated alias for `--permission` |
| `sandbox.mode=workspace-write` / `read-only` | Explicit legacy offline policy; remove it to use the new defaults |
| `sandbox.mode=disabled` | Explicit No-Limit filesystem/network policy |
| `sandbox.network` | Use a preset's `networkAllowed` boolean |
| `permissions.mode=plan|default|accept-edits|accept-all|yolo|auto-review` | Removed approval aliases; select `permissions.preset` = `read-only`, `workspace-edit`, `no-limit`, or a custom preset ID |
| `permissions.readOnlyCommands`, `plugins.auto_review` | Removed; Shell syntax/model verdicts do not establish execution boundaries |

New default: `permissions.preset=workspace-edit` with writable workspace and task networking.
Read-Only also permits networking; offline operation is a custom three-flag preset. Names and
permissions are editable independently. See [configuration](../reference/configuration.md#permissions)
for the full JSON and `/permissions save` syntax. User settings own presets and approvals; projects only add deny rules.

## Approvals and remembered decisions

Managed operations within the selected profile run directly. `request_permissions` asks for one
exact operation with explicit permission changes; approval does not execute the operation. The next
identical call consumes the exception. The permission preset stays unchanged. Different arguments, caller/run,
resolved targets, shell environment, policy revision or tool instance cannot reuse it.

`ask` permits requests; `never` refuses new requests, not all operations. `/permissions revoke`
clears remembered decisions and pending exceptions. Explicit deny rules remain binding. Failed
or possibly side-effectful commands are not replayed automatically. Opening history does not
restore grants or execute a plan. Trusted extension policies can add restrictions.

## Default execution boundary

Every run captures its source permissions. Permission preset changes during work apply to subsequent runs;
background work and child runtimes keep their original source. Read-only protects host file data,
with private scratch and broker records allowed. It does not promise to prevent remote mutations.

Core file tools enforce file scope at application level. Native Shell backends enforce supported
process policies; managed Web/native search and remote MCP calls also respect network policy.
MCP services unable to enforce file scope need an explicit unrestricted one-operation exception.
The broker and trusted Python extensions are outside the Shell OS boundary.

Check [Sandbox](sandbox.md) and [Windows limitations](windows-sandbox.md). Windows retains ambient
LPAC read grants and rejects constrained `workspaceOnly=false` combinations. Backend discovery
is not proof of a successful launch; unsupported/unavailable isolation rejects execution. No-Limit
uses the host account's existing rights, not automatic administrator privileges.

## Integration APIs

`SandboxManager.mode_for(runtime)` returns the captured `PermissionMode`; `selected_mode` is the
current conversation's next selection. `select_mode(id)` selects locally and `save_mode(...)`
persists named flags in user settings. Configuration strings are never used as a substitute for
the effective flags. `Runtime.prepare_run()` captures the policy.

`read_only=True` remains an explicit child ceiling without removing tool schemas. Profile
inheritance is separate from that ceiling. Delayed work carries `ToolContext.origin` so a TUI
conversation change cannot transfer authority. Runtime-only permission exceptions are never
restored from stored custom records.

Plan is installed through `plugins.plan`; Core has no mandatory Plan service. Extensions can add
request-level instructions with `add_request_system_section`, and use the existing Runtime and
SessionStore. Review retains target resolution, frozen evidence and validated findings, with
normal tools and request/interaction handling. The compatibility `harness.plans` import remains;
new integrations should use the plugin API.
