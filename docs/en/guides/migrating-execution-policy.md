# Migrating execution policy

[Documentation](../README.md) · [简体中文](../../zh-CN/guides/migrating-execution-policy.md)

Virgo now separates work intent, explicit approval and the execution boundary. This is an incompatible change: old permission modes fail configuration instead of being translated dynamically. Saved conversations and plan Markdown remain readable; reopening history does not restore authorization or start execution.

## Update configuration

| Retired setting or interface | Replacement |
| --- | --- |
| `permissions.mode=plan`, `/permissions plan` | `workMode=plan`, `--work-mode plan` or `/mode plan` |
| `permissions.mode=auto-review`, `default`, `accept-edits` | `permissions.approval=ask`; uncertain operations ask a person |
| `permissions.mode=accept-all`, `yolo` | Choose explicit user-owned allow rules and `ask` or `never`; there is no automatic equivalent |
| `permissions.readOnlyCommands` | Remove it; Plan and read-only children have no arbitrary Shell tool |
| `plugins.auto_review`, `features.permission` | Remove them; Core authorization is always installed and no model grants permissions |
| `sandbox.network` | Remove it; active Shell sandboxes block networking |
| `--permission-mode ...` | `--approval ask\|never` and, separately, `--work-mode default\|plan` |
| `--mode text\|json` | `--format text\|json` |
| Shell allow command prefixes such as `bash(npm test*)` | Exact complete command grants such as `bash(npm test)`, or an intentional tool-only grant |

`never` means that operations needing a new approval are rejected. It does not grant all tools, ignore deny rules, leave Plan or disable Sandbox. Explicit deny rules always remain effective.

Move grants and the approval choice out of project `.virgo/settings.json` into user settings. Project `permissions` can only add deny rules; its list is combined with user denials. A project cannot clear user denials, select an approval strategy or declare more commands read-only. Project sandbox settings remain ignored.

A user configuration for ordinary interactive work:

```json
{
  "workMode": "default",
  "permissions": {
    "approval": "ask",
    "allow": ["bash(uv run --no-sync pytest -q)"],
    "deny": ["bash(git push*)"]
  },
  "sandbox": {"mode": "workspace-write"}
}
```

An optional project restriction:

```json
{
  "permissions": {"deny": ["write(secrets/**)", "edit(secrets/**)"]}
}
```

A Shell allow rule with a command matches its complete text exactly; command wildcard patterns are rejected. Path/web allow patterns and deny patterns retain their glob semantics. A tool-only `bash` grant authorizes the entire Bash capability within the current hard boundaries, including potentially destructive changes to ordinary workspace files. Select such grants deliberately.

## Approvals and remembered decisions

Ordinary workspace file operations remain deterministic. Simple local Shell forms may run automatically within an active sandbox. Host Shell, outside paths, network and unknown operations need an explicit grant or human approval. An unavailable native sandbox never turns into host execution. Sandbox limits and read-only ceilings are checked before asking a question that could not authorize the operation.

Human approval can be one-shot or remembered for the exact originating operation. Dynamic Shell paths and PowerShell scripts are one-shot. Remembered decisions bind task identity, tool, arguments, cwd, resolved paths, environment and explicit policy. `/permissions revoke` clears remembered grants and denials. Changing explicit policy also invalidates prior decisions. The main agent still receives conversation instructions, but a separate model no longer interprets transcript text as an authorization-system grant or revocation.

## Default execution boundary

The default is now `workspace-write`, including the minimal profile. Use `--sandbox read-only` for a hard write ceiling, or explicitly choose `--sandbox disabled` for host execution. That choice does not alter approval policy or WorkMode. Native setup failures remain errors and commands are not automatically replayed.

Active Shell sandboxes block network access and protect repository metadata/instruction paths. Dependency installation, remote Git actions and commits may therefore be unavailable. See [the sandbox guide](sandbox.md) and [Windows prerequisites](windows-sandbox.md) before choosing a host environment. The Windows backend remains experimental: administrator CI results do not establish standard-account desktop compatibility. Core Python file tools use separate application restrictions; trusted extensions, hooks and remote tools are not placed inside the Shell OS sandbox.

Plan and read-only children use file reading, search and controlled `git_read`. They do not receive arbitrary Bash or PowerShell, even when the host sandbox is explicitly disabled. Code Review remains an independent optional task with frozen source evidence and validated findings, not a third work mode.

## Integration APIs

- Use `AgentSession(approval_policy="ask" | "never")` and `session.approval_policy` for approval choices.
- Use the existing permission service's `set_work_mode(...)` for work intent.
- Create restricted children with `read_only=True`; an optional child `approval_policy="never"` can narrow interactive approval. Children cannot widen parent ceilings.
- Carry `ToolContext.origin` / `ToolCallEvent.origin`, backed by `ExecutionOrigin`, through deferred work. Capture it before publishing a background task. Do not substitute the currently selected conversation.
- `ExecutionOrigin` retains workspace, source Runtime and branch-bound accounting. The old `ReviewSource` transcript, reviewer input and model-verdict APIs are removed.
- Custom permission policies add restrictions to mandatory Core checks. They do not replace the Core policy. The old reviewer registration API and `features.permission*` imports are retired.
- Mode/plan notifications run after state commits. They can observe and refresh UI; they cannot synchronously reenter state changes or publish work. Schedule any next explicit action through the normal admission path after notification completes.

Work mode changes require the originating conversation and its queued/running child agents to be idle. Already running native processes retain their launch boundary; switching modes is not process revocation. Plan execution selects an exact plan version for one run and preserves current approval/Sandbox settings. Editing a plan invalidates an older execution selection; restoring history does not execute it.
