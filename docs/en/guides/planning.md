# Plan a change before executing it

[Documentation](../README.md) · [简体中文](../../zh-CN/guides/planning.md)

Plan mode lets Virgo investigate a repository and maintain a proposed implementation plan. Publishing or revising that plan keeps the conversation in Plan. To start implementing the selected version, use its explicit execution command.

## Investigate, review and execute

Start in Plan with `virgo --work-mode plan`, or enter `/mode plan` in an existing conversation:

```text
/mode plan
Investigate how this project loads configuration. Propose a plan to add an optional timeout without changing files.
/plan
```

Virgo's Plan instructions direct it to read project instructions, inspect code and existing tests, and find repository facts before asking questions. When the `ask_user` plugin is available, it is instructed to use it only for a decision that investigation cannot resolve and that materially changes the scope, approach, behavior or acceptance conditions. An unanswered question remains an unresolved decision or a stated assumption. Without that tool, the model can ask in the conversation.

The main agent publishes the full Markdown through the Core `propose_plan` tool. Its instructions ask for a title, goal, change scope, key decisions, implementation steps, acceptance criteria and known constraints. This is conversation metadata; publishing it does not create a plan file in the project or authorize code changes.

`/plan` and `/plan show` display the current Markdown, its version token and its confirmation state. Continue the conversation to request a revision:

```text
Keep the existing timeout as the default. Revise the plan to include the error message and compatibility behavior.
/plan show
```

When the current version is ready, copy its displayed token into:

```text
/plan execute <plan-id>:<revision>
```

Replace the placeholder with the exact token shown by `/plan`. A missing, stale or foreign token is rejected. This user command confirms that version, switches the originating conversation to Default, and starts its existing Runtime with the complete selected Markdown in the user request. It does not change the approval strategy or Sandbox. Normal tool permission checks still apply to every operation.

`/mode default` only leaves Plan; it does not confirm or execute the proposal. Text produced by the model, the plan itself and an answer to a clarification question cannot invoke the execution command.

## Review and edit in the full-screen interface

Press Ctrl+G to open the current proposal, including its Markdown and exact version token. Edit the body and select **Save changes** (Ctrl+S) to save a new unconfirmed revision. Unsaved edits disable **Execute this plan**. After saving, reopen the window to review the new version, then select **Execute this plan** when ready. The window uses the same version checks and execution command as `/plan`; an outdated window cannot overwrite or execute a replacement. **Close** or Esc discards unsaved edits without executing.

Shift+Tab toggles Default and Plan through `/mode`. It does not confirm a proposal or change approval strategy or Sandbox. The footer displays all three dimensions separately and prioritizes them over model metadata when space is limited. Ctrl+P and Ctrl+N continue to browse input history.

A busy conversation rejects plan saves and execution rather than queuing them. Wait for it to finish or stop it, review the current version and select execution again.

## Edit the Markdown with a command

Use `/plan edit` to replace the complete proposal through the client's existing multiline input. In the plain terminal interface, enter the replacement lines and finish with a line containing `.`; `/cancel` cancels, and `.` alone keeps the supplied text. The current Markdown is shown as the starting content. A client without interactive input can supply the Markdown with the command:

```text
/plan edit # Optional timeout

## Goal
Add a configurable timeout while preserving the current default.

## Scope
Configuration parsing and its existing timeout consumer.

## Decisions
An omitted value retains current behavior; an invalid value produces a configuration error.

## Steps
1. Read the existing configuration and timeout paths.
2. Add the option and connect it to the consumer after explicit execution.
3. Run the existing acceptance checks.

## Acceptance
Omitted values preserve behavior; valid values are used; invalid values are rejected.

## Constraints
Keep the current dependencies, approval policy and Sandbox.
```

Send that block as one command. Saving a replacement enters Plan, increments the revision and clears confirmation of the earlier version. The edit replaces the body; it is not a patch or a task graph. Asking the model to revise the plan has the same revision and confirmation behavior.

Integrations holding a displayed version can make the replacement conditional:

```text
/plan edit <plan-id>:<revision>
<complete replacement Markdown>
```

The token must be followed by a newline and the full body. If the version changed before saving, the replacement is rejected rather than overwriting the newer plan. The interactive `/plan edit` path also checks the version it opened. Core admission requires an idle conversation and checks the captured version again; a rejected admission does not start execution.

## Plan, Todo, approval and Sandbox

| Concern | Role |
| --- | --- |
| ProposedPlan | One versioned Markdown proposal per conversation: the intended solution and acceptance criteria |
| Todo | The existing execution checklist, with `pending`, `in_progress` and `completed` statuses |
| WorkMode | Plan adds a read-only tool ceiling; Default permits ordinary work subject to the other policies |
| Approval strategy | Determines which tool calls are allowed, reviewed or sent for human approval |
| Sandbox | Enforces the configured file and process restrictions independently |

`propose_plan` is a Core tool available only to the main Runtime in Plan. It is marked read-only with respect to project changes and saves metadata in SessionStore. It does not give subagents a second plan store. `todo_write` is hidden and denied in Plan; it remains the existing checklist tool for execution in Default. Executing a plan does not create a second task store or automatically turn its steps into Todo items.

`/permissions accept-all` does not remove the Plan ceiling. Executing a plan or returning to Default retains the selected approval strategy and active Sandbox. A read-only Sandbox can therefore continue to reject file writes after a plan is confirmed. Plan itself does not enable an OS sandbox; see [Native command sandbox](sandbox.md) for its separately configured boundary.

## Switching conversations and restoring history

Plan state belongs to the main Runtime of its conversation. Child runtimes inherit the originating parent's effective mode, including after the user selects another conversation. A model proposal from an older Runtime updates that Runtime's plan, not the currently visible conversation.

| Entry point | Work-mode behavior |
| --- | --- |
| A fresh conversation | Uses configured `workMode`, whose default is `default` |
| Switching to a retained conversation in the same process | Retains that conversation's Runtime, plan and work mode |
| Opening saved history | Restores the saved work mode; configured `workMode: "plan"` keeps a Plan ceiling even if history saved Default |
| Explicit `--work-mode plan\|default` or `AgentSession(work_mode="plan" \| "default")` | Overrides the saved/configured independent work mode for the initial conversation only |

Ordinary startup with the default setting does not discard a saved Plan mode. An explicit CLI or constructor override does not change the default for subsequently created conversations or write a user preference. Use `virgo --continue --work-mode plan` to resume with an explicit Plan ceiling. The CLI `--mode text|json` remains the output-format option; it is independent of `--work-mode`.

When `--resume` opens the full-screen session picker, `--work-mode` applies to the selected startup conversation before any initial prompt runs; later session switches do not reuse it. An override of a retained conversation is rejected while its main agent or any queued or running descendant is active; the selected conversation, saved mode and initial prompt remain unchanged until the work finishes.

Cold restoration clears the plan's live confirmation state. The historical confirmation entry remains available for auditing, but does not queue a request, wake a Runtime or authorize execution again. Approval strategy and Sandbox are taken from the current session configuration, never restored from the plan. Viewing history and selecting a mode do not execute a plan.

The existing SessionStore holds `work_mode` and `proposed_plan` custom records. Each saved plan snapshot contains `plan_id`, `revision`, `markdown` and `confirmed_revision`; the model context uses the current plan. Mode and plan changes emit `work_mode_changed` and `proposed_plan_changed` through the existing event bus. There is no separate plan file or task database. As with other session state, a persistence failure blocks execution.

## Request and execution boundaries

Entering Plan restricts calls that have not started. The next model request uses Plan instructions and tools, and the existing preflight and execution checkpoints enforce the mode for the originating Runtime. Leaving Plan or confirming execution is rejected while that conversation has an active request or a queued or running child agent, even if its main agent has finished. Wait for that work to finish or stop it before trying again. Work owned by another conversation does not block this one. Work already pending at admission is rejected before saving confirmation. If the plan, mode or selected conversation changes during an admission check, the older action is rejected and the newer selection is preserved.

Already running processes retain the permissions and Sandbox policy used at launch. A mode change does not terminate them or retroactively revoke their access. The mode status distinguishes that limit; see [Command shells](shells.md) for cancellation behavior.

Legacy `permissions.mode=plan`, `--permission-mode plan` and the direct `session.permission_mode` API remain accepted. `/permissions plan` enters the independent WorkMode without replacing an already selected approval strategy. See [Work mode configuration](../reference/configuration.md#work-mode) for migration behavior.

Implementation: [plan lifecycle](../../../src/virgo/harness/plans.py), [Runtime](../../../src/virgo/harness/session.py), [permission policy](../../../src/virgo/harness/permission_policy.py). Acceptance uses the existing tool loop and SessionStore in [Plan workflow scenarios](../../../tests/harness/test_plan_workflow_e2e.py).
