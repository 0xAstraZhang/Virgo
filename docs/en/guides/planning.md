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

`/plan` and `/plan show` display the current proposal and its version token. Continue the conversation to request a revision:

```text
Keep the existing timeout as the default. Revise the plan to include the error message and compatibility behavior.
/plan show
```

When the current version is ready, copy its displayed token into:

```text
/plan execute <plan-id>:<revision>
```

Replace the placeholder with the exact token shown by `/plan`. A missing, stale or foreign token is rejected. This user command selects that version for one run, switches the originating conversation to Default, and starts its existing Runtime. The full selected Markdown appears once in the current system context; the execution request references its token. It does not change the approval policy or Sandbox. Normal tool permission checks still apply to every operation.

`/mode default` only leaves Plan; it does not confirm or execute the proposal. Text produced by the model, the plan itself and an answer to a clarification question cannot invoke the execution command.

## Review and edit in the full-screen interface

Press Ctrl+G to open the current proposal, including its Markdown and exact version token. Edit the body and select **Save changes** (Ctrl+S) to save a new revision. Unsaved edits disable **Execute this plan**. After saving, reopen the window to review the new version, then select **Execute this plan** when ready. The window uses the same version checks and execution command as `/plan`; an outdated window cannot overwrite or execute a replacement. **Close** or Esc discards unsaved edits without executing.

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

Send that block as one command. Saving a replacement enters Plan and increments the revision. Every execution must select the current token explicitly; the proposal carries no reusable execution grant. The edit replaces the body; it is not a patch or a task graph. Asking the model to revise the plan has the same version behavior.

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
| WorkMode | The originating conversation's intent: Plan adds investigation instructions and a read-only tool ceiling; Default permits ordinary work subject to the other policies |
| Read-only capability | Independently limits a child Runtime's tools without turning its task into planning |
| Approval policy | `ask` can request permission for an operation that needs approval; `never` rejects such operations without prompting |
| Sandbox | Enforces the configured file and process restrictions independently |

`propose_plan` is a Core tool available only to the main Runtime in Plan. It is marked read-only with respect to project changes and saves metadata in SessionStore. It does not give subagents a second plan store. `todo_write` is hidden and denied in Plan; it remains the existing checklist tool for execution in Default. Executing a plan does not create a second task store or automatically turn its steps into Todo items.

Plan does not expose Bash, PowerShell or arbitrary shell commands. The Core `git_read` tool provides bounded local history, committed patches and comparisons between two commits through fixed Git arguments. Revisions resolve to immutable commit IDs before reading. It does not expose live status/diff, fetch, repository helpers or conversion filters. Normal file read/search tools remain available subject to their permissions. A read-only child in Default keeps its own task instructions; an independent `/review` never receives Plan instructions, including when its originating conversation is in Plan.

`/permissions ask` and `/permissions never` do not remove the Plan or read-only ceiling. `never` is not blanket permission: denied rules and operations needing approval remain blocked. Executing a plan or returning to Default retains the selected approval policy and Sandbox setting. Plan itself is an application tool boundary. Native Sandbox execution is separately requested by default and fails closed when unavailable; see [Native command sandbox](sandbox.md) for its configured and effective boundaries.

## Switching conversations and restoring history

Plan state belongs to the main Runtime of its conversation. Child runtimes inherit the originating parent's effective mode, including after the user selects another conversation. A model proposal from an older Runtime updates that Runtime's plan, not the currently visible conversation.

| Entry point | Work-mode behavior |
| --- | --- |
| A fresh conversation | Uses configured `workMode`, whose default is `default` |
| Switching to a retained conversation in the same process | Retains that conversation's Runtime, plan and work mode |
| Opening saved history | Restores the saved work mode; configured `workMode: "plan"` keeps a Plan ceiling even if history saved Default |
| Explicit `--work-mode plan\|default` or `AgentSession(work_mode="plan" \| "default")` | Overrides the saved/configured independent work mode for the initial conversation only |

Ordinary startup with the default setting does not discard a saved Plan mode. An explicit CLI or constructor override does not change the default for subsequently created conversations or write a user preference. Use `virgo --continue --work-mode plan` to resume with an explicit Plan ceiling. The CLI `--format text|json` selects the output format independently of `--work-mode` and `--approval ask|never`.

When `--resume` opens the full-screen session picker, `--work-mode` applies to the selected startup conversation before any initial prompt runs; later session switches do not reuse it. An override of a retained conversation is rejected while its main agent or any queued or running descendant is active; the selected conversation, saved mode and initial prompt remain unchanged until the work finishes.

The selected plan belongs only to its executing run and is cleared when that run ends. Restoring history restores the proposal, with no active execution. The historical execution entry remains available for auditing, but does not queue a request, wake a Runtime or authorize execution again. Approval policy and Sandbox are taken from the current session configuration, never restored from the plan. Viewing history and selecting a mode do not execute a plan.

The existing SessionStore holds `work_mode` and `proposed_plan` custom records. Each proposal contains only `plan_id`, `revision` and `markdown`; a separate `plan_execution` audit record references the selected ID and revision. The model context distinguishes a proposal from the selected plan for the active run. Mode and plan changes emit `work_mode_changed` and `proposed_plan_changed` after publishing their state. Event listeners may observe these notifications but cannot reenter state-changing operations. There is no separate plan file or task database. A persistence failure blocks execution; a failed startup mode override preserves the earlier runtime state.

## Request and execution boundaries

Changing work mode or selecting plan execution requires an idle originating conversation, including its queued or running child agents. Wait for that work to finish or stop it before trying again. Work owned by another conversation does not block this one. Admission checks the selected runtime and exact current version under the existing session lock, records the execution selection, and establishes the mode before notifying listeners. Notifications cannot revise the plan, change mode, switch conversation or publish new child work during that operation. Publishing a proposal from an active Plan request remains possible because staying in the same mode does not change its boundary.

The existing preflight and execution checkpoints enforce the originating Runtime's capability and policy on every call. Already launched independent processes retain their launch permissions and Sandbox; changing the conversation's mode does not terminate them. See [Command shells](shells.md) for cancellation behavior.

Legacy `permissions.mode`, `--permission-mode`, `session.permission_mode`, `/permissions plan` and the approval-review plugin are removed. Use `workMode` or `/mode plan` for planning intent, `permissions.approval` or `/permissions ask|never` for approval, and `create_runtime(..., read_only=True)` for a restricted child capability. See [Work mode configuration](../reference/configuration.md#work-mode).

Implementation: [plan lifecycle](../../../src/virgo/harness/plans.py), [Runtime](../../../src/virgo/harness/session.py), [permission policy](../../../src/virgo/harness/permission_policy.py), [controlled Git reads](../../../src/virgo/tools/git_read.py). Acceptance uses the existing tool loop and SessionStore in [Plan workflow scenarios](../../../tests/harness/test_plan_workflow_e2e.py).
