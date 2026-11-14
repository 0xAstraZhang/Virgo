# Scheduled tasks

[Documentation](../README.md) · [简体中文](../../zh-CN/guides/schedules.md)

## Create and manage a task

Enter `/schedule` to fill in a title, task prompt, schedule type and time, then review the summary and save. Creating a schedule does not call a model. At execution time, the saved prompt is delivered to the agent. Manually created tasks persist by default in the workspace's `.virgo/schedules.json`.

`/schedules` opens the task workspace. Select a task to read its full prompt, inspect the next run, edit it or delete it. Deletion requires confirmation. In the full-screen list, use arrows, Enter, `n` for a new task and Esc to return. Long details scroll; text editing uses Enter for newlines and Ctrl+S to confirm the field. Confirming a field does not save the whole task: the final summary still requires confirmation.

Both commands take no arguments and wait for the current model run to finish. Plain mode uses numbered choices. For multiline input, a line containing only `.` finishes; `/cancel` cancels; `.` alone keeps existing content during an edit. Noninteractive use, such as `virgo -p /schedules`, prints the list or usage without waiting for input.

Editing only the title or prompt preserves the next execution time. Choosing a new schedule recalculates it while retaining the task ID. A stale form cannot overwrite another session's update, deletion or completion of a one-off task.

## Schedule types

| Type | Input | Behavior |
| --- | --- | --- |
| One-off | A future date and time, or a relative value such as `+10m` | Run once |
| Interval | A positive number of minutes or hours; optional first-run time | Repeat by elapsed time |
| Daily | `HH:MM` | Match local wall-clock time |
| Weekly | A weekday and `HH:MM` | Match local weekday and time |
| Custom cron | Five fields: minute, hour, day, month, weekday | Match the expression in the local time zone |

The wizard displays the system's local time zone. A one-off timestamp may include a UTC offset. Intervals use elapsed minutes, including across daylight-saving changes, and allow values such as every 7 minutes or every 25 hours.

Without a first-run time, an interval starts counting from final save; the summary's time is an estimate. With an explicit start, subsequent runs follow that original cadence rather than drifting with execution or polling delays. A delayed scheduler delivers at most one occurrence and advances the next time into the future.

## Persistence and restart behavior

**Virgo must stay running for tasks to trigger.** This feature does not install an operating-system cron job or a background service.

After restart, interval tasks skip missed occurrences and continue their cadence; cron tasks do not replay missed times. An overdue persisted one-off task runs once on a later scheduler check. Persisted tasks in the same workspace are shared between sessions within one Virgo process, with a single delivery owner. Independent Virgo processes do not coordinate delivery.

Cancelling an unfinished create/edit flow leaves stored tasks unchanged. While the full-screen task workspace is open, background approvals wait until you return to the conversation.

## Model tool contract

The `schedule` tool supports `add`, `list`, `get`, `edit` and `cancel`:

- `add` takes `prompt`, optional `title` and `persist`, and exactly one timing rule: `at`, `cron` or `interval_minutes`. Intervals may also specify `start_at`.
- `list` returns identifiers, types, timing and titles. `get` with an `id` returns the complete prompt.
- `edit` changes selected fields. To replace an interval, provide the full new timing rule; `start_at` alone is insufficient.
- `cancel` deletes by `id`.

Model-created tasks default to `persist=false`. Existing task files are read without manual migration; title-less tasks use the first nonempty prompt line for display. New timing fields may not be writable by older Virgo versions, so avoid alternating versions against the same schedule file.

Implementation: [scheduler](../../../src/virgo/features/schedule.py), [wizard](../../../src/virgo/features/schedule_commands.py), [task workspace](../../../src/virgo/cli/tui/schedules.py).
