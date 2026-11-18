# Permission modes

Use `--permission read-only`, `--permission workspace-edit` or `--permission no-limit`. Fresh installations use No-Limit. The compatibility flag `--execution host` selects No-Limit. The CLI and TUI use the same three choices.

Migration compares the old filesystem, web and Shell network permissions with the fixed modes. Virgo reports a configuration error when no mode preserves all three. An old offline workspace policy needs an explicit decision because Workspace-Edit enables Shell networking and both sandboxed modes allow web access.

`/permissions show` reports the current task and next-task mode. `/permissions list` lists the modes. Use `/permissions grants` and `/permissions revoke ID` to manage saved resource approvals. Projects cannot grant execution authority. A running child or background operation keeps its captured context.

See [permission modes](../development/permission-profiles.md) and [native backends](../guides/sandbox.md).
