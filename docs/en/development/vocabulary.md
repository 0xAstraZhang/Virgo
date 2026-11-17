# Vocabulary

[Architecture](architecture.md) · [Core and plugin boundaries](core-plugin-boundaries.md) · [简体中文](../../zh-CN/development/vocabulary.md)

Virgo has two independent axes. Use one word for each axis in code, configuration, the CLI, slash commands and documentation. A synonym is a migration alias, not a second concept.

## The two axes

| Axis | Canonical term | What it decides | Where it lives |
| --- | --- | --- | --- |
| Capabilities | **plugins** (with **`plugins.<id>.enabled`**) | Which optional tools and commands exist in a session | `plugins.<id>.enabled` |
| Permissions | **permission preset** (with **approval**) | Which managed file, Shell and network operations may run | `permissions.preset`, `permissions.presets`, `permissions.approval`, `--permission`, `--approval`, `/permissions` |

## Canonical terms

| Term | Meaning | Not |
| --- | --- | --- |
| `plugins.<id>` | An explicit capability switch; official plugins are enabled by default | `features.<id>` |
| permission preset | A named set of `readOnly`, `workspaceOnly` and `networkAllowed` flags with a stable ID | "mode", "sandbox mode", "permission Mode" |
| approval | `ask` or `never`: whether a new single-operation approval may be requested | "auto review", "accept-edits", "yolo" |
| sandbox | The native OS boundary behind a permission preset | A permission preset itself |
| session | One persisted conversation and its store | "pane", "tab" |
| conversation | One live runtime shown in a pane | "session" when the stored file matters |

`preset` alone always means a permission preset. Capabilities are selected per plugin. Do not use "mode" for a permission preset.

## Command and flag surface

| Intent | Canonical | Deprecated alias (one release) |
| --- | --- | --- |
| Inspect or change permission settings | `/permissions [preset-id\|list\|save <id> <JSON>\|ask\|never\|revoke\|show]` | `/mode`, `/sandbox` |
| Select the startup permission preset | `--permission <preset-id>` | `--mode` |
| Select approval policy | `--approval ask\|never` | — |
| Inspect backend availability | `/permissions show` | `/sandbox` |

Deprecated aliases print a notice. They do not change the meaning of the canonical entry point and are removed in a later release.

## State ownership

- `SandboxManager` owns the selected permission preset and maps it to a platform backend.
- `Permissions` owns remembered approvals and denials and makes the deterministic decision for one tool call.
- `Runtime.permission_snapshot` captures the preset, approval policy and revision once at `prepare_run()`.
  A selection change while work is running applies to the next run; the running snapshot stays fixed.
- Children inherit the parent snapshot. An explicit read-only child is a ceiling, not a new preset.

## Configuration migration

New configuration uses:

```json
{
  "permissions": {
    "preset": "workspace-edit",
    "presets": {},
    "approval": "ask",
    "deny": ["bash(git push*)"]
  }
}
```

These old keys are read with a notice and are not written back automatically:

| Old | New |
| --- | --- |
| `permissions.mode` | `permissions.preset` |
| `permissions.modes` | `permissions.presets` |
| `--mode <id>` | `--permission <id>` |
| `sandbox.mode` | A permission preset with the same flags |
| `features.<id>` | `plugins.<id>` |

A project may only add `permissions.deny`. It cannot select a preset or set approval. Remove a legacy key before upgrading to 0.4.

## Naming rules for new code

1. One owner per state. A second copy of the same decision needs a snapshot or a derived view, not another field.
2. Core code does not name plugin tools or commands. Declare behavior on `AgentTool` metadata or register a policy.
3. Plugins use `ExtensionAPI`. They do not write undeclared attributes on `Runtime` or reach into `session._lock`.
4. A command returns user-facing output through `UI.command_result` (or a return value); ordinary status text uses `UI.print`.
5. If a term is not in this page, do not add it to configuration, the CLI, a slash command or user documentation.
