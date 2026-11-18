# Migrating permission settings

Set `execution.mode` in `~/.virgo/settings.json` (or `$VIRGO_HOME/settings.json`). For example, to keep the workspace sandbox:

```json
{"execution": {"mode": "workspace-edit"}}
```

An existing user settings file without an execution selection keeps the old Workspace-Edit default. This includes an empty JSON object or a file containing only a model or language. If the file contains custom presets without a selection, Virgo migrates the old effective Workspace-Edit preset. An explicit legacy `sandbox.mode=disabled` selects No-Limit.

When the user settings file does not exist, Virgo starts in No-Limit, including when only project settings exist. The first ordinary preference saved by the new version records this default. A command-line mode override applies only to that invocation.

The three fixed values are `read-only`, `workspace-edit` and `no-limit`. An explicit `--permission` selects the mode for this invocation. Project files cannot set a mode or install a saved approval.

For older user presets, migration compares the filesystem, managed web and Shell network permissions. The old writable workspace with networking maps to Workspace-Edit. Unrestricted execution maps to No-Limit. An old offline preset requires an explicit choice because both restricted modes now allow managed web access. An old read-only preset with networking also needs a choice because the new Read-Only disables Shell networking. Retired nonempty command allow/deny lists require manual migration. [Mode selection and saved approvals](../development/permission-profiles.md)
