# Migrating execution settings

Fresh configurations now use Host. Explicit old isolation requirements are **not** silently converted to Host. Remove an old requirement only when Host is your intended authority; otherwise explicitly select Sandbox and review its current [native blockers](../development/execution-acceptance.md).

If your user settings already select a supported preset such as `read-only`, you can select the provider at startup with `--execution sandbox`. Startup validates the preset after applying this option.

| Retired setting or API | Migration |
| --- | --- |
| `sandbox.mode`, `--sandbox` | Choose `execution.provider` / `--execution` explicitly; install and configure MXC for Sandbox. |
| `readOnly`, `workspaceOnly` preset flags | Use `readPaths` and `writePaths`. Workspace read-only maps to `readPaths:["."], writePaths:[]`; workspace write maps to both `["."]`. Review network separately. |
| `no-limit` | Use `execution.provider=host`; it is not a resource preset. |
| Nonempty `permissions.allow/deny` | Remove command rules. Put task conventions in instructions; express technical capabilities with resources. There is no equivalent resource mapping for a command-intent prohibition such as `git push`. |
| `permissions.mode/modes` | Deprecated spelling of `preset/presets`; the new path schema and explicit provider requirement still apply. |
| Project permissions/execution configuration | Move authority to user settings or startup overrides. Project configuration cannot grant authority or change trust. |
| `/mode`, `/sandbox` | Use `/permissions show`; Sandbox also exposes preset selection, saving and resource-request policy. Host exposes status only. |
| Agent-tool / Runtime / ExtensionAPI `read_only` argument | Remove it. Children inherit the source task context. |
| `services.permissions`, `services.sandbox`, `PermissionPolicy`, `register_permission_policy` | Use the single-owner execution-provider contract. Trusted extension code itself is outside the managed boundary. |
| `sandbox_policy` in command adapters | Pass the captured `execution_context`. Never construct one from the current visible session when servicing older work. |
| `details.sandbox` / `command_started` | Read `details.boundary` / `payload_started`. `details.execution.started` describes entry into the Python tool function. |

The legacy native backends, command parser, ordinary tool approvals and session approval cache are removed. `ask` and `never` control only explicit Sandbox resource increments. Existing authorization history remains historical; it cannot restore execution rights.

Windows no longer has the old LPAC backend. MXC ProcessContainer startup remains unaccepted on the previously evaluated CI hosts because PSEC was unavailable. macOS no longer uses the coalition supervisor; MXC cleanup is explicitly best effort. See [Sandbox](sandbox.md) for effective scope and [Windows](windows-sandbox.md) for support status.
