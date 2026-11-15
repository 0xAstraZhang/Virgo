# Independent code review

[Documentation](../README.md) · [简体中文](../../zh-CN/guides/code-review.md)

## Start a review

Start Virgo at the Git repository root, then select a target:

```text
/review
/review uncommitted
/review base main
/review base refs/remotes/origin/main
/review commit HEAD
/review commit <commit-sha>
/review show last
/review show <review-id>
```

`/code-review` accepts the same arguments. At startup, the plugin registers each name only when no existing command or prompt template occupies it, and warns about conflicts. If `review.md` already supplies `/review`, use `/code-review`. If both names are occupied, rename a conflicting template or command before restarting. Existing user files are preserved.

The `coding` preset enables `code_review`. This plugin is separate from `auto_review`, which assists permission approvals. Set `plugins.code_review.enabled` to override the preset, for example:

```json
{"profile": "minimal", "plugins": {"code_review": {"enabled": true}}}
```

Plain and JSON print modes run the same task:

```bash
uv run virgo -C /path/to/repository -p "/review base main"
uv run virgo -C /path/to/repository -p --mode json "/review uncommitted"
uv run virgo -C /path/to/repository --continue -p "/review show last"
```

JSON mode emits a `code_review` event containing a `report` object. Exit status is `0` for a completed, current report, `1` for an unsuccessful, incomplete, stale or unverifiable result, and `130` for cancellation. Findings alone do not make execution unsuccessful. `--no-session` keeps results only in memory.

## Targets and snapshots

| Target | Frozen comparison |
| --- | --- |
| `uncommitted` (default) | HEAD → index, index → working files, and non-ignored untracked files, in separate `staged`, `unstaged` and `untracked` layers |
| `base <branch>` | The unique merge-base of the resolved branch and HEAD → HEAD; unrelated baseline advances and dirty working files are excluded from this diff |
| `commit <revision>` | First parent → selected commit; root commits compare against an empty tree |

A staged edit reversed in the working tree remains visible in both layers. Merge commits use their first parent. Baselines must identify an unambiguous local/remote branch; use a full ref to disambiguate a branch from a same-named tag. Review does not fetch branches or missing objects.

The Harness resolves controlled Git argv, then freezes file bytes and old/new line mappings. Staged and committed rename detection uses Git object/index comparisons. Unstaged removals and untracked additions remain separate changes. Read/search tools select frozen `head`, `index`, `worktree`, `base` or `target` versions. They never read a later live file; symlinks expose link text rather than their targets.

Saved snapshots record the requested target, resolved commits, comparison, change metadata, file fingerprints and a snapshot ID. They identify the reviewed code; they do not permanently archive every source byte. Source contents are held in memory for the task.

## Read-only execution

Each review creates a fresh child of its originating Runtime and uses the existing Agent Loop. The source workspace, model, shared request budget, cancellation and permission ancestry are inherited. Parent messages are not copied. `subagents.maxTurns` bounds the child; compaction is disabled, so context overflow becomes an incomplete result.

Only `read`, `glob`, literal `grep` and `review_git` are offered. `review_git` pages through frozen status/diff and accepts no arbitrary commands or Git arguments. There are no file mutation, shell, network, MCP, background or delegation tools. The task cannot fix, commit or merge code.

The command bypasses prompt hooks. Its Runtime excludes executable tool/stop hooks, context/system-section injection, dynamic tool registration and hosted web preparation. Both Core permission gates remain active, and snapshot content respects inherited read restrictions. `accept-all`, allow rules and approvals cannot add tools. Results are bounded in memory; the review does not create `.virgo/outputs` files.

Controlled Git reads disable optional index writes, external diff, textconv, fsmonitor, hooks and implicit transport. Working files are compared from captured bytes, without executing clean/smudge filters. Lifecycle/results and diagnostics use Virgo's user/session locations; the task refuses to start when those destinations resolve inside the reviewed workspace.

This is the official task's capability boundary. Trusted Python extensions still run in the host process; their initialization and event observers are not sandboxed by this task. Starting review does not stop previously started independent jobs.

## Reports and finding locations

| Status | Meaning |
| --- | --- |
| `running` | Capture or review is in progress |
| `completed` | A complete report passed validation, or the exact target has no changes |
| `invalid_report` | JSON, fields, types or a finding's location failed validation |
| `incomplete` | Context, turn/request budget or model output cannot establish a complete conclusion |
| `failed` | Target selection, execution, provider or cleanup failed |
| `cancelled` | Cancellation was observed |

Reports contain `id`, `status`, `snapshot`, `summary`, `findings`, timestamps and freshness. Errors retain diagnostic evidence when available. Each finding has `change_id`, `path`, `side`, `start_line`, `end_line`, `priority` (integer 0–3), `title`, `explanation` and finite `confidence` (0–1). The change ID distinguishes layers of the same file; `side` selects old/deleted or new/added lines. A location must match the exact path/side, exist in that version, overlap a changed line and span at most 20 lines.

The reviewer is instructed to identify actionable defects introduced by these changes, with concrete evidence and impact, excluding preexisting issues, speculation and style preferences. Structural validation cannot prove model reasoning correct. Invalid output, refusal, truncation, overflow, missing evidence and cancellation never become successful empty findings. The UI publishes the validated report instead of raw final model text.

## Saved results and current limits

Lifecycle/results are `code_review` custom records in the originating session branch, outside the parent model conversation. `/review show` rechecks fingerprints without calling a model and displays `current`, `stale` or `unknown`, preserving the saved conclusion and snapshot. Freshness includes workspace/index context: a later workspace edit can mark a fixed-commit report stale even though that commit remains immutable. An unfinished `running` record reopened without a live task is displayed as incomplete.

Freshness is also checked before publishing a terminal report. Capture detects observed ref/index/file changes and refuses a mixed snapshot; it does not lock out concurrent host writers. Rerun review after changing code. Normal cancellation terminates/reaps controlled Git work and releases child Runtime registration, permission bindings and persistence-failure signals.

Limits are 1 MiB per file, 32 MiB captured content, and 10,000 paths per version. Tool results page up to 400 lines and remain below 45 KiB. Required oversized/unavailable evidence yields incomplete. Changed binary/non-UTF-8 content, unresolved index conflicts and uncommitted submodules are outside this text-review increment. Unsupported clean filters, `working-tree-encoding` and `ident` conversions are reported as missing context without executing them. Standard text/line-ending metadata is handled in snapshot comparison. Broad searches encountering unavailable/binary context report skipped evidence and mark the review incomplete; scope searches to readable source paths when appropriate.

Pure renames and mode-only changes are recorded, but have no changed text-line anchor for a finding in this increment. A Runtime opened on a subdirectory cannot widen its workspace to the repository root. The command has no automatic repairs, separate review model setting, patch application, PR publication or multi-review orchestration.

Implementation: [targets/tools](../../../src/virgo/harness/code_review_targets.py), [validation/display](../../../src/virgo/harness/code_review_report.py), [task plugin](../../../src/virgo/plugins/code_review.py). The [E2E acceptance scenarios](../../../tests/harness/test_code_review_e2e.py) were committed before production implementation.
